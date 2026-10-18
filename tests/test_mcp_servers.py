"""Generic MCP servers for the workers (workers/mcp_servers.py, docs/mcp.md): the declaration is validated
and fails closed, and against a real MCP server (examples/mcp-example) the allowlist hides what is not
declared, the static bearer and the task's own token reach the server, and a missing token is refused."""

from __future__ import annotations

import asyncio
import json
import runpy
import socket
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from workers import mcp_servers
from workers.mcp_servers import McpConfigError

ROLES = frozenset({"executor", "validator", "documentalist", "author", "medic"})
SERVER = Path(__file__).resolve().parent.parent / "examples" / "mcp-example" / "server.py"
STATIC, TASK = "static-secret-1234", "task-token-9876"


def entry(**overrides):
    base = {"name": "example", "url": "http://mcp-example:8000/mcp", "roles": ["documentalist"],
            "tools": ["echo", "whoami"], "auth": {"type": "none"}}
    return {**base, **overrides}


def ctx(token=TASK):
    meta = {"vanguard_worker_token": token} if token else {}
    return SimpleNamespace(_invocation_context=SimpleNamespace(
        run_config=SimpleNamespace(custom_metadata={"a2a_metadata": meta})))


# ── declaration ──────────────────────────────────────────────────────────────

def test_a_valid_declaration_and_its_defaults():
    [s] = mcp_servers.parse(json.dumps([entry(prefix="ex", note="For tests.")]), ROLES)
    assert (s.name, s.url, s.roles, s.tools, s.auth_type, s.prefix) == (
        "example", "http://mcp-example:8000/mcp", ("documentalist",), ("echo", "whoami"), "none", "ex")
    assert mcp_servers.instruction_note(s).strip() == \
        "Tools from the example MCP server are available: ex_echo, ex_whoami. For tests."
    assert mcp_servers.parse("", ROLES) == [] and mcp_servers.parse(None, ROLES) == []


@pytest.mark.parametrize("bad, why", [
    (entry(tools=[]), "allowlist"),
    (entry(tools=None), "allowlist"),
    (entry(roles=["archon"]), "unknown roles"),
    (entry(roles=[]), "roles"),
    (entry(url="ftp://x"), "url"),
    (entry(name="Bad Name"), "name"),
    (entry(token="inline"), "no secrets"),
    (entry(auth={"type": "bearer_env", "env": "X", "token": "inline"}), "no secrets"),
    (entry(auth={"type": "basic"}), "auth.type"),
    (entry(auth={"type": "bearer_env", "env": "lower"}), "auth.env"),
    (entry(auth={"type": "bearer_env", "env": "TOK", "forward_worker_token": "Authorization"}), "Authorization"),
    (entry(auth={"type": "worker_token", "header": "bad header"}), "header name"),
])
def test_a_bad_declaration_stops_the_start(bad, why):
    with pytest.raises(McpConfigError, match=why):
        mcp_servers.parse([bad], ROLES)


def test_names_are_unique_and_the_input_is_a_list():
    with pytest.raises(McpConfigError, match="twice"):
        mcp_servers.parse([entry(), entry()], ROLES)
    for raw in ("{}", "not json", json.dumps({"name": "x"})):
        with pytest.raises(McpConfigError, match="JSON list"):
            mcp_servers.parse(raw, ROLES)


def test_a_missing_bearer_variable_is_refused(monkeypatch):
    monkeypatch.delenv("EXAMPLE_TOKEN", raising=False)
    [s] = mcp_servers.parse([entry(auth={"type": "bearer_env", "env": "EXAMPLE_TOKEN"})], ROLES)
    with pytest.raises(McpConfigError, match="EXAMPLE_TOKEN is not set"):
        s.static_headers()


def test_the_task_token_header():
    [fwd] = mcp_servers.parse([entry(auth={"type": "worker_token"})], ROLES)
    assert fwd.header_provider()(ctx()) == {"Authorization": f"Bearer {TASK}"}
    assert fwd.header_provider()(ctx(None)) == {}
    [own] = mcp_servers.parse([entry(auth={"type": "worker_token", "header": "X-Task-Token"})], ROLES)
    assert own.header_provider()(ctx()) == {"X-Task-Token": TASK}
    [none] = mcp_servers.parse([entry()], ROLES)
    assert none.header_provider() is None


def test_the_file_form_and_the_roles_that_get_the_tools(monkeypatch, tmp_path):
    f = tmp_path / "servers.json"
    f.write_text(json.dumps([entry()]))
    monkeypatch.setenv("ENGINE_MCP_SERVERS_FILE", str(f))
    assert len(mcp_servers.toolsets_for("documentalist", ROLES)) == 1
    assert mcp_servers.toolsets_for("executor", ROLES) == []
    monkeypatch.setenv("ENGINE_MCP_SERVERS_FILE", str(tmp_path / "missing.json"))
    with pytest.raises(McpConfigError, match="missing.json"):
        mcp_servers.configured(ROLES)


def test_the_agent_gets_the_declared_server(monkeypatch):
    monkeypatch.delenv("ENGINE_MCP_SERVERS_FILE", raising=False)
    monkeypatch.delenv("KNOWLEDGE_MCP_URL", raising=False)
    monkeypatch.setenv("ENGINE_MCP_SERVERS", json.dumps([entry()]))
    from workers.agent import make_worker_agent
    doc = make_worker_agent("documentalist")
    assert sum(type(t).__name__ == "McpToolset" for t in doc.tools) == 1     # the documentalist had none
    assert "Tools from the example MCP server" in doc.instruction(None)
    author = make_worker_agent("author")
    assert not any(type(t).__name__ == "McpToolset" for t in author.tools)
    monkeypatch.setenv("ENGINE_MCP_SERVERS", json.dumps([entry(tools=[])]))
    with pytest.raises(McpConfigError):
        make_worker_agent("documentalist")


# ── against a real MCP server ────────────────────────────────────────────────

@pytest.fixture(scope="module")
def example_server():
    import os

    import uvicorn
    previous = os.environ.get("EXAMPLE_MCP_TOKEN")
    os.environ["EXAMPLE_MCP_TOKEN"] = STATIC                 # read when the module loads
    try:
        mod = runpy.run_path(str(SERVER), run_name="example_mcp")
    finally:
        os.environ.pop("EXAMPLE_MCP_TOKEN") if previous is None else os.environ.update(EXAMPLE_MCP_TOKEN=previous)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(mod["app"](), host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(5)


def _toolset(url, auth):
    [s] = mcp_servers.parse([entry(url=url, auth=auth)], ROLES)
    return mcp_servers.toolset(s)


def _text(result) -> str:
    return "".join(c.get("text", "") for c in result.get("content", []))


def test_the_allowlist_and_both_tokens_reach_the_server(example_server, monkeypatch):
    monkeypatch.setenv("EXAMPLE_TOKEN", STATIC)

    async def run():
        ts = _toolset(example_server, {"type": "bearer_env", "env": "EXAMPLE_TOKEN",
                                       "forward_worker_token": "X-Engine-Worker-Token"})
        try:
            tools = {t.name: t for t in await ts.get_tools(readonly_context=ctx())}
            # the server also has utc_now and delete_everything: not declared, not seen
            assert sorted(tools) == ["echo", "whoami"]
            assert _text(await tools["echo"].run_async(args={"text": "hola"}, tool_context=ctx())) == "hola"
            who = json.loads(_text(await tools["whoami"].run_async(args={}, tool_context=ctx())))
            assert who == {"authorization": "present (…1234)", "x_engine_worker_token": "present (…9876)"}
            assert STATIC not in json.dumps(who) and TASK not in json.dumps(who)   # never the values
        finally:
            await ts.close()
    asyncio.run(run())


def test_without_the_token_the_server_refuses(example_server):
    async def run():
        ts = _toolset(example_server, {"type": "none"})
        try:
            with pytest.raises(Exception, match="401"):
                await ts.get_tools(readonly_context=ctx())
        finally:
            await ts.close()
    asyncio.run(run())
