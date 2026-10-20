"""Create and seed the engine's schema (one-shot): python -m store.schema

Idempotent, for the tables the engine keeps:
- the gateway's tables (store.models) through Base.metadata.create_all (idempotent: only
  missing tables are created; an existing column is never altered);
- schema `adk`, where ADK's DatabaseSessionService creates its own tables (workers connect
  with search_path=adk), so an INPUT_REQUIRED task resumes after a worker restart;
- `a2a_tasks`, created by a2a-sdk's own DatabaseTaskStore model (workers open it with
  create_table=False, so they never race on startup);
- the built-in AI presets, the pinned price table and the built-in agent team `basic`
  (no model, no addendum: every role as its base instruction says).
Run by the `schema` service in docker-compose.yml before the gateway and the workers start.
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

logger = logging.getLogger("store.schema")


def database_url() -> str:
    from urllib.parse import quote
    return os.environ.get("DATABASE_URL") or (
        f"postgresql+asyncpg://{os.environ.get('POSTGRES_USER', 'engine')}:"
        f"{quote(os.environ.get('POSTGRES_PASSWORD', ''), safe='')}@{os.environ.get('POSTGRES_HOST', 'postgres')}:"
        f"{os.environ.get('POSTGRES_PORT', '5432')}/{os.environ.get('POSTGRES_DB', 'engine')}")


async def migrate(url: str | None = None) -> None:
    from a2a.server.tasks import DatabaseTaskStore  # type: ignore[import]

    import store.models  # noqa: F401  (registers every table)
    from store.models.activity import Base
    from store.services import presets, prices

    engine = create_async_engine(url or database_url(), pool_pre_ping=True)
    try:
        async with engine.begin() as conn:
            if conn.dialect.name == "postgresql":
                await conn.execute(text("CREATE SCHEMA IF NOT EXISTS adk"))
            await conn.run_sync(Base.metadata.create_all)
        await DatabaseTaskStore(engine, create_table=True, table_name="a2a_tasks").initialize()
        sf = async_sessionmaker(engine, expire_on_commit=False)
        async with sf() as db:
            await presets.seed_presets(db)
        async with sf() as db:
            seeded = await prices.seed_prices(db)
            await db.commit()
        async with sf() as db:
            from sqlalchemy import select
            from store.models.automation import AgentTeam
            if (await db.execute(select(AgentTeam).where(AgentTeam.name == "basic"))).scalar_one_or_none() is None:
                db.add(AgentTeam(name="basic", description="Every agent as its base instruction says.", members={},
                                 builtin=True, created_by="seed", updated_by="seed"))
                await db.commit()
        logger.info("schema ready: %d tables, schema adk, a2a_tasks; presets and team basic seeded, %d price rows added",
                    len(Base.metadata.tables), seeded)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(migrate())
