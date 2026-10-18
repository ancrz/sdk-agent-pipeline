"""Worker entrypoint — python -m workers.entrypoint.

Reads AGENT_ROLES (or AGENT_ROLE), builds one ADK agent per role from its spec
(workers/agent.py SPECS) and serves them over A2A with uvicorn. One role is served at
the root (a per-role container); several share one process, each under /<role>/
(G-AGENTS A4, docs/pipeline/evolution_20261015_200000.md).

Env vars consumed:
  AGENT_ROLES         comma list of roles served by this process (the generic worker)
  AGENT_ROLE          one role, served at the root (used when AGENT_ROLES is empty)
  VANGUARD_GATEWAY_TOKEN_<ROLE>  each role's gateway token, set per model request (generic worker)
  POSTGRES_HOST/PORT/DB/USER/PASSWORD  Postgres connection (a2a_tasks + adk schema)
  DATABASE_URL        Override the assembled Postgres URL (optional)
  VALKEY_HOST/PORT/PASSWORD  Valkey connection for RPM limiter
  VANGUARD_MODEL_{ROLE}  Gemini model override per role  (default: VANGUARD_DEFAULT_MODEL, orchestrator/models.py)
  VANGUARD_RPM_LIMIT / VANGUARD_RPM_FLASH / VANGUARD_RPM_PRO  RPM budget

NOT consumed (must not be present in worker environments):
  JWT_SECRET_KEY, ADMIN_*, CORS_*  (API-only secrets; never passed to workers)
"""

from __future__ import annotations

import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("workers.entrypoint")

# ─────────────────────────────────────────────────────────────────────────────
# Role validation (fail fast before any heavy import)
# ─────────────────────────────────────────────────────────────────────────────

_VALID_ROLES = {"executor", "validator", "documentalist", "author", "medic"}


def parse_roles(roles: str, role: str) -> list[str]:
    """AGENT_ROLES (comma list) or else AGENT_ROLE; ordered, without repeats. Raises ValueError."""
    names = [r.strip().lower() for r in (roles or role or "").split(",") if r.strip()]
    if not names:
        raise ValueError("AGENT_ROLES (or AGENT_ROLE) is required but not set.")
    unknown = sorted(set(names) - _VALID_ROLES)
    if unknown:
        raise ValueError(f"Unknown roles {unknown}; each must be one of {sorted(_VALID_ROLES)}")
    return list(dict.fromkeys(names))


def router(apps: dict, roles: list[str]):
    """ASGI app: /<role>/… goes to that role's A2A app (its routes carry the prefix); /healthz lists the roles.

    The lifespan starts every role's app (ADK attaches the A2A routes in it)."""
    import json
    from contextlib import AsyncExitStack

    async def app(scope, receive, send):
        if scope["type"] == "lifespan":
            async with AsyncExitStack() as stack:
                message = await receive()                       # lifespan.startup
                try:
                    for sub in apps.values():
                        await stack.enter_async_context(sub.router.lifespan_context(sub))
                except Exception as exc:
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
                    return
                await send({"type": "lifespan.startup.complete"})
                message = await receive()                       # lifespan.shutdown
                assert message["type"] == "lifespan.shutdown"
            await send({"type": "lifespan.shutdown.complete"})
            return
        path = scope.get("path", "")
        if scope["type"] == "http" and path == "/healthz":
            body = json.dumps({"roles": roles}).encode()
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        sub = apps.get(path.strip("/").split("/", 1)[0])
        if sub is None:
            await send({"type": "http.response.start", "status": 404, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"Not Found"})
            return
        await sub(scope, receive, send)
    return app


def _build_database_url() -> str:
    host = os.environ.get("POSTGRES_HOST", "postgres")
    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "vanguard")
    user = os.environ.get("POSTGRES_USER", "vanguard")
    password = os.environ.get("POSTGRES_PASSWORD", "vanguard")
    return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"


async def _main() -> None:
    import uvicorn
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from a2a.server.tasks import DatabaseTaskStore  # type: ignore[import]
    from google.adk.a2a.utils.agent_to_a2a import to_a2a  # type: ignore[import]
    from google.adk.artifacts.in_memory_artifact_service import InMemoryArtifactService  # type: ignore[import]
    from google.adk.runners import Runner  # type: ignore[import]
    from google.adk.sessions.database_session_service import DatabaseSessionService  # type: ignore[import]

    from orchestrator.ratelimit import ValkeyRpmLimiter, set_rpm_limiter
    from workers.agent import make_worker_agent

    database_url = os.environ.get("DATABASE_URL") or _build_database_url()
    logger.info("Worker roles=%s, connecting to DB", ",".join(roles))

    engine = create_async_engine(database_url, echo=False, future=True, pool_pre_ping=True)

    # DatabaseTaskStore: table already created by Alembic migration 004.
    # create_table=False prevents race conditions on startup; table_name must
    # match the name in the migration (a2a_tasks).
    task_store = DatabaseTaskStore(engine, create_table=False, table_name="a2a_tasks")

    # DatabaseSessionService: ADK sessions live in schema `adk` (created by
    # migration 004). search_path ensures ADK's session tables are found there.
    session_service = DatabaseSessionService(
        database_url,
        connect_args={"server_settings": {"search_path": "adk"}},
    )

    # Valkey RPM limiter (shared budget across all worker replicas)
    valkey_host = os.environ.get("VALKEY_HOST", "valkey")
    valkey_port = int(os.environ.get("VALKEY_PORT", "6379"))
    valkey_password = os.environ.get("VALKEY_PASSWORD") or None
    try:
        import valkey.asyncio as valkey_client_lib
        vc = valkey_client_lib.Valkey(host=valkey_host, port=valkey_port, password=valkey_password)
        set_rpm_limiter(ValkeyRpmLimiter(vc))
        logger.info("Valkey RPM limiter enabled (host=%s:%s)", valkey_host, valkey_port)
    except Exception as exc:
        logger.warning("Valkey not available; using in-process limiter: %s", exc)

    # The host passed to to_a2a becomes the URL in the AgentCard that A2A
    # clients use for RPC calls. Use the Docker Compose service name so the
    # card advertises a routable hostname. Uvicorn still binds to 0.0.0.0.
    # AGENT_PUBLIC_HOST overrides for non-Compose deployments.
    port = int(os.environ.get("WORKER_PORT", "8080"))
    single = len(roles) == 1
    public_host = os.environ.get("AGENT_PUBLIC_HOST", f"worker-{roles[0]}" if single else "worker-agents")
    apps = {}
    for role in roles:
        agent = make_worker_agent(role)
        runner = Runner(
            app_name=agent.name,
            agent=agent,
            artifact_service=InMemoryArtifactService(),
            session_service=session_service,
        )
        # one role: at the root, as before; several: each under /<role>/ (its card advertises that URL)
        apps[role] = to_a2a(agent, host=public_host, port=port, task_store=task_store, runner=runner,
                            rpc_path="" if single else role)
    app = apps[roles[0]] if single else router(apps, roles)

    logger.info("Starting worker vanguard %s on 0.0.0.0:%d (card host: %s)", ",".join(roles), port, public_host)
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    import asyncio
    try:
        roles = parse_roles(os.environ.get("AGENT_ROLES", ""), os.environ.get("AGENT_ROLE", ""))
    except ValueError as exc:      # fail fast before any heavy import
        logger.error("%s", exc)
        sys.exit(1)
    asyncio.run(_main())
