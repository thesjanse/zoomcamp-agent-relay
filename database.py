"""PostgreSQL database setup and durable Agent Relay models.

This module owns the PostgreSQL engine and the ORM models.  The rest of the
application talks to the models through :mod:`storage`, which coordinates
concurrent claims, heartbeats, terminal submissions, and lease recovery with
PostgreSQL row locks (``FOR UPDATE`` / ``FOR UPDATE SKIP LOCKED``) instead of a
process-wide writer lock.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker


LOGGER = logging.getLogger("agent_relay.database")


def _database_url() -> str:
    return (
        os.getenv("RELAY_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or "postgresql+psycopg://relay:relay@127.0.0.1:5432/agent_relay"
    )


def positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


DATABASE_URL = _database_url()
LEASE_SECONDS = positive_int("RELAY_LEASE_SECONDS", 60)
MAX_ATTEMPTS = positive_int("RELAY_MAX_ATTEMPTS", 5)
RECOVERY_INTERVAL_SECONDS = max(1, positive_int("RELAY_RECOVERY_INTERVAL_SECONDS", 5))
MAX_BODY_BYTES = positive_int("RELAY_MAX_BODY_BYTES", 256 * 1024)
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 100


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_db_time(value: datetime) -> datetime:
    """Store wall-clock UTC moments as naive datetimes in ``timestamp`` columns.

    Values are always written and read as UTC, so no PostgreSQL session
    timezone can reinterpret them.
    """

    return value.astimezone(timezone.utc).replace(tzinfo=None)


def db_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso_time(value: datetime | None) -> str | None:
    value = db_time(value)
    if value is None:
        return None
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


class Base(DeclarativeBase):
    pass


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    sent_tasks: Mapped[list[Task]] = relationship(
        "Task", foreign_keys="Task.sender_id", back_populates="sender", passive_deletes=True
    )
    received_tasks: Mapped[list[Task]] = relationship(
        "Task", foreign_keys="Task.recipient_id", back_populates="recipient", passive_deletes=True
    )


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("sender_id", "idempotency_key", name="uq_task_sender_idempotency"),)

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    sender_id: Mapped[str] = mapped_column(String(100), ForeignKey("agents.id"), nullable=False, index=True)
    recipient_id: Mapped[str] = mapped_column(String(100), ForeignKey("agents.id"), nullable=False, index=True)
    input: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    sender: Mapped[Agent] = relationship("Agent", foreign_keys=[sender_id], back_populates="sent_tasks")
    recipient: Mapped[Agent] = relationship("Agent", foreign_keys=[recipient_id], back_populates="received_tasks")
    attempts: Mapped[list[Attempt]] = relationship(
        "Attempt", back_populates="task", cascade="all, delete-orphan", order_by="Attempt.attempt_number"
    )


class Attempt(Base):
    __tablename__ = "attempts"
    __table_args__ = (UniqueConstraint("task_id", "attempt_number", name="uq_attempt_task_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(
        String(100), ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    claim_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    terminal_action: Mapped[str | None] = mapped_column(String(10), nullable=True)
    terminal_payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    task: Mapped[Task] = relationship("Task", back_populates="attempts")


engine: Engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True)

SessionLocal = sessionmaker(bind=engine, class_=Session, expire_on_commit=False, autoflush=True)

_init_retries = max(1, positive_int("RELAY_DB_INIT_RETRIES", 30))
_init_delay_seconds = max(0.5, positive_int("RELAY_DB_INIT_DELAY_SECONDS", 1))


def init_db() -> None:
    """Create all tables, retrying until PostgreSQL accepts connections.

    ``main`` initializes the schema at import time, so this must tolerate the
    database being briefly unavailable (for example while a container network
    or the ``postgres`` service is still starting).  ``create_all`` is
    idempotent and safe to run after a partial boot.
    """

    for attempt in range(1, _init_retries + 1):
        try:
            Base.metadata.create_all(engine)
            return
        except OperationalError:
            if attempt >= _init_retries:
                raise
            LOGGER.warning("database not ready (attempt %d/%d); retrying", attempt, _init_retries)
            time.sleep(_init_delay_seconds)


@contextmanager
def db_session() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def immediate_transaction() -> Generator[Session, None, None]:
    """Open one PostgreSQL transaction for a write that takes row locks.

    ``storage`` uses this seam wherever two operations must agree on one
    outcome: task creation (sender-scoped idempotency), claims, heartbeats,
    terminal submissions, and lease recovery.  Concurrency is controlled by
    ``SELECT ... FOR UPDATE [SKIP LOCKED]`` inside the transaction rather than
    a process-wide writer lock.
    """

    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def recover_expired_in_session(db: Session, now: datetime) -> int:
    """Expire active leases and requeue/fail their tasks within ``db``."""

    now_db = as_db_time(now)
    expired = list(
        db.scalars(
            select(Attempt)
            .where(Attempt.outcome == "processing", Attempt.lease_expires_at <= now_db)
            .order_by(Attempt.lease_expires_at, Attempt.id)
            .with_for_update()
        )
    )
    count = 0
    for attempt in expired:
        task = db.get(Task, attempt.task_id)
        if task is None or attempt.outcome != "processing":
            continue
        attempt.outcome = "expired"
        attempt.finished_at = now_db
        if task.status == "processing":
            if task.attempt_count >= MAX_ATTEMPTS:
                task.status = "failed"
                task.error = "attempts_exhausted"
                task.output = None
                task.finished_at = now_db
            else:
                task.status = "queued"
                task.finished_at = None
        count += 1
    return count


def recover_expired() -> int:
    """Run one recovery pass and return the number of expired attempts."""

    with immediate_transaction() as db:
        return recover_expired_in_session(db, utcnow())


__all__ = [
    "Agent",
    "Attempt",
    "Base",
    "DATABASE_URL",
    "DEFAULT_PAGE_SIZE",
    "LEASE_SECONDS",
    "MAX_ATTEMPTS",
    "MAX_BODY_BYTES",
    "MAX_PAGE_SIZE",
    "RECOVERY_INTERVAL_SECONDS",
    "Task",
    "as_db_time",
    "db_session",
    "db_time",
    "engine",
    "immediate_transaction",
    "init_db",
    "iso_time",
    "recover_expired",
    "recover_expired_in_session",
    "utcnow",
]
