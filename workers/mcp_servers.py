"""Generic MCP servers for the worker agents, declared by configuration (docs/mcp.md).

Two MCP servers are connected in code: an Ansible Controller's front (`aap_mcp_toolset`,
the aap-mcp-server toolsets) and the read-only
knowledge server (`knowledge_toolset`). They are the reference implementation. This module
applies the same pattern to any other MCP server, without code:

    ENGINE_MCP_SERVERS='[{"name": "tickets", "url": "http://tickets-mcp:8000/mcp",
                          "roles": ["documentalist"], "tools": ["search_tickets", "get_ticket"],
                          "auth": {"type": "bearer_env", "env": "TICKETS_MCP_TOKEN"}}]'
    (or ENGINE_MCP_SERVERS_FILE=/path/to/servers.json with the same list)

Each entry:
  name     identifier ([a-z][a-z0-9_]*), used in logs and as the optional tool prefix
  url      Streamable HTTP endpoint (http/https), usually on engine-agents-net
  roles    the worker roles that get these tools (workers/agent.py SPECS)
  tools    the allowlist: the only tools the model sees. Required and never "all": a tool the
           server adds later stays invisible until it is listed here
  auth     how the worker authenticates, never a secret in the configuration:
             {"type": "none"}
             {"type": "bearer_env", "env": "VAR"}        static token from the worker's environment
             {"type": "worker_token", "header": "..."}   the task's own token from the A2A metadata
                                                         (engine_worker_token), per request, as the
                                                         Controller MCP does; default header
                                                         Authorization: Bearer <token>
           bearer_env and worker_token may be combined: {"type": "bearer_env", "env": "VAR",
           "forward_worker_token": "X-Engine-Worker-Token"}
  prefix   optional: tools are renamed <prefix>_<tool> (two servers with the same tool names)
  note     optional: one line added to the role's instruction (what the tools are for)
  timeout  optional: seconds for the HTTP connection (default 10)

Guardrails are outside the model: the allowlist filters what is listed, the server checks the
token it receives, and the worker has no egress (a server it reaches must be on its networks).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

TOKEN_METADATA_KEY = "engine_worker_token"   # the same key as workers/agent.py (the Controller MCP)
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_TOOL = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_HEADER = re.compile(r"^[A-Za-z0-9-]{1,64}$")
AUTH_TYPES = ("none", "bearer_env", "worker_token")
_SECRET_KEYS = {"token", "password", "secret", "api_key", "bearer", "authorization"}


class McpConfigError(ValueError):
    """ENGINE_MCP_SERVERS is malformed: the worker refuses to start."""


@dataclass(frozen=True)
class McpServer:
    name: str
    url: str
    roles: tuple[str, ...]
    tools: tuple[str, ...]
    auth_type: str = "none"
    bearer_env: Optional[str] = None
    worker_token_header: Optional[str] = None       # where the task's token goes, if forwarded
    prefix: Optional[str] = None
    note: str = ""
    timeout: float = 10.0
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    def static_headers(self) -> dict[str, str]:
        if not self.bearer_env:
            return {}
        token = os.environ.get(self.bearer_env, "")
        if not token:
            raise McpConfigError(f"MCP server {self.name}: {self.bearer_env} is not set")
        return {"Authorization": f"Bearer {token}"}

    def header_provider(self):
        """ADK header_provider: the task's token from the A2A metadata, on every request."""
        header = self.worker_token_header
        if not header:
            return None

        def provide(ctx) -> dict[str, str]:
            inv = getattr(ctx, "_invocation_context", None) or ctx
            run_config = getattr(inv, "run_config", None)
            metadata = (getattr(run_config, "custom_metadata", None) or {}).get("a2a_metadata") or {}
            token = metadata.get(TOKEN_METADATA_KEY)
            if not isinstance(token, str) or not token:
                return {}
            return {header: f"Bearer {token}" if header.lower() == "authorization" else token}
        return provide


def _fail(name: str, why: str) -> McpConfigError:
    return McpConfigError(f"ENGINE_MCP_SERVERS: {name}: {why}")


def parse(raw: Any, valid_roles: frozenset[str] | set[str]) -> list[McpServer]:
    """The declared servers, validated; raises McpConfigError (fail closed at start-up)."""
    if raw in (None, "", []):
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise McpConfigError("ENGINE_MCP_SERVERS must be a JSON list") from exc
    if not isinstance(raw, list):
        raise McpConfigError("ENGINE_MCP_SERVERS must be a JSON list")
    servers, seen = [], set()
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise _fail(f"entry {i}", "must be an object")
        name = str(entry.get("name") or "")
        if not _NAME.match(name):
            raise _fail(f"entry {i}", "name must match [a-z][a-z0-9_]*")
        if name in seen:
            raise _fail(name, "declared twice")
        seen.add(name)
        secrets_inline = _SECRET_KEYS & {k.lower() for k in entry} | _SECRET_KEYS & {
            k.lower() for k in (entry.get("auth") or {}) if isinstance(entry.get("auth"), dict)}
        if secrets_inline:
            raise _fail(name, f"no secrets in the configuration ({', '.join(sorted(secrets_inline))}); "
                              "use auth.type bearer_env with the variable's name")
        url = str(entry.get("url") or "")
        if not url.startswith(("http://", "https://")) or any(c.isspace() for c in url):
            raise _fail(name, "url must be an http(s) Streamable HTTP endpoint")
        roles = entry.get("roles")
        if not isinstance(roles, list) or not roles or not all(isinstance(r, str) for r in roles):
            raise _fail(name, "roles must be a non-empty list")
        unknown = sorted(set(roles) - set(valid_roles))
        if unknown:
            raise _fail(name, f"unknown roles {unknown}; one of {sorted(valid_roles)}")
        tools = entry.get("tools")
        if not isinstance(tools, list) or not tools or not all(isinstance(t, str) and _TOOL.match(t) for t in tools):
            raise _fail(name, "tools must be a non-empty allowlist of tool names (never all tools)")
        auth = entry.get("auth") or {"type": "none"}
        if not isinstance(auth, dict) or auth.get("type") not in AUTH_TYPES:
            raise _fail(name, f"auth.type must be one of {', '.join(AUTH_TYPES)}")
        bearer_env = forward = None
        if auth["type"] == "bearer_env":
            bearer_env = str(auth.get("env") or "")
            if not re.match(r"^[A-Z_][A-Z0-9_]*$", bearer_env):
                raise _fail(name, "auth.env must name an environment variable")
            forward = auth.get("forward_worker_token")
        elif auth["type"] == "worker_token":
            forward = auth.get("header") or "Authorization"
        if forward is not None and (not isinstance(forward, str) or not _HEADER.match(forward)):
            raise _fail(name, "the worker token header must be an HTTP header name")
        if bearer_env and forward and forward.lower() == "authorization":
            raise _fail(name, "a static bearer already uses Authorization; forward the worker token in another header")
        prefix = entry.get("prefix")
        if prefix is not None and (not isinstance(prefix, str) or not _NAME.match(prefix)):
            raise _fail(name, "prefix must match [a-z][a-z0-9_]*")
        try:
            timeout = float(entry.get("timeout", 10))
        except (TypeError, ValueError) as exc:
            raise _fail(name, "timeout must be a number of seconds") from exc
        servers.append(McpServer(name=name, url=url.rstrip("/"), roles=tuple(dict.fromkeys(roles)),
                                 tools=tuple(dict.fromkeys(tools)), auth_type=auth["type"], bearer_env=bearer_env or None,
                                 worker_token_header=forward, prefix=prefix, note=str(entry.get("note") or "")[:500],
                                 timeout=timeout))
    return servers


def configured(valid_roles: frozenset[str] | set[str]) -> list[McpServer]:
    """ENGINE_MCP_SERVERS, or the JSON file named by ENGINE_MCP_SERVERS_FILE."""
    path = os.environ.get("ENGINE_MCP_SERVERS_FILE", "").strip()
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                return parse(fh.read(), valid_roles)
        except OSError as exc:
            raise McpConfigError(f"ENGINE_MCP_SERVERS_FILE {path}: {exc.strerror}") from exc
    return parse(os.environ.get("ENGINE_MCP_SERVERS", ""), valid_roles)


def toolset(server: McpServer):
    from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams  # type: ignore[import]
    from google.adk.tools.mcp_tool.mcp_toolset import McpToolset  # type: ignore[import]

    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(url=server.url, headers=server.static_headers() or None,
                                                         timeout=server.timeout),
        tool_filter=list(server.tools),
        tool_name_prefix=server.prefix,
        header_provider=server.header_provider(),
    )


def instruction_note(server: McpServer) -> str:
    names = [f"{server.prefix}_{t}" if server.prefix else t for t in server.tools]
    return (f"\nTools from the {server.name} MCP server are available: {', '.join(names)}."
            + (f" {server.note}" if server.note else "") + "\n")


def toolsets_for(role: str, valid_roles: frozenset[str] | set[str]) -> list[tuple[Any, str]]:
    """(toolset, instruction note) for every declared server this role uses."""
    return [(toolset(s), instruction_note(s)) for s in configured(valid_roles) if role in s.roles]


# ── Operations: python -m workers.mcp_servers check [--call TOOL --args JSON] ──────────────────────

async def _check(call: Optional[str], args: dict[str, Any], valid_roles) -> int:
    """Connect to every declared server and list what each role would see (and call one tool)."""
    from types import SimpleNamespace
    ctx = SimpleNamespace(_invocation_context=SimpleNamespace(run_config=SimpleNamespace(
        custom_metadata={"a2a_metadata": {}})))
    servers, failed = configured(valid_roles), 0
    if not servers:
        print("no MCP servers declared (ENGINE_MCP_SERVERS / ENGINE_MCP_SERVERS_FILE)")
    for server in servers:
        ts = toolset(server)
        try:
            tools = {t.name: t for t in await ts.get_tools(readonly_context=ctx)}
            missing = [t for t in server.tools if (f"{server.prefix}_{t}" if server.prefix else t) not in tools]
            print(f"[ok] {server.name} {server.url} → roles {', '.join(server.roles)}: {', '.join(sorted(tools)) or '-'}"
                  + (f"  (declared but not offered: {', '.join(missing)})" if missing else ""))
            if call and call in tools:
                print(f"     {call}: {await tools[call].run_async(args=args, tool_context=ctx)}")
        except Exception as exc:  # report every server, then fail
            failed += 1
            print(f"[FAIL] {server.name} {server.url}: {type(exc).__name__}: {str(exc)[:200]}")
        finally:
            await ts.close()
    return 1 if failed else 0


if __name__ == "__main__":
    import argparse
    import asyncio

    ap = argparse.ArgumentParser(prog="python -m workers.mcp_servers")
    ap.add_argument("command", choices=["check"])
    ap.add_argument("--call", help="a tool to call on the server that offers it")
    ap.add_argument("--args", default="{}", help="its arguments as JSON")
    a = ap.parse_args()
    from workers.agent import VALID_ROLES
    raise SystemExit(asyncio.run(_check(a.call, json.loads(a.args), VALID_ROLES)))
