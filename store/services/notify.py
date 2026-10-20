"""Gateway notifications: what the operator is told, grouped by key.

The gateway's events and their grouping; recipients and read state belong to the application
that has users. The contract is unchanged: `emit(db, event, ...)` records in the
caller's transaction (no commit) inside a SAVEPOINT, and never raises — telling people must
not break the thing being told about.

Repeats with the same dedupe key inside ENGINE_NOTIFY_GROUP_S (default 3600) become one row
with a count. Every notification is also logged (logger "store.notify"), so a deployment
without a reader still sees them in `docker compose logs`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..models.notification import Notification

logger = logging.getLogger("store.notify")


@dataclass(frozen=True)
class EventDef:
    category: str
    severity: str
    title: str                      # str.format with the event's params


CATALOGUE: dict[str, EventDef] = {
    "secrets.unavailable": EventDef("secrets", "error", "Secret store unavailable"),
    "gateway.breaker_opened": EventDef("models", "error", "Model key blocked: {account}"),
    "gateway.failover": EventDef("models", "warning", "Model calls moved to the backup key {account}"),
    "gateway.local_fallback": EventDef("models", "warning", "No cloud model reachable: the local {model} answers"),
    "gateway.internet_down": EventDef("models", "error", "No internet: cloud models unreachable"),
    "gateway.recovered": EventDef("models", "success", "Back on the primary key {account}"),
    "gateway.last_resort": EventDef("models", "warning", "Every model is down: the deterministic stand-in answered"),
}
_LOG_LEVEL = {"success": logging.INFO, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


def group_s() -> float:
    return float(os.environ.get("ENGINE_NOTIFY_GROUP_S", "3600"))


def _render(template: str, params: dict[str, Any]) -> str:
    try:
        return template.format(**params)[:200]
    except (KeyError, IndexError, ValueError):
        return template[:200]


async def emit(db: AsyncSession, event: str, *, params: Optional[dict[str, Any]] = None, body: str = "",
               link: Optional[str] = None, dedupe_key: Optional[str] = None, **_ignored: Any) -> Optional[Notification]:
    """Record a notification in the caller's transaction (no commit). Never raises. Extra keyword
    arguments of the full notifier (people, inventory_id, entity) are accepted and ignored."""
    try:
        async with db.begin_nested():
            return await _emit(db, event, params or {}, body, link, dedupe_key)
    except Exception as exc:  # a notification must never fail its producer
        logger.warning("notification %s not recorded: %s", event, exc)
        return None


async def _emit(db: AsyncSession, event: str, params: dict[str, Any], body: str, link: Optional[str],
                dedupe_key: Optional[str]) -> Notification:
    spec = CATALOGUE[event]
    now = datetime.now(timezone.utc)
    title = _render(spec.title, params)
    logger.log(_LOG_LEVEL.get(spec.severity, logging.INFO), "%s: %s%s", event, title, f" — {body}" if body else "")
    existing = None
    if dedupe_key:
        existing = (await db.execute(
            select(Notification).where(Notification.dedupe_key == dedupe_key,
                                       Notification.last_at >= now - timedelta(seconds=group_s()))
            .order_by(Notification.last_at.desc()).limit(1))).scalar_one_or_none()
    if existing is not None:
        existing.count += 1
        existing.last_at = now
        existing.title, existing.body = title, body[:2000] or existing.body
        await db.flush()
        return existing
    note = Notification(event=event, category=spec.category, severity=spec.severity, title=title,
                        body=body[:2000], link=link, dedupe_key=dedupe_key, last_at=now, created_at=now)
    db.add(note)
    await db.flush()
    return note


async def emit_now(session_factory: async_sessionmaker[AsyncSession], event: str, **kwargs: Any) -> None:
    """For producers outside a transaction: emit and commit in a session of its own."""
    try:
        async with session_factory() as db:
            await emit(db, event, **kwargs)
            await db.commit()
    except Exception as exc:
        logger.warning("notification %s not recorded: %s", event, exc)
