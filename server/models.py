"""SQLAlchemy models: User, Run, RunEvent.

Phase 1 (see README "Honest limitations" before deploying this for more
than one person): every run still authenticates its Claude Code sessions
via the single shared `claude` CLI login on the host — there is no
per-user Claude billing/quota isolation yet. User accounts here exist so
runs have an owner and the UI can list "my runs", not to isolate Claude
Code usage between users.
"""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> datetime:
    return datetime.now(timezone.utc)


class RunStatus(str, enum.Enum):
    running = "running"
    completed = "completed"
    failed = "failed"


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    runs: Mapped[list["Run"]] = relationship(back_populates="owner")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[RunStatus] = mapped_column(SAEnum(RunStatus), default=RunStatus.running)
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    workspace_path: Mapped[str] = mapped_column(Text)
    checkpoint_path: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    owner: Mapped["User"] = relationship(back_populates="runs")
    events: Mapped[list["RunEvent"]] = relationship(back_populates="run", order_by="RunEvent.seq")


class RunEvent(Base):
    __tablename__ = "run_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    # Monotonic per-run counter (assigned by the runner, not the DB) — two
    # events can share a wall-clock timestamp, so `at` alone can't order them.
    seq: Mapped[int] = mapped_column()
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    source: Mapped[str] = mapped_column(String(64))  # agent name, or "system"
    # turn_started | tool_call | turn_completed | verdict | run_completed | run_failed
    event_type: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str] = mapped_column(Text, default="")
    extra: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON: cost_usd, tool_name, etc.

    run: Mapped["Run"] = relationship(back_populates="events")
