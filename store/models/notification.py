"""Operator notifications raised by the model gateway (breakers, failover, last resort).

No recipients, receipts or pool tasks: who reads them is the consuming application's business (a dashboard, an alert
relay, `psql`). Repeats with the same dedupe key inside ENGINE_NOTIFY_GROUP_S become one row
with a count (store/services/notify.py).
"""

from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, Column, DateTime, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID

from .activity import Base

SEVERITIES = ("success", "info", "warning", "error")


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint("severity IN ('success', 'info', 'warning', 'error')", name="ck_notifications_severity"),
        Index("ix_notifications_dedupe", "dedupe_key", "last_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event = Column(String(64), nullable=False, index=True)          # e.g. gateway.failover
    category = Column(String(32), nullable=False, index=True)       # models, secrets
    severity = Column(String(8), nullable=False)
    title = Column(String(200), nullable=False)
    body = Column(Text, nullable=False, default="")
    link = Column(String(300), nullable=True)
    dedupe_key = Column(String(200), nullable=True)
    count = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    last_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
