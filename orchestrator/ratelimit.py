"""Gemini requests-per-minute limiter for free-tier API keys.

Interval spacing = 60 / rpm, pro vs flash buckets, enabled only on the free tier;
asyncio-native (never blocks the event loop).

The limiter is injectable: the workers install a Valkey-backed limiter shared across
processes (workers/entrypoint.py); without Valkey the in-process default applies.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional, Protocol

logger = logging.getLogger(__name__)


def model_bucket(model: Optional[str]) -> str:
    return "pro" if model and "pro" in model else "flash"


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def rpm_limit_enabled() -> bool:
    """ENGINE_RPM_LIMIT = on | off | auto (auto: on unless paid key or Vertex AI).

    With ENGINE_MODEL_ROUTING=gateway, auto is off: the model gateway limits per role
    and per account tier in one place (a second client-side limit would double-count)."""
    mode = os.environ.get("ENGINE_RPM_LIMIT", "auto").strip().lower()
    if mode in {"on", "true", "1"}:
        return True
    if mode in {"off", "false", "0"}:
        return False
    if os.environ.get("ENGINE_MODEL_ROUTING", "").strip().lower() == "gateway":
        return False
    paid = _truthy(os.environ.get("GOOGLE_GENAI_PAID_API_KEY", "FALSE"))
    vertex = _truthy(os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "FALSE"))
    return not (paid or vertex)


def rpm_for(bucket: str) -> int:
    default = "2" if bucket == "pro" else "10"
    return max(1, int(os.environ.get(f"ENGINE_RPM_{bucket.upper()}", default)))


class RpmLimiter(Protocol):
    async def acquire(self, model: Optional[str]) -> float:
        """Wait until a request slot is free. Returns seconds waited."""


class InProcessRpmLimiter:
    """Spaces requests at 60/rpm seconds per model bucket within one process."""

    def __init__(self) -> None:
        self._next_slot: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, model: Optional[str]) -> float:
        bucket = model_bucket(model)
        interval = 60.0 / rpm_for(bucket)
        async with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot.get(bucket, 0.0))
            self._next_slot[bucket] = slot + interval
        wait = slot - now
        if wait > 0:
            logger.info("RPM limiter: waiting %.1fs for %s slot", wait, bucket)
            await asyncio.sleep(wait)
        return wait


class _NoopLimiter:
    async def acquire(self, model: Optional[str]) -> float:
        return 0.0


_limiter: Optional[RpmLimiter] = None


def set_rpm_limiter(limiter: Optional[RpmLimiter]) -> None:
    global _limiter
    _limiter = limiter


def get_rpm_limiter() -> RpmLimiter:
    global _limiter
    if not rpm_limit_enabled():
        return _NoopLimiter()
    if _limiter is None:
        _limiter = InProcessRpmLimiter()
    return _limiter


async def rpm_before_model_callback(callback_context: Any, llm_request: Any) -> None:
    """ADK before_model_callback: throttle every agent LLM call. Returning None continues."""
    await get_rpm_limiter().acquire(getattr(llm_request, "model", None))
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Valkey-backed cross-process limiter
# ─────────────────────────────────────────────────────────────────────────────

_RESERVE_SLOT = """
local now = tonumber(ARGV[1])
local interval = tonumber(ARGV[2])
local nxt = tonumber(redis.call('GET', KEYS[1]) or '0')
local slot = now
if nxt > now then slot = nxt end
redis.call('SET', KEYS[1], slot + interval, 'PX', 120000)
return slot - now
"""


class ValkeyRpmLimiter:
    """Shared Gemini RPM budget backed by Valkey; one free-tier budget per model bucket.

    Each call atomically reserves the next free slot for its model bucket
    (pro/flash) using a Lua script and sleeps until that slot.
    """

    def __init__(self, client: Any, prefix: str = "engine:rpm") -> None:
        self._client = client
        self._prefix = prefix

    async def acquire(self, model: Optional[str]) -> float:
        bucket = model_bucket(model)
        interval_ms = int(60_000 / rpm_for(bucket))
        now_ms = int(time.time() * 1000)
        wait_ms = int(await self._client.eval(
            _RESERVE_SLOT, 1, f"{self._prefix}:{bucket}", now_ms, interval_ms
        ))
        if wait_ms > 0:
            await asyncio.sleep(wait_ms / 1000)
        return wait_ms / 1000


# ─────────────────────────────────────────────────────────────────────────────
# Bounded reservation (model gateway)
# ─────────────────────────────────────────────────────────────────────────────

_RESERVE_BOUNDED = """
local now = tonumber(ARGV[1])
local interval = tonumber(ARGV[2])
local max_wait = tonumber(ARGV[3])
local nxt = tonumber(redis.call('GET', KEYS[1]) or '0')
local slot = now
if nxt > now then slot = nxt end
if slot - now > max_wait then return -1 end
redis.call('SET', KEYS[1], slot + interval, 'PX', 120000)
return slot - now
"""


_RELEASE_TAIL = """
local nxt = tonumber(redis.call('GET', KEYS[1]) or '0')
if nxt == tonumber(ARGV[1]) then
  redis.call('SET', KEYS[1], tonumber(ARGV[1]) - tonumber(ARGV[2]), 'PX', 120000)
  return 1
end
return 0
"""


class Reservation:
    """A reserved slot: wait `wait_s`; `next_ms` is what the key held after reserving it."""

    __slots__ = ("key", "wait_s", "next_ms", "interval_ms")

    def __init__(self, key: str, wait_s: float, next_ms: int, interval_ms: int) -> None:
        self.key, self.wait_s, self.next_ms, self.interval_ms = key, wait_s, next_ms, interval_ms


class BoundedSlots:
    """Spaced request slots (60/rpm) per key with a bounded wait, shared through Valkey.

    reserve() returns the seconds the caller must wait for its slot, or None when that
    wait would exceed max_wait_s (the slot is then not taken: the caller answers 429).
    Without Valkey, or when it fails, the same spacing is kept in this process.
    """

    def __init__(self, client: Any = None, prefix: str = "engine:gateway:rpm", clock=time.time) -> None:
        self._client = client
        self._prefix = prefix
        self._clock = clock
        self._local: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def reserve(self, key: str, rpm: int, max_wait_s: float) -> Optional[float]:
        r = await self.reserve_slot(key, rpm, max_wait_s)
        return None if r is None else r.wait_s

    async def reserve_slot(self, key: str, rpm: int, max_wait_s: float) -> Optional[Reservation]:
        """Like reserve(), keeping what release() needs if the caller gives up while waiting."""
        if rpm <= 0:
            return Reservation(key, 0.0, 0, 0)
        interval_ms = int(60_000 / rpm)
        now_ms = int(self._clock() * 1000)
        if self._client is not None:
            try:
                wait_ms = int(await self._client.eval(_RESERVE_BOUNDED, 1, f"{self._prefix}:{key}",
                                                      now_ms, interval_ms, int(max_wait_s * 1000)))
                if wait_ms < 0:
                    return None
                return Reservation(key, wait_ms / 1000, now_ms + wait_ms + interval_ms, interval_ms)
            except Exception as exc:  # Valkey down: keep limiting in this process
                logger.warning("gateway limiter: Valkey unavailable (%s); using in-process slots", exc)
        async with self._lock:
            slot = max(now_ms, self._local.get(key, 0))
            if slot - now_ms > max_wait_s * 1000:
                return None
            self._local[key] = slot + interval_ms
        return Reservation(key, (slot - now_ms) / 1000, slot + interval_ms, interval_ms)

    async def release(self, r: Reservation) -> bool:
        """Give back a slot its caller abandoned (client gone while waiting). Only the last
        reservation can be returned safely; an earlier one simply lapses."""
        if r.interval_ms <= 0:
            return False
        if self._client is not None:
            try:
                return bool(await self._client.eval(_RELEASE_TAIL, 1, f"{self._prefix}:{r.key}", r.next_ms, r.interval_ms))
            except Exception as exc:
                logger.warning("gateway limiter: release failed (%s)", exc)
        async with self._lock:
            if self._local.get(r.key) == r.next_ms:
                self._local[r.key] = r.next_ms - r.interval_ms
                return True
        return False


_TAKE_DAILY = """
local n = tonumber(redis.call('GET', KEYS[1]) or '0')
if n >= tonumber(ARGV[1]) then return -1 end
n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
return n
"""


class DailyCounter:
    """Requests per day per key (an account and a model on a given day T8),
    shared through Valkey; in this process when Valkey is down. take() counts one request and
    returns the new count, or None when the limit is already reached (nothing counted);
    give_back() returns one the caller did not use."""

    def __init__(self, client: Any = None, prefix: str = "engine:gateway:rpd") -> None:
        self._client = client
        self._prefix = prefix
        self._local: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def take(self, key: str, limit: int, ttl_s: int) -> Optional[int]:
        if limit <= 0:
            return 0
        if self._client is not None:
            try:
                n = int(await self._client.eval(_TAKE_DAILY, 1, f"{self._prefix}:{key}", limit, max(60, ttl_s)))
                return None if n < 0 else n
            except Exception as exc:  # Valkey down: count in this process
                logger.warning("gateway daily counter: Valkey unavailable (%s); counting in-process", exc)
        async with self._lock:
            n = self._local.get(key, 0)
            if n >= limit:
                return None
            self._local[key] = n + 1
            return n + 1

    async def give_back(self, key: str) -> None:
        if self._client is not None:
            try:
                await self._client.decr(f"{self._prefix}:{key}")
                return
            except Exception as exc:
                logger.warning("gateway daily counter: give back failed (%s)", exc)
        async with self._lock:
            if self._local.get(key, 0) > 0:
                self._local[key] -= 1
