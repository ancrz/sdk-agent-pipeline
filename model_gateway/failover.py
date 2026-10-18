"""Why a route failed, and how long to stop trying it (circuit breakers).

classify() turns a provider answer or a transport error into a failure kind; each
kind opens a breaker on the narrowest scope that failed, for a cooldown, so the
next calls skip the broken route at once instead of paying its timeout again:

  kind            scope                 cooldown                        examples
  rate            account:model         Retry-After / RetryInfo, ≥10s   429 per minute
  quota_daily     account:model         until the provider's reset      429 GenerateRequestsPerDay…
  billing         account               1 h                             402, insufficient_quota, credit balance
  auth            account               1 h                             401, 403, 400 API_KEY_INVALID
  model_missing   account:model         1 h                             404 (retired for this key)
  outage          provider:model        30 s                            500 502 503 504 529
  timeout         provider:model        30 s                            read timeout
  network         provider              30 s                            DNS, refused, connect timeout
  (two providers network-failing within a minute → "egress": the internet is down;
   every cloud route is skipped for 30 s and calls go straight to the local model)

Breakers live in Valkey (shared by gateway instances) and fall back to process
memory when Valkey is down. Admins see and reset them (AI Gateway → Resilience).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import httpx

logger = logging.getLogger("model_gateway.failover")

PREFIX = "vg:cb:"
FAILOVER_KINDS = {"rate", "quota_daily", "billing", "auth", "model_missing", "outage", "timeout", "network"}
OUTAGE_CODES = {500, 502, 503, 504, 529}


def _s(name: str, default: float) -> float:
    return float(os.environ.get(name, str(default)))


@dataclass
class Failure:
    kind: str                   # one of FAILOVER_KINDS, or "client" (no failover)
    detail: str
    retry_after: Optional[float] = None


def _retry_info(body: Any, headers: Optional[httpx.Headers]) -> Optional[float]:
    if headers is not None and headers.get("retry-after", "").strip().isdigit():
        return float(headers["retry-after"])
    for d in ((body or {}).get("error") or {}).get("details") or [] if isinstance(body, dict) else []:
        delay = isinstance(d, dict) and d.get("retryDelay")
        if isinstance(delay, str) and re.fullmatch(r"\d+(\.\d+)?s", delay):
            return float(delay[:-1])
    return None


def _text(body: Any) -> str:
    try:
        return json.dumps(body)[:4000].lower() if body is not None else ""
    except (TypeError, ValueError):
        return ""


def classify(status: Optional[int], body: Any = None, headers: Optional[httpx.Headers] = None,
             exc: Optional[BaseException] = None) -> Failure:
    """The failure kind of one attempt (status None = a transport error in exc)."""
    if exc is not None:
        if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
            return Failure("network", f"unreachable: {type(exc).__name__}")
        if isinstance(exc, httpx.TimeoutException):
            return Failure("timeout", f"timed out: {type(exc).__name__}")
        return Failure("network", f"unreachable: {type(exc).__name__}")
    text = _text(body)
    if status == 429:
        if "insufficient_quota" in text or "billing" in text:
            return Failure("billing", "429 billing/credit exhausted")
        if "perday" in text or "per day" in text or "daily" in text:
            return Failure("quota_daily", "429 daily quota exhausted")
        return Failure("rate", "429 rate limited", _retry_info(body, headers))
    if status == 402 or (status == 400 and "credit balance" in text):
        return Failure("billing", f"{status} billing/credit exhausted")
    if status in (401, 403) or (status == 400 and ("api_key_invalid" in text or "api key not valid" in text
                                                   or "api key expired" in text)):
        return Failure("auth", f"{status} key invalid, revoked or not allowed")
    if status == 404:
        return Failure("model_missing", "404 model not available for this key")
    if status in OUTAGE_CODES or (status is not None and status >= 500):
        return Failure("outage", f"{status} provider error")
    if status == 408:
        return Failure("timeout", "408 provider timeout")
    return Failure("client", f"{status}")


def next_reset(provider: str, now: Optional[datetime] = None) -> float:
    """Seconds until the provider's daily quota resets. Gemini API: midnight Pacific time."""
    if provider in ("gemini", "vertex_ai"):
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(os.environ.get("VANGUARD_QUOTA_RESET_TZ", "America/Los_Angeles"))
        local = (now or datetime.now(tz)).astimezone(tz)
        midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
        return max(60.0, (midnight - local).total_seconds())
    return _s("VANGUARD_FAILOVER_QUOTA_COOLDOWN_S", 3600)


def quota_day(provider: str, now: Optional[datetime] = None) -> str:
    """The provider's quota day (Gemini API: the date in Pacific time), for daily counters."""
    if provider in ("gemini", "vertex_ai"):
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(os.environ.get("VANGUARD_QUOTA_RESET_TZ", "America/Los_Angeles"))
        return (now or datetime.now(tz)).astimezone(tz).date().isoformat()
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()


def scope_and_cooldown(kind: str, failure: Failure, account: str, provider: str, model: str) -> tuple[str, float]:
    if kind == "rate":
        return f"account:{account}:{model}", max(10.0, failure.retry_after or _s("VANGUARD_FAILOVER_RATE_COOLDOWN_S", 60))
    if kind == "quota_daily":
        return f"account:{account}:{model}", next_reset(provider)
    if kind == "billing":
        return f"account:{account}", _s("VANGUARD_FAILOVER_BILLING_COOLDOWN_S", 3600)
    if kind == "auth":
        return f"account:{account}", _s("VANGUARD_FAILOVER_AUTH_COOLDOWN_S", 3600)
    if kind == "model_missing":
        return f"account:{account}:{model}", _s("VANGUARD_FAILOVER_MODEL_COOLDOWN_S", 3600)
    if kind in ("outage", "timeout"):
        return f"provider:{provider}:{model}", _s("VANGUARD_FAILOVER_OUTAGE_COOLDOWN_S", 30)
    return f"provider:{provider}", _s("VANGUARD_FAILOVER_NETWORK_COOLDOWN_S", 30)


class Breakers:
    """Open circuits with a reason and an end time; Valkey first, process memory as fallback."""

    def __init__(self, valkey: Any = None, clock=time.time) -> None:
        self.valkey, self.clock = valkey, clock
        self.local: dict[str, dict[str, Any]] = {}
        self.network_failures: dict[str, float] = {}      # provider → last network failure (egress detection)

    async def open(self, scope: str, kind: str, detail: str, seconds: float) -> None:
        entry = {"scope": scope, "kind": kind, "detail": detail[:300], "until": self.clock() + seconds}
        self.local[scope] = entry
        if self.valkey is not None:
            try:
                await self.valkey.set(PREFIX + scope, json.dumps(entry), ex=max(1, int(seconds)))
            except Exception as exc:  # Valkey down: process memory keeps the breaker
                logger.warning("breaker %s kept in memory only: %s", scope, type(exc).__name__)

    async def get(self, scope: str) -> Optional[dict[str, Any]]:
        if self.valkey is not None:
            try:
                raw = await self.valkey.get(PREFIX + scope)
                if raw:
                    return json.loads(raw)
            except Exception:
                pass
        entry = self.local.get(scope)
        if entry and entry["until"] > self.clock():
            return entry
        self.local.pop(scope, None)
        return None

    async def blocking(self, scopes: list[str]) -> Optional[dict[str, Any]]:
        for scope in scopes:
            entry = await self.get(scope)
            if entry:
                return entry
        return None

    async def all(self) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        if self.valkey is not None:
            try:
                async for key in self.valkey.scan_iter(match=PREFIX + "*"):
                    raw = await self.valkey.get(key)
                    if raw:
                        entry = json.loads(raw)
                        found[entry["scope"]] = entry
            except Exception:
                pass
        now = self.clock()
        for scope, entry in list(self.local.items()):
            if entry["until"] > now:
                found.setdefault(scope, entry)
        return sorted(found.values(), key=lambda e: e["scope"])

    async def reset(self, scope: Optional[str] = None, prefix: Optional[str] = None) -> int:
        """Close one circuit, those starting with `prefix` (a key stored again), or all."""
        if prefix:
            scopes = [e["scope"] for e in await self.all() if e["scope"] == prefix or e["scope"].startswith(prefix + ":")]
        else:
            scopes = [scope] if scope else [e["scope"] for e in await self.all()]
        for s in scopes:
            self.local.pop(s, None)
            if self.valkey is not None:
                try:
                    await self.valkey.delete(PREFIX + s)
                except Exception:
                    pass
        if scope is None and prefix is None:
            self.network_failures.clear()
        return len(scopes)

    async def network_failed(self, provider: str) -> bool:
        """Record a network failure; True when it means the internet is down (two providers in a minute)."""
        now = self.clock()
        self.network_failures[provider] = now
        recent = {p for p, t in self.network_failures.items() if now - t < 60}
        if len(recent) >= 2:
            await self.open("egress", "network", f"no internet: {', '.join(sorted(recent))} unreachable",
                            _s("VANGUARD_FAILOVER_NETWORK_COOLDOWN_S", 30))
            return True
        return False


def scopes_for(account: str, provider: str, model: str, cloud: bool) -> list[str]:
    scopes = [f"account:{account}", f"account:{account}:{model}", f"provider:{provider}", f"provider:{provider}:{model}"]
    return (["egress"] + scopes) if cloud else scopes


class Serving:
    """Who answers each role now (docs/pipeline/continuity_*): `tier:account`, shared by every
    gateway replica in Valkey. Notices go out on transitions only, and `SET … GET` is atomic, so
    with two replicas exactly one of them sees each change. Valkey down: process memory, and
    calls are never affected."""

    KEY, SINCE = "gw:serving:", "gw:serving-since:"
    RESET = "reset"

    def __init__(self, valkey: Any = None, clock=time.time) -> None:
        self.valkey, self.clock = valkey, clock
        self.local: dict[str, tuple[str, float]] = {}

    async def changed(self, role: str, state: str) -> tuple[bool, Optional[str]]:
        """Record `state` for `role`; (changed, previous)."""
        now = self.clock()
        if self.valkey is not None:
            try:
                raw = await self.valkey.set(self.KEY + role, state, get=True)
                previous = raw.decode() if isinstance(raw, bytes) else raw
                if previous != state:
                    await self.valkey.set(self.SINCE + role, str(now))
                self.local[role] = (state, now if previous != state else self.local.get(role, (state, now))[1])
                return previous != state, previous
            except Exception as exc:
                logger.warning("serving state for %s kept in memory only: %s", role, type(exc).__name__)
        previous = self.local.get(role, (None, now))[0]
        if previous != state:
            self.local[role] = (state, now)
        return previous != state, previous

    async def get(self, role: str) -> Optional[str]:
        if self.valkey is not None:
            try:
                raw = await self.valkey.get(self.KEY + role)
                return raw.decode() if isinstance(raw, bytes) else raw
            except Exception:
                pass
        return self.local.get(role, (None, 0))[0]

    async def all(self) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {r: {"state": s, "since": t} for r, (s, t) in self.local.items()}
        if self.valkey is not None:
            try:
                async for key in self.valkey.scan_iter(match=self.KEY + "*"):
                    key = key.decode() if isinstance(key, bytes) else key
                    role = key[len(self.KEY):]
                    raw, since = await self.valkey.get(key), await self.valkey.get(self.SINCE + role)
                    found[role] = {"state": raw.decode() if isinstance(raw, bytes) else raw,
                                   "since": float(since) if since else None}
            except Exception:
                pass
        return found

    async def reset(self) -> int:
        """"Return to primary": the next answer from the primary is a recovery (and with
        failback manual, the backup stops going first)."""
        roles = list(await self.all())
        for role in roles:
            self.local[role] = (self.RESET, self.clock())
            if self.valkey is not None:
                try:
                    await self.valkey.set(self.KEY + role, self.RESET)
                except Exception:
                    pass
        return len(roles)
