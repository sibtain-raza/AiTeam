"""SQLite database setup for the web UI's control plane (users/runs/events).

This is separate from — and does not replace — the pipeline's own on-disk
state: the per-run workspace, `.pipeline-docs/` artifacts, and the
checkpoint JSON files under `output/checkpoints/` are still the source of
truth for actually resuming a run. This database only tracks who owns
which run and the live event feed the UI subscribes to.

Phase 1 (see README "Honest limitations"): SQLite, single file, no
migrations tooling — deliberately minimal for a not-yet-multi-tenant-hardened
deployment. Override LOOPER_DB_URL for Postgres etc. when that changes.
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

DB_URL = os.environ.get("LOOPER_DB_URL", "sqlite:///./looper_web.db")

_connect_args = {"check_same_thread": False} if DB_URL.startswith("sqlite") else {}
engine = create_engine(DB_URL, connect_args=_connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401  (import registers the tables on Base)

    Base.metadata.create_all(bind=engine)
