"""Declarative base and the run a model call is attributed to.

Extracted from vanguard-control's api/models/activity.py. Only ActivityRun is kept, without
its links to plans, templates and schedules (those stay in the application that owns runs):
the gateway keeps a worker's X-Vanguard-Activity "run:<uuid>" only when that run exists, so
money can be summed per run. An application that dispatches work to the workers inserts its
runs here (or leaves the table empty: calls are then simply not attributed).
"""

from __future__ import annotations

import enum
import uuid

from sqlalchemy import Column, DateTime, Enum as SAEnum, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    # Required for SQLAlchemy 2.0 annotated declarative scanner when using
    # `from __future__ import annotations` with bare (non-Mapped[]) annotations.
    __allow_unmapped__ = True


class RunStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ActivityRun(Base):
    """A unit of work the workers do for a caller (one A2A conversation or a whole pipeline)."""
    __tablename__ = "activity_runs"
    __table_args__ = (Index("ix_activity_runs_started", "started_at"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_name = Column(String, nullable=False)
    sdd_yaml = Column(Text, nullable=False, default="")   # the caller's spec snapshot, if any
    status = Column(SAEnum(RunStatus), default=RunStatus.QUEUED, nullable=False, index=True)
    started_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    error_message = Column(Text, nullable=True)
    # The agent team this run works with (G-AGENTS A3): the gateway puts the team's model for a role
    # first. In vanguard-control the run reaches its team through its plan; here directly.
    agent_team_id = Column(UUID(as_uuid=True), ForeignKey("agent_teams.id", name="fk_activity_runs_agent_team"),
                           nullable=True)
