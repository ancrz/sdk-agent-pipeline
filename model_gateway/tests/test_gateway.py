"""Model gateway: role from token, role's model enforced, real credential injected, fallback, audit."""

from __future__ import annotations

import json
import os

os.environ.setdefault("VANGUARD_GATEWAY_AUTOSTART", "0")

import httpx
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

EXEC_TOKEN = "gw-executor-" + "x" * 20
EMB_TOKEN = "gw-embeddings-" + "y" * 20
PROVIDER_KEY = "AIzaSyREAL-provider-key-000000000000wxyz"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("VANGUARD_GATEWAY_TOKEN_EXECUTOR", EXEC_TOKEN)
    monkeypatch.setenv("VANGUARD_GATEWAY_TOKEN_EMBEDDINGS", EMB_TOKEN)
    monkeypatch.setenv("GOOGLE_API_KEY", PROVIDER_KEY)
    monkeypatch.setenv("VANGUARD_DEFAULT_MODEL", "model-default")
    monkeypatch.delenv("VANGUARD_MODEL_EXECUTOR", raising=False)
    monkeypatch.delenv("VANGUARD_FALLBACK_MODEL", raising=False)
    monkeypatch.setenv("VANGUARD_CREDENTIALS_KEY", Fernet.generate_key().decode())


@pytest_asyncio.fixture
async def sf(tmp_path):
    import store.models  # noqa: F401  (registers every table)
    from store.models.activity import Base
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'gw.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


class Upstream:
    """Fake provider: records requests, answers by model name."""

    def __init__(self, status_by_model=None):
        self.calls: list[httpx.Request] = []
        self.status_by_model = status_by_model or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        model = request.url.path.rsplit("/", 1)[-1].split(":")[0]
        status = self.status_by_model.get(model, 200)
        if status != 200:
            return httpx.Response(status, json={"error": {"code": status, "message": f"{model} unavailable"}})
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "OK"}]}}],
                                         "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 2}})


def client_for(sf, upstream):
    from model_gateway.app import create_app
    app = create_app(session_factory=sf, transport=httpx.MockTransport(upstream))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gw")


BODY = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}


async def calls(sf):
    from store.models.automation import ModelCall
    async with sf() as db:
        return (await db.execute(select(ModelCall).order_by(ModelCall.created_at))).scalars().all()


@pytest.mark.asyncio
async def test_unknown_token_is_rejected(sf):
    up = Upstream()
    async with client_for(sf, up) as c:
        r = await c.post("/v1beta/models/x:generateContent", json=BODY, headers={"x-goog-api-key": "nope"})
        assert r.status_code == 401
        r = await c.post("/v1beta/models/x:deleteEverything", json=BODY, headers={"x-goog-api-key": EXEC_TOKEN})
        assert r.status_code == 404
    assert up.calls == []


@pytest.mark.asyncio
async def test_role_model_is_enforced_and_the_provider_key_injected(sf):
    up = Upstream()
    async with client_for(sf, up) as c:
        r = await c.post("/v1beta/models/gemini-ultra-expensive:generateContent", json=BODY,
                         headers={"x-goog-api-key": EXEC_TOKEN})
    assert r.status_code == 200 and r.json()["candidates"][0]["content"]["parts"][0]["text"] == "OK"
    sent = up.calls[0]
    assert sent.url.path == "/v1beta/models/model-default:generateContent"   # not the requested model
    assert sent.headers["x-goog-api-key"] == PROVIDER_KEY
    assert EXEC_TOKEN not in str(sent.headers) and EXEC_TOKEN not in str(sent.url)
    assert json.loads(sent.content) == BODY
    (row,) = await calls(sf)
    assert (row.role, row.requested_model, row.model, row.status_code, row.prompt_tokens, row.output_tokens) == (
        "executor", "gemini-ultra-expensive", "model-default", 200, 7, 2)


@pytest.mark.asyncio
async def test_vault_profile_credential_and_fallback(sf, monkeypatch):
    from store.models.automation import ModelProfile
    from store.services import credentials as svc
    async with sf() as db:
        cred = await svc.store(db, "gemini-main", "model_provider", {"provider": "gemini", "api_key": "AIzaVAULT-key-1111"}, "t")
        await db.flush()
        db.add(ModelProfile(preset="custom", role="executor", credential_id=cred.id, model="primary", fallback_model="backup",
                            active=True, updated_by="t"))
        await db.commit()
    up = Upstream({"primary": 503})
    async with client_for(sf, up) as c:
        r = await c.post("/v1beta/models/whatever:generateContent?key=" + EXEC_TOKEN, json=BODY)
    assert r.status_code == 200
    assert [u.url.path for u in up.calls] == ["/v1beta/models/primary:generateContent",
                                              "/v1beta/models/backup:generateContent"]
    assert all(u.headers["x-goog-api-key"] == "AIzaVAULT-key-1111" for u in up.calls)
    assert all("key" not in u.url.params for u in up.calls)     # the worker token never goes upstream
    (row,) = await calls(sf)
    assert row.model == "backup" and row.fallback_used is True


@pytest.mark.asyncio
async def test_embeddings_use_the_embeddings_model(sf):
    up = Upstream()
    async with client_for(sf, up) as c:
        r = await c.post("/v1beta/models/anything:embedContent", json={"content": {"parts": [{"text": "x"}]}},
                         headers={"x-goog-api-key": EMB_TOKEN})
    assert r.status_code == 200
    assert up.calls[0].url.path == "/v1beta/models/gemini-embedding-001:embedContent"


@pytest.mark.asyncio
async def test_errors_are_passed_through_and_audited_without_bodies(sf):
    up = Upstream({"model-default": 400})
    async with client_for(sf, up) as c:
        r = await c.post("/v1beta/models/m:generateContent", json={"contents": [{"parts": [{"text": "secret prompt"}]}]},
                         headers={"x-goog-api-key": EXEC_TOKEN})
    assert r.status_code == 400
    (row,) = await calls(sf)
    assert row.status_code == 400 and "secret prompt" not in (row.error or "")


@pytest.mark.asyncio
async def test_rpm_limit_refuses_with_429_and_audits(sf, monkeypatch):
    monkeypatch.setenv("VANGUARD_GATEWAY_RPM_EXECUTOR", "2")       # one slot every 30 s
    monkeypatch.setenv("VANGUARD_GATEWAY_MAX_WAIT_S", "0")          # no queueing: refuse at once
    up = Upstream()
    async with client_for(sf, up) as c:
        codes = [(await c.post("/v1beta/models/m:generateContent", json=BODY,
                               headers={"x-goog-api-key": EXEC_TOKEN})) for _ in range(2)]
    assert [r.status_code for r in codes] == [200, 429]
    assert codes[1].json()["error"]["status"] == "RESOURCE_EXHAUSTED" and int(codes[1].headers["Retry-After"]) >= 1
    assert len(up.calls) == 1                      # the refused call never reached the provider
    rows = await calls(sf)
    assert [r.status_code for r in rows] == [200, 429] and "requests per minute" in rows[1].error


@pytest.mark.asyncio
async def test_slots_space_requests_wait_within_the_bound_and_share_an_account():
    from orchestrator.ratelimit import BoundedSlots
    now = [1000.0]
    slots = BoundedSlots(clock=lambda: now[0])
    assert await slots.reserve("acct:m", 6, 30) == 0.0          # interval 10 s
    assert await slots.reserve("acct:m", 6, 30) == 10.0         # queued within the bound
    assert await slots.reserve("acct:m", 6, 30) == 20.0
    assert await slots.reserve("acct:m", 6, 15) is None         # 30 s > 15 s: refused, slot not taken
    assert await slots.reserve("acct:other", 6, 0) == 0.0       # another account/model: its own slots
    assert await slots.reserve("acct:m", 0, 0) == 0.0           # 0 = unlimited


@pytest.mark.asyncio
async def test_slots_fall_back_in_process_when_valkey_fails():
    from orchestrator.ratelimit import BoundedSlots

    class Down:
        async def eval(self, *a):
            raise ConnectionError("valkey down")

    slots = BoundedSlots(Down(), clock=lambda: 50.0)
    assert await slots.reserve("k", 60, 5) == 0.0 and await slots.reserve("k", 60, 5) == 1.0


def test_tier_defaults_apply_when_nothing_else_is_set(monkeypatch):
    from store.services.models import ResolvedModel, effective_rpm
    for name in ("VANGUARD_GATEWAY_RPM_EXECUTOR", "VANGUARD_GATEWAY_RPM", "VANGUARD_GATEWAY_RPM_FREE", "VANGUARD_GATEWAY_RPM_PAID"):
        monkeypatch.delenv(name, raising=False)
    assert effective_rpm(ResolvedModel(role="executor", model="m", tier="free")) == 10
    assert effective_rpm(ResolvedModel(role="executor", model="m", tier="paid")) == 0
    assert effective_rpm(ResolvedModel(role="executor", model="m", tier="free", rpm=3)) == 3     # the profile wins
    monkeypatch.setenv("VANGUARD_GATEWAY_RPM_FREE", "4")
    assert effective_rpm(ResolvedModel(role="executor", model="m", tier="free")) == 4
    assert effective_rpm(ResolvedModel(role="executor", model="m", provider="offline", tier="free")) == 0


@pytest.mark.asyncio
async def test_daily_token_budget(sf, monkeypatch):
    monkeypatch.setenv("VANGUARD_GATEWAY_DAILY_TOKENS", "10")     # each fake call uses 7 + 2 tokens
    up = Upstream()
    async with client_for(sf, up) as c:
        first = await c.post("/v1beta/models/m:generateContent", json=BODY, headers={"x-goog-api-key": EXEC_TOKEN})
        second = await c.post("/v1beta/models/m:generateContent", json=BODY, headers={"x-goog-api-key": EXEC_TOKEN})
        third = await c.post("/v1beta/models/m:generateContent", json=BODY, headers={"x-goog-api-key": EXEC_TOKEN})
    assert (first.status_code, second.status_code, third.status_code) == (200, 200, 429)
    assert "daily token budget" in third.json()["error"]["message"] and len(up.calls) == 2


@pytest.mark.asyncio
async def test_an_abandoned_tail_reservation_is_given_back():
    from orchestrator.ratelimit import BoundedSlots
    now = [100.0]
    slots = BoundedSlots(clock=lambda: now[0])
    first = await slots.reserve_slot("k", 6, 30)                  # at once
    second = await slots.reserve_slot("k", 6, 30)                 # queued 10 s
    third = await slots.reserve_slot("k", 6, 30)                  # queued 20 s
    assert (first.wait_s, second.wait_s, third.wait_s) == (0.0, 10.0, 20.0)
    assert not await slots.release(second)                        # not the last: it lapses
    assert await slots.release(third)                             # the tail comes back
    assert (await slots.reserve_slot("k", 6, 30)).wait_s == 20.0  # the next caller takes it


@pytest.mark.asyncio
async def test_release_works_through_valkey_semantics():
    from orchestrator.ratelimit import BoundedSlots

    class FakeValkey:
        """Implements the two Lua scripts' logic on a dict (what Valkey would run)."""
        def __init__(self):
            self.v = {}

        async def eval(self, script, n, key, *args):
            nxt = self.v.get(key, 0)
            if "max_wait" in script:
                now, interval, max_wait = map(int, args)
                slot = max(now, nxt)
                if slot - now > max_wait:
                    return -1
                self.v[key] = slot + interval
                return slot - now
            tail, interval = map(int, args)
            if nxt == tail:
                self.v[key] = tail - interval
                return 1
            return 0

    slots = BoundedSlots(FakeValkey(), clock=lambda: 10.0)
    a = await slots.reserve_slot("k", 60, 5)
    b = await slots.reserve_slot("k", 60, 5)
    assert b.wait_s == 1.0 and await slots.release(b) and (await slots.reserve_slot("k", 60, 5)).wait_s == 1.0


@pytest.mark.asyncio
async def test_a_free_account_is_skipped_before_the_providers_daily_429(sf, monkeypatch):
    """cost_* T8: requests per day per account:model, counted by the gateway."""
    from store.models.automation import ModelProfile
    from store.services import credentials as svc
    monkeypatch.setenv("VANGUARD_GATEWAY_RPD_FREE", "2")
    monkeypatch.setenv("VANGUARD_GATEWAY_RPM_EXECUTOR", "0")
    async with sf() as db:
        cred = await svc.store(db, "gemini-free", "model_provider",
                               {"provider": "gemini", "api_key": "AIzaFREE-key-2222", "tier": "free"}, "t")
        await db.flush()
        db.add(ModelProfile(preset="custom", role="executor", credential_id=cred.id, model="flash", active=True, updated_by="t"))
        await db.commit()
    up = Upstream()
    async with client_for(sf, up) as c:
        codes = [(await c.post("/v1beta/models/x:generateContent", json=BODY, headers={"x-goog-api-key": EXEC_TOKEN}))
                 for _ in range(3)]
    assert [r.status_code for r in codes] == [200, 200, 429]
    assert "requests per day" in codes[2].json()["error"]["message"] and len(up.calls) == 2
    assert int(codes[2].headers["Retry-After"]) >= 60


@pytest.mark.asyncio
async def test_the_daily_counter_counts_gives_back_and_survives_valkey_down():
    from orchestrator.ratelimit import DailyCounter

    class Down:
        async def eval(self, *a):
            raise ConnectionError("down")

        async def decr(self, *a):
            raise ConnectionError("down")

    for counter in (DailyCounter(), DailyCounter(Down())):
        assert [await counter.take("a:m:d", 2, 100) for _ in range(3)] == [1, 2, None]
        await counter.give_back("a:m:d")
        assert await counter.take("a:m:d", 2, 100) == 2
        assert await counter.take("other", 0, 100) == 0          # no limit, nothing counted


def test_rpd_is_per_tier_and_off_by_default(monkeypatch):
    from store.services.models import ResolvedModel, effective_rpd
    from model_gateway.failover import quota_day
    from datetime import datetime, timezone
    for name in ("VANGUARD_GATEWAY_RPD_FREE", "VANGUARD_GATEWAY_RPD_PAID"):
        monkeypatch.delenv(name, raising=False)
    assert effective_rpd(ResolvedModel(role="executor", model="m", tier="free")) == 0
    monkeypatch.setenv("VANGUARD_GATEWAY_RPD_FREE", "1000")
    assert effective_rpd(ResolvedModel(role="executor", model="m", tier="free")) == 1000
    assert effective_rpd(ResolvedModel(role="executor", model="m", tier="paid")) == 0
    assert effective_rpd(ResolvedModel(role="executor", model="m", provider="local", tier="free")) == 0
    late = datetime(2026, 10, 10, 6, 30, tzinfo=timezone.utc)          # 23:30 the day before in Pacific time
    assert quota_day("gemini", late) == "2026-10-09" and quota_day("openai", late) == "2026-10-10"
