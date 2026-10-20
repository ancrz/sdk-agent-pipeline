"""A minimal MCP server to prove a connection to the engine's workers (docs/mcp.md).

    python examples/mcp-example/server.py          # Streamable HTTP on 0.0.0.0:8000/mcp

Tools:
  echo(text)          returns the text: the simplest round trip
  utc_now()           the server's time: a tool without arguments
  whoami()            which credentials reached the server (presence and last 4 characters,
                      never the value): proves the static bearer and the task's own token
  delete_everything() a tool the worker must NOT see: declare it outside the allowlist and
                      the model never gets it (it does nothing anyway)

Environment:
  EXAMPLE_MCP_TOKEN          when set, every request needs Authorization: Bearer <it> (else 401)
  EXAMPLE_MCP_ALLOWED_HOSTS  Host headers accepted (DNS-rebinding protection), comma list,
                             default mcp-example:*,localhost:*,127.0.0.1:*
  EXAMPLE_MCP_PORT           default 8000
"""

from __future__ import annotations

import hmac
import os
from datetime import datetime, timezone

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings

PORT = int(os.environ.get("EXAMPLE_MCP_PORT", "8000"))
TOKEN = os.environ.get("EXAMPLE_MCP_TOKEN", "")
HOSTS = [h.strip() for h in os.environ.get("EXAMPLE_MCP_ALLOWED_HOSTS",
                                           "mcp-example:*,localhost:*,127.0.0.1:*").split(",") if h.strip()]

mcp = FastMCP("engine-example", host="0.0.0.0", port=PORT, stateless_http=True, json_response=True,
              transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                                           allowed_hosts=HOSTS, allowed_origins=[]))


def _seen(value: str | None) -> str:
    return f"present (…{value[-4:]})" if value else "absent"


@mcp.tool()
def echo(text: str) -> str:
    """Return the text unchanged."""
    return text


@mcp.tool()
def utc_now() -> str:
    """The server's current time in UTC, ISO 8601."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@mcp.tool()
def whoami(ctx: Context) -> dict:
    """Which credentials reached this server (never their values)."""
    request = getattr(ctx.request_context, "request", None)
    headers = request.headers if request is not None else {}
    auth = headers.get("authorization", "")
    return {"authorization": _seen(auth.removeprefix("Bearer ").strip() or None),
            "x_engine_worker_token": _seen(headers.get("x-engine-worker-token"))}


@mcp.tool()
def delete_everything() -> str:
    """Never exposed by a correct allowlist."""
    return "this tool should not have been reachable"


def app():
    """The Streamable HTTP app, behind a bearer check when EXAMPLE_MCP_TOKEN is set."""
    inner = mcp.streamable_http_app()
    if not TOKEN:
        return inner

    async def guarded(scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope.get("headers") or []).get(b"authorization", b"").decode()
            if not hmac.compare_digest(auth, f"Bearer {TOKEN}"):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error": "unauthorized"}'})
                return
        await inner(scope, receive, send)
    return guarded


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app(), host="0.0.0.0", port=PORT, log_level="info")
