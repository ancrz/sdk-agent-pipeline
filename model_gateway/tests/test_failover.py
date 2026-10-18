"""Resilience: every failure case of the catalogue (docs/pipeline/resilience_*.md) keeps a model answering."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from model_gateway import failover

from .test_gateway import BODY, EMB_TOKEN, EXEC_TOKEN, calls, client_for, env, sf  # noqa: F401  (fixtures)

MAIN, BACKUP = "AIzaMAIN-key-0000000000000000001111", "AIzaBACKUP-key-00000000000000002222"
OK = {"candidates": [{"content": {"role": "model", "parts": [{"text": "OK"}]}}],
      "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 1}}


@pytest.fixture(autouse=True)
def admin_token(monkeypatch):
    monkeypatch.setenv("VANGUARD_GATEWAY_ADMIN_TOKEN", "a" * 32)


def gemini_error(code: int, status: str, message: str, details=None) -> httpx.Response:
    return httpx.Response(code, json={"error": {"code": code, "status": status, "message": message, "details": details or []}})


class Keys:
    """A Gemini endpoint that answers per API key (and per model)."""

    def __init__(self, answers: dict[str, object]):
        self.answers, self.calls = answers, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        key = request.headers.get("x-goog-api-key", "")
        who = "main" if key == MAIN else "backup" if key == BACKUP else request.url.host
        self.calls.append(who)
        if request.url.host == "ollama":
            return httpx.Response(200, json={"model": "gemma4:e4b", "choices": [{"finish_reason": "stop",
                                  "message": {"content": "local"}}], "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
        answer = self.answers.get(who, httpx.Response(200, json=OK))
        if isinstance(answer, Exception):
            raise answer
        return answer() if callable(answer) else answer


async def setup(sf, backups=("gemini-backup",), local=False, last_resort=True, emb=False):  # noqa: F811
    from store.models.automation import ModelProfile
    from store.services import credentials as svc
    from store.services import failover as fo
    from store.services import presets
    async with sf() as db:
        await presets.seed_presets(db)
        main = await svc.store(db, "gemini-main", "model_provider", {"provider": "gemini", "api_key": MAIN, "tier": "paid"}, "t")
        await svc.store(db, "gemini-backup", "model_provider", {"provider": "gemini", "api_key": BACKUP, "tier": "paid"}, "t")
        if local:
            await svc.store(db, "local-gemma", "model_provider", {"provider": "local", "base_url": "http://ollama:11434/v1"}, "t")
            await presets.set_local_fallback(db, "local-gemma", "gemma4:e4b", True, "t")
        await db.flush()
        db.add(ModelProfile(preset="custom", role="executor", credential_id=main.id, model="m1", active=True, updated_by="t"))
        if emb:
            db.add(ModelProfile(preset="custom", role="embeddings", credential_id=main.id, model="gemini-embedding-001",
                                active=True, updated_by="t"))
        await presets.activate(db, "custom", "t")
        await fo.set_config(db, [{"credential": b, "model": None} for b in backups], last_resort, "t")
        await db.commit()


async def call(c, token=EXEC_TOKEN, method="generateContent", body=BODY):
    return await c.post(f"/v1beta/models/x:{method}", json=body, headers={"x-goog-api-key": token})


# ── Classification ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status,body,kind", [
    (429, {"error": {"message": "Resource exhausted", "details": [{"retryDelay": "17s"}]}}, "rate"),
    (429, {"error": {"details": [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}]}}, "quota_daily"),
    (429, {"error": {"code": "insufficient_quota", "message": "You exceeded your current quota"}}, "billing"),
    (402, {"error": {"message": "Insufficient Balance"}}, "billing"),
    (400, {"error": {"message": "Your credit balance is too low"}}, "billing"),
    (401, {}, "auth"), (403, {"error": {"status": "PERMISSION_DENIED"}}, "auth"),
    (400, {"error": {"message": "API key not valid. Please pass a valid API key.", "details": [{"reason": "API_KEY_INVALID"}]}}, "auth"),
    (404, {}, "model_missing"), (500, {}, "outage"), (503, {}, "outage"), (529, {}, "outage"),
    (400, {"error": {"message": "Request contains an invalid argument."}}, "client"),
])
def test_classification(status, body, kind):
    assert failover.classify(status, body).kind == kind


def test_transport_errors_and_retry_hints():
    req = httpx.Request("POST", "https://x")
    assert failover.classify(None, exc=httpx.ConnectError("dns", request=req)).kind == "network"
    assert failover.classify(None, exc=httpx.ConnectTimeout("t", request=req)).kind == "network"
    assert failover.classify(None, exc=httpx.ReadTimeout("t", request=req)).kind == "timeout"
    f = failover.classify(429, {"error": {"details": [{"retryDelay": "17s"}]}})
    assert failover.scope_and_cooldown("rate", f, "a", "gemini", "m") == ("account:a:m", 17.0)


def test_daily_quota_waits_for_midnight_pacific():
    tz = ZoneInfo("America/Los_Angeles")
    assert 3590 <= failover.next_reset("gemini", datetime(2026, 10, 1, 23, 0, tzinfo=tz)) <= 3610
    assert failover.next_reset("openai") == 3600


# ── The chain ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_invalid_key_fails_over_to_the_backup_and_is_skipped_afterwards(sf):  # noqa: F811
    await setup(sf)
    up = Keys({"main": gemini_error(400, "INVALID_ARGUMENT", "API key not valid.", [{"reason": "API_KEY_INVALID"}])})
    async with client_for(sf, up) as c:
        first, second = await call(c), await call(c)
        breakers = (await c.get("/admin/breakers", headers={"Authorization": "Bearer " + "a" * 32})).json()
    assert first.status_code == second.status_code == 200
    assert up.calls == ["main", "backup", "backup"]          # the broken key is not paid for twice
    rows = await calls(sf)
    assert rows[0].fallback_used and "key invalid" in rows[0].error
    assert any(b["scope"] == "account:gemini-main" and b["kind"] == "auth" for b in breakers)


@pytest.mark.asyncio
async def test_daily_quota_moves_to_the_backup_until_the_reset(sf):  # noqa: F811
    await setup(sf)
    up = Keys({"main": gemini_error(429, "RESOURCE_EXHAUSTED", "Quota exceeded",
                                    [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]}])})
    async with client_for(sf, up) as c:
        assert (await call(c)).status_code == 200 and (await call(c)).status_code == 200
        b = (await c.get("/admin/breakers", headers={"Authorization": "Bearer " + "a" * 32})).json()
    assert up.calls == ["main", "backup", "backup"]
    (quota,) = [x for x in b if x["kind"] == "quota_daily"]
    assert quota["scope"] == "account:gemini-main:m1" and quota["seconds_left"] > 60


@pytest.mark.asyncio
async def test_a_reset_breaker_tries_the_route_again(sf, monkeypatch):  # noqa: F811
    monkeypatch.setenv("VANGUARD_GATEWAY_ADMIN_TOKEN", "a" * 32)
    await setup(sf)
    up = Keys({"main": httpx.Response(401, json={})})
    async with client_for(sf, up) as c:
        await call(c)
        r = await c.delete("/admin/breakers", headers={"Authorization": "Bearer " + "a" * 32})
        assert r.json()["reset"] >= 1
        await call(c)
    assert up.calls == ["main", "backup", "main", "backup"]


@pytest.mark.asyncio
async def test_our_rpm_limit_uses_another_account_never_the_stand_in(sf, monkeypatch):  # noqa: F811
    monkeypatch.setenv("VANGUARD_GATEWAY_MAX_WAIT_S", "0")
    await setup(sf)
    from store.models.automation import ModelProfile
    from sqlalchemy import update
    async with sf() as db:
        await db.execute(update(ModelProfile).where(ModelProfile.role == "executor").values(rpm=1))
        await db.commit()
    up = Keys({})
    async with client_for(sf, up) as c:
        assert (await call(c)).status_code == 200 and (await call(c)).status_code == 200
    assert up.calls == ["main", "backup"]                    # the backup's own quota, not a downgrade
    await setup_no_backup_rpm(sf)


async def setup_no_backup_rpm(sf):  # noqa: F811
    """Without another account, our own limit is a 429 (the stand-in is for outages, not throttling)."""
    from store.services import failover as fo
    async with sf() as db:
        await fo.set_config(db, [], True, "t")
        await db.commit()
    async with client_for(sf, Keys({})) as c:
        assert (await call(c)).status_code == 200
        r = await call(c)
    assert r.status_code == 429 and "requests per minute" in r.json()["error"]["message"]


@pytest.mark.asyncio
async def test_no_internet_goes_straight_to_the_local_model(sf):  # noqa: F811
    await setup(sf, local=True)
    dns = httpx.ConnectError("Temporary failure in name resolution")
    up = Keys({"main": dns, "backup": dns, "generativelanguage.googleapis.com": dns})

    async with client_for(sf, up) as c:
        r1 = await call(c)
        assert r1.status_code == 200 and r1.json()["candidates"][0]["content"]["parts"][0]["text"] == "local"
        up.calls.clear()
        r2 = await call(c)
    assert r2.status_code == 200 and up.calls == ["ollama"]     # the cloud is skipped while it is down


@pytest.mark.asyncio
async def test_the_internet_is_down_when_two_providers_cannot_be_reached(sf):  # noqa: F811
    b = failover.Breakers()
    assert await b.network_failed("gemini") is False
    assert await b.network_failed("openai") is True
    assert (await b.get("egress"))["kind"] == "network"


@pytest.mark.asyncio
async def test_a_provider_outage_is_skipped_for_its_cooldown(sf):  # noqa: F811
    await setup(sf, backups=(), local=True)
    up = Keys({"main": gemini_error(503, "UNAVAILABLE", "The model is overloaded.")})
    async with client_for(sf, up) as c:
        await call(c)
        up.calls.clear()
        await call(c)
    assert up.calls == ["ollama"]


@pytest.mark.asyncio
async def test_a_bad_request_is_not_retried_elsewhere(sf):  # noqa: F811
    await setup(sf, local=True)
    up = Keys({"main": gemini_error(400, "INVALID_ARGUMENT", "Request contains an invalid argument.")})
    async with client_for(sf, up) as c:
        r = await call(c)
    assert r.status_code == 400 and up.calls == ["main"]


@pytest.mark.asyncio
async def test_the_daily_budget_allows_only_the_free_local_model(sf, monkeypatch):  # noqa: F811
    monkeypatch.setenv("VANGUARD_GATEWAY_DAILY_TOKENS", "3")
    await setup(sf, local=True)
    up = Keys({})
    async with client_for(sf, up) as c:
        await call(c)                                       # 4 tokens: the budget is spent
        r = await call(c)
    assert r.status_code == 200 and up.calls == ["main", "ollama"]


@pytest.mark.asyncio
async def test_embeddings_fail_over_only_to_the_same_model(sf):  # noqa: F811
    await setup(sf, local=True, emb=True)
    seen = []

    def handler(request):
        seen.append((request.headers.get("x-goog-api-key"), request.url.path))
        if request.headers.get("x-goog-api-key") == MAIN:
            return httpx.Response(401, json={})
        return httpx.Response(200, json={"embedding": {"values": [0.1]}})

    async with client_for(sf, handler) as c:
        r = await call(c, token=EMB_TOKEN, method="embedContent", body={"content": {"parts": [{"text": "x"}]}})
    assert r.status_code == 200
    assert seen == [(MAIN, "/v1beta/models/gemini-embedding-001:embedContent"),
                    (BACKUP, "/v1beta/models/gemini-embedding-001:embedContent")]


@pytest.mark.asyncio
async def test_a_stream_fails_over_before_its_first_byte(sf):  # noqa: F811
    await setup(sf)
    up = Keys({"main": lambda: httpx.Response(401, json={}),
               "backup": lambda: httpx.Response(200, content=b'data: {"candidates": []}\n\n',
                                                headers={"content-type": "text/event-stream"})})
    async with client_for(sf, up) as c:
        r = await call(c, method="streamGenerateContent")
    assert r.status_code == 200 and r.text.startswith("data:") and up.calls == ["main", "backup"]


@pytest.mark.asyncio
async def test_routing_survives_a_database_outage(sf, monkeypatch):  # noqa: F811
    await setup(sf)
    import model_gateway.app as gw
    real = gw.failover_chain
    state = {"down": False}

    async def chain(db, role, **kw):
        if state["down"]:
            raise ConnectionError("postgres is gone")
        return await real(db, role, **kw)

    monkeypatch.setattr(gw, "failover_chain", chain)
    up = Keys({})
    async with client_for(sf, up) as c:
        assert (await call(c)).status_code == 200
        state["down"] = True
        assert (await call(c)).status_code == 200             # last known routes
    assert up.calls == ["main", "main"]


@pytest.mark.asyncio
async def test_the_local_model_answers_without_thinking_and_with_the_schema(sf):  # noqa: F811
    """A CPU-served model is asked not to think (VANGUARD_LOCAL_REASONING, default none) and gets
    the answer's JSON Schema (docs/pipeline/audit_*); cloud calls carry neither change."""
    await setup(sf, local=True)
    dns = httpx.ConnectError("Temporary failure in name resolution")
    sent = []

    class Capture(Keys):
        def __call__(self, request):
            if request.url.host == "ollama":
                sent.append(json.loads(request.content))
            return super().__call__(request)

    up = Capture({"main": dns, "backup": dns, "generativelanguage.googleapis.com": dns})
    schema = {"type": "object", "properties": {"title": {"type": "string"}}}
    body = {**BODY, "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": schema}}
    async with client_for(sf, up) as c:
        assert (await call(c, body=body)).status_code == 200
    assert sent and sent[0]["reasoning_effort"] == "none"
    assert sent[0]["response_format"] == {"type": "json_schema", "json_schema": {"name": "response", "schema": schema}}


@pytest.mark.asyncio
async def test_a_local_model_that_announces_instead_of_calling_is_asked_once_more(sf):  # noqa: F811
    await setup(sf, local=True)
    dns = httpx.ConnectError("Temporary failure in name resolution")
    sent = []

    class Announcer(Keys):
        def __call__(self, request):
            if request.url.host != "ollama":
                return super().__call__(request)
            body = json.loads(request.content)
            sent.append(body)
            if len(sent) == 1:
                msg = {"content": "I will now locate the job template and launch it."}
            else:
                msg = {"content": None, "tool_calls": [{"id": "c1", "type": "function",
                                                        "function": {"name": "job_templates_list", "arguments": "{}"}}]}
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": msg}],
                                             "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

    up = Announcer({"main": dns, "backup": dns, "generativelanguage.googleapis.com": dns})
    body = {**BODY, "tools": [{"functionDeclarations": [{"name": "job_templates_list", "parameters": {"type": "OBJECT"}}]}]}
    async with client_for(sf, up) as c:
        r = await call(c, body=body)
    assert r.status_code == 200 and len(sent) == 2
    assert sent[1]["messages"][-1]["content"].startswith("Do it now")
    assert r.json()["candidates"][0]["content"]["parts"][0]["functionCall"]["name"] == "job_templates_list"



@pytest.mark.asyncio
async def test_the_executor_on_the_local_tier_is_the_deterministic_procedure(sf, monkeypatch):  # noqa: F811
    """Measured (docs/pipeline/audit_*): small models do not launch templates; on the local tier the
    executor and validator are answered by the deterministic procedure, and Ollama is not called."""
    monkeypatch.setattr("model_gateway.app.LOCAL_DETERMINISTIC_ROLES", frozenset({"executor", "validator"}))
    await setup(sf, local=True)
    dns = httpx.ConnectError("Temporary failure in name resolution")
    up = Keys({"main": dns, "backup": dns, "generativelanguage.googleapis.com": dns})
    async with client_for(sf, up) as c:
        r = await call(c)
    assert r.status_code == 200 and "ollama" not in up.calls
    assert r.json()["modelVersion"] == "deterministic-executor"
