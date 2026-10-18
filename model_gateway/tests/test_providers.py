"""Remediation R2: presets switch providers without restarts; offline and OpenAI-compatible adapters."""

from __future__ import annotations

import json
import os

os.environ.setdefault("VANGUARD_GATEWAY_AUTOSTART", "0")

import httpx
import pytest

from model_gateway import providers

from .test_gateway import BODY, EXEC_TOKEN, calls, client_for, env, sf  # noqa: F401  (fixtures)

TOOLS_BODY = {
    "systemInstruction": {"parts": [{"text": "You are the executor."}]},
    "contents": [
        {"role": "user", "parts": [{"text": "Run health_check on t1"}]},
        {"role": "model", "parts": [{"functionCall": {"id": "c1", "name": "job_templates_list", "args": {"search": "hc"}}}]},
        {"role": "user", "parts": [{"functionResponse": {"id": "c1", "name": "job_templates_list", "response": {"results": []}}}]},
    ],
    "tools": [{"functionDeclarations": [{"name": "job_templates_list", "description": "List templates",
                                         "parameters": {"type": "OBJECT", "properties": {
                                             "search": {"type": "STRING", "nullable": True}}, "required": []}}]}],
    "generationConfig": {"temperature": 0.1, "responseMimeType": "application/json"},
}


def test_gemini_request_translates_to_openai_messages_and_tools():
    req = providers.gemini_to_openai(TOOLS_BODY, "gpt-x")
    roles = [m["role"] for m in req["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]
    assert req["messages"][2]["tool_calls"][0] == {"id": "c1", "type": "function", "function": {
        "name": "job_templates_list", "arguments": json.dumps({"search": "hc"})}}
    assert req["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": json.dumps({"results": []})}
    params = req["tools"][0]["function"]["parameters"]
    assert params == {"type": "object", "properties": {"search": {"type": ["string", "null"]}}, "required": []}
    assert req["response_format"] == {"type": "json_object"} and req["temperature"] == 0.1


def test_calls_without_ids_are_paired_in_order():
    body = {"contents": [
        {"role": "model", "parts": [{"functionCall": {"name": "f", "args": {}}}, {"functionCall": {"name": "f", "args": {"n": 2}}}]},
        {"role": "user", "parts": [{"functionResponse": {"name": "f", "response": {"r": 1}}},
                                   {"functionResponse": {"name": "f", "response": {"r": 2}}}]}]}
    msgs = providers.gemini_to_openai(body, "m")["messages"]
    ids = [c["id"] for c in msgs[0]["tool_calls"]]
    assert [m["tool_call_id"] for m in msgs[1:]] == ids


def test_openai_response_translates_back_to_gemini():
    out = providers.openai_to_gemini({"model": "gpt-x", "choices": [{"finish_reason": "tool_calls", "message": {
        "content": None, "tool_calls": [{"id": "k", "function": {"name": "jobs_retrieve", "arguments": "{\"id\": 7}"}}]}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14}})
    part = out["candidates"][0]["content"]["parts"][0]
    assert part == {"functionCall": {"id": "k", "name": "jobs_retrieve", "args": {"id": 7}}}
    assert out["usageMetadata"]["promptTokenCount"] == 11 and out["candidates"][0]["finishReason"] == "STOP"


async def _set_preset(sf, name, profiles=()):
    from store.models.automation import ModelProfile
    from store.services import presets
    async with sf() as db:
        await presets.seed_presets(db)
        await presets.activate(db, name, "t")
        for p in profiles:
            db.add(p)
        await db.commit()


@pytest.mark.asyncio
async def test_offline_preset_answers_without_any_provider_call(sf):
    await _set_preset(sf, "offline")
    up = []
    async with client_for(sf, lambda r: up.append(r) or httpx.Response(500)) as c:
        r = await c.post("/v1beta/models/whatever:generateContent", headers={"x-goog-api-key": EXEC_TOKEN}, json={
            "contents": [{"role": "user", "parts": [{"text": "Run the job template `health_check` on `t1`."}]}]})
    assert r.status_code == 200 and up == []                                   # nothing left the gateway
    part = r.json()["candidates"][0]["content"]["parts"][0]
    assert part["functionCall"] == {"name": "job_templates_list", "args": {"search": "health_check"}}
    (row,) = await calls(sf)
    assert (row.provider, row.model) == ("offline", "deterministic-executor")


@pytest.mark.asyncio
async def test_switching_the_preset_changes_the_next_call(sf, monkeypatch):
    from store.models.automation import ModelProfile
    from store.services import credentials as svc
    from store.services import presets

    async with sf() as db:
        cred = await svc.store(db, "oai", "model_provider", {"provider": "openai", "api_key": "sk-test-000", "tier": "paid"}, "t")
        await db.flush()
        await presets.seed_presets(db)
        db.add(ModelProfile(preset="paid", role="executor", credential_id=cred.id, model="gpt-x", active=True, updated_by="t"))
        await presets.activate(db, "paid", "t")
        await db.commit()
    seen = []

    def openai(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"model": "gpt-x", "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
                                         "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6}})

    async with client_for(sf, openai) as c:
        r = await c.post("/v1beta/models/x:generateContent", json=BODY, headers={"x-goog-api-key": EXEC_TOKEN})
        assert r.status_code == 200 and r.json()["candidates"][0]["content"]["parts"][0]["text"] == "ok"
        assert str(seen[0].url) == "https://api.openai.com/v1/chat/completions"
        assert seen[0].headers["authorization"] == "Bearer sk-test-000" and json.loads(seen[0].content)["model"] == "gpt-x"
        async with sf() as db:                                                 # switch, no restart
            await presets.activate(db, "offline", "t")
            await db.commit()
        r = await c.post("/v1beta/models/x:generateContent", json=BODY, headers={"x-goog-api-key": EXEC_TOKEN})
        assert r.status_code == 200 and len(seen) == 1
    rows = await calls(sf)
    assert [(x.provider, x.prompt_tokens) for x in rows] == [("openai", 5), ("offline", 0)]


@pytest.mark.asyncio
async def test_model_listing_needs_the_admin_token(sf, monkeypatch):
    from store.services import credentials as svc
    monkeypatch.setenv("VANGUARD_GATEWAY_ADMIN_TOKEN", "a" * 32)
    async with sf() as db:
        await svc.store(db, "gem", "model_provider", {"provider": "gemini", "api_key": "AIza-k"}, "t")
        await db.commit()

    def gemini(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "AIza-k"
        return httpx.Response(200, json={"models": [
            {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent", "countTokens"]},
            {"name": "models/gemini-embedding-001", "supportedGenerationMethods": ["embedContent"]}]})

    async with client_for(sf, gemini) as c:
        assert (await c.get("/admin/credentials/gem/models")).status_code == 401
        assert (await c.get("/admin/credentials/gem/models", headers={"Authorization": "Bearer " + "b" * 32})).status_code == 401
        r = await c.get("/admin/credentials/gem/models", headers={"Authorization": "Bearer " + "a" * 32})
    assert r.json() == {"provider": "gemini", "models": [
        {"name": "gemini-3.5-flash-lite", "generate": True, "embed": False},
        {"name": "gemini-embedding-001", "generate": False, "embed": True}]}


async def _local_setup(sf, enabled=True):
    from store.services import credentials as svc
    from store.services import presets
    async with sf() as db:
        await svc.store(db, "local-gemma", "model_provider",
                        {"provider": "local", "base_url": "http://ollama:11434/v1"}, "t")
        await presets.seed_presets(db)
        await presets.activate(db, "custom", "t")
        await presets.set_local_fallback(db, "local-gemma", "gemma4:e4b", enabled, "t")
        await db.commit()


def _cloud_down_local_up(seen, cloud_status=None):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "ollama":
            assert "authorization" not in request.headers                      # no key for a local server
            assert json.loads(request.content)["model"] == "gemma4:e4b"
            return httpx.Response(200, json={"model": "gemma4:e4b", "choices": [{"finish_reason": "stop",
                                  "message": {"content": "local ok"}}], "usage": {"prompt_tokens": 4, "completion_tokens": 2}})
        if cloud_status:
            return httpx.Response(cloud_status, json={"error": {"code": cloud_status, "message": "down"}})
        raise httpx.ConnectError("Temporary failure in name resolution")      # no internet
    return handler


@pytest.mark.asyncio
async def test_no_internet_falls_back_to_the_local_model(sf):
    await _local_setup(sf)
    seen = []
    async with client_for(sf, _cloud_down_local_up(seen)) as c:
        r = await c.post("/v1beta/models/x:generateContent", json=BODY, headers={"x-goog-api-key": EXEC_TOKEN})
    assert r.status_code == 200 and r.json()["candidates"][0]["content"]["parts"][0]["text"] == "local ok"
    assert seen[-1] == "http://ollama:11434/v1/chat/completions"
    (row,) = await calls(sf)
    assert (row.provider, row.model, row.fallback_used) == ("local", "gemma4:e4b", True)
    assert "unreachable" in row.error


@pytest.mark.asyncio
async def test_provider_outage_503_also_falls_back(sf):
    await _local_setup(sf)
    seen = []
    async with client_for(sf, _cloud_down_local_up(seen, cloud_status=503)) as c:
        r = await c.post("/v1beta/models/x:generateContent", json=BODY, headers={"x-goog-api-key": EXEC_TOKEN})
    assert r.status_code == 200 and seen[-1].startswith("http://ollama:11434")


@pytest.mark.asyncio
async def test_without_a_fallback_the_outage_is_a_clean_audited_503(sf):
    await _local_setup(sf, enabled=False)
    from store.services import failover as fo
    async with sf() as db:                                       # no last resort either
        await fo.set_config(db, [], last_resort=False, actor="t")
        await db.commit()
    async with client_for(sf, _cloud_down_local_up([])) as c:
        r = await c.post("/v1beta/models/x:generateContent", json=BODY, headers={"x-goog-api-key": EXEC_TOKEN})
    assert r.status_code == 503 and r.json()["error"]["status"] == "UNAVAILABLE"
    (row,) = await calls(sf)
    assert row.status_code == 503 and "unreachable" in row.error


@pytest.mark.asyncio
async def test_the_local_preset_serves_every_role_without_internet(sf):
    from store.models.automation import Credential, ModelProfile
    from store.services import presets
    from sqlalchemy import select
    await _local_setup(sf)
    async with sf() as db:
        cred = (await db.execute(select(Credential).where(Credential.name == "local-gemma"))).scalar_one()
        db.add(ModelProfile(preset="local", role="default", credential_id=cred.id, model="gemma4:e4b", active=True, updated_by="t"))
        await presets.activate(db, "local", "t")
        await db.commit()
    seen = []
    async with client_for(sf, _cloud_down_local_up(seen)) as c:
        r = await c.post("/v1beta/models/x:generateContent", json=BODY, headers={"x-goog-api-key": EXEC_TOKEN})
    assert r.status_code == 200 and seen == ["http://ollama:11434/v1/chat/completions"]   # never tried the cloud
    (row,) = await calls(sf)
    assert (row.provider, row.fallback_used) == ("local", False)


def test_gemini_request_translates_to_native_anthropic_messages():
    req = providers.gemini_to_anthropic(TOOLS_BODY, "claude-x")
    assert req["model"] == "claude-x" and req["max_tokens"] == 4096 and req["temperature"] == 0.1
    assert req["system"].startswith("You are the executor.") and "JSON object" in req["system"]   # JSON mode
    roles = [m["role"] for m in req["messages"]]
    assert roles == ["user", "assistant", "user"]                                  # alternating turns
    use = req["messages"][1]["content"][0]
    assert use == {"type": "tool_use", "id": "c1", "name": "job_templates_list", "input": {"search": "hc"}}
    result = req["messages"][2]["content"][0]
    assert result == {"type": "tool_result", "tool_use_id": "c1", "content": json.dumps({"results": []})}
    assert req["tools"][0]["input_schema"]["properties"]["search"]["type"] == ["string", "null"]


def test_anthropic_ids_are_made_valid_and_paired():
    body = {"contents": [
        {"role": "model", "parts": [{"functionCall": {"id": "adk-9f/x", "name": "f", "args": {}}}]},
        {"role": "user", "parts": [{"functionResponse": {"id": "adk-9f/x", "name": "f", "response": {}}}]}]}
    msgs = providers.gemini_to_anthropic(body, "m")["messages"]
    tid = msgs[0]["content"][0]["id"]
    assert tid.startswith("toolu_") and msgs[1]["content"][0]["tool_use_id"] == tid


def test_anthropic_response_translates_back():
    out = providers.anthropic_to_gemini({"model": "claude-x", "stop_reason": "tool_use", "content": [
        {"type": "text", "text": "Listing"}, {"type": "tool_use", "id": "toolu_1", "name": "jobs_retrieve", "input": {"id": 7}}],
        "usage": {"input_tokens": 12, "output_tokens": 4}})
    parts = out["candidates"][0]["content"]["parts"]
    assert parts == [{"text": "Listing"}, {"functionCall": {"id": "toolu_1", "name": "jobs_retrieve", "args": {"id": 7}}}]
    assert out["usageMetadata"] == {"promptTokenCount": 12, "candidatesTokenCount": 4, "totalTokenCount": 16}


@pytest.mark.asyncio
async def test_anthropic_calls_go_to_the_messages_api(sf):
    from store.models.automation import ModelProfile
    from store.services import credentials as svc
    from store.services import presets

    async with sf() as db:
        cred = await svc.store(db, "claude", "model_provider", {"provider": "anthropic", "api_key": "sk-ant-test", "tier": "paid"}, "t")
        await db.flush()
        await presets.seed_presets(db)
        db.add(ModelProfile(preset="paid", role="executor", credential_id=cred.id, model="claude-x", active=True, updated_by="t"))
        await presets.activate(db, "paid", "t")
        await db.commit()
    seen = []

    def anthropic(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"model": "claude-x", "stop_reason": "end_turn",
                                         "content": [{"type": "text", "text": "ok"}], "usage": {"input_tokens": 3, "output_tokens": 1}})

    async with client_for(sf, anthropic) as c:
        r = await c.post("/v1beta/models/x:generateContent", json=BODY, headers={"x-goog-api-key": EXEC_TOKEN})
    assert r.status_code == 200 and r.json()["candidates"][0]["content"]["parts"][0]["text"] == "ok"
    assert str(seen[0].url) == "https://api.anthropic.com/v1/messages"
    assert seen[0].headers["x-api-key"] == "sk-ant-test" and seen[0].headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in seen[0].headers
    (row,) = await calls(sf)
    assert (row.provider, row.prompt_tokens, row.output_tokens) == ("anthropic", 3, 1)


@pytest.mark.asyncio
async def test_with_nothing_else_left_the_stand_in_keeps_the_core_answering(sf):
    """No internet and no local model: the deterministic stand-in (last resort) answers."""
    await _local_setup(sf, enabled=False)
    async with client_for(sf, _cloud_down_local_up([])) as c:
        r = await c.post("/v1beta/models/x:generateContent", json=BODY, headers={"x-goog-api-key": EXEC_TOKEN})
    assert r.status_code == 200
    (row,) = await calls(sf)
    assert (row.provider, row.fallback_used) == ("offline", True) and "unreachable" in row.error


def test_a_response_schema_reaches_openai_compatible_and_local_servers():
    """The planner asks for its Plan Graph's JSON Schema: a local model must get it, or it
    guesses the shape (docs/pipeline/audit_*)."""
    plan = {"type": "object", "required": ["title"], "properties": {"title": {"type": "string"}},
            "$defs": {"Node": {"type": "object"}}}
    body = {"contents": [{"role": "user", "parts": [{"text": "plan"}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseJsonSchema": plan}}
    assert providers.gemini_to_openai(body, "m")["response_format"] == {
        "type": "json_schema", "json_schema": {"name": "response", "schema": plan}}
    gemini_typed = {"contents": body["contents"], "generationConfig": {
        "responseMimeType": "application/json", "responseSchema": {"type": "OBJECT", "properties": {"n": {"type": "INTEGER"}}}}}
    assert providers.gemini_to_openai(gemini_typed, "m")["response_format"]["json_schema"]["schema"] == {
        "type": "object", "properties": {"n": {"type": "integer"}}}
    system = providers.gemini_to_anthropic(body, "claude-x")["system"]
    assert '"required": ["title"]' in system


def test_an_announcement_without_a_call_is_recognised_only_when_tools_were_offered():
    tools = {"tools": [{"type": "function", "function": {"name": "f"}}]}
    said = lambda text, calls=None: {"choices": [{"message": {"content": text, "tool_calls": calls}}]}  # noqa: E731
    assert providers.announces_without_acting(tools, said("I will now locate the job template and launch it."))
    assert providers.announces_without_acting(tools, said("Let me search for the playbook first."))
    assert not providers.announces_without_acting({}, said("Let me search."))                     # no tools offered
    assert not providers.announces_without_acting(tools, said("Let me…", [{"id": "c"}]))           # it did call
    assert not providers.announces_without_acting(tools, said("Done. VERDICT: FIXED removed the pin"))
    assert not providers.announces_without_acting(tools, said("The port answers; the check passed."))
