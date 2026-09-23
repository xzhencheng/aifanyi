import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from sqlalchemy import JSON, Float, Index, Integer, String, Text, UniqueConstraint, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool


def uid(prefix="obj"):
    return f"{prefix}_{uuid4().hex}"


def now():
    return time.time()


class Base(DeclarativeBase):
    pass


class Record(Base):
    """Versioned aggregates. Immutable kinds are only inserted, never edited by business services."""

    __tablename__ = "records"
    id: Mapped[str] = mapped_column(String(80), primary_key=True, default=uid)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    tenant: Mapped[str] = mapped_column(String(100), index=True)
    project: Mapped[str] = mapped_column(String(100), default="", index=True)
    owner: Mapped[str] = mapped_column(String(200), default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created: Mapped[float] = mapped_column(Float, default=now)
    updated: Mapped[float] = mapped_column(Float, default=now, onupdate=now)
    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (Index("ix_records_scope_kind", "tenant", "project", "kind"),)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: uid("job"))
    tenant: Mapped[str] = mapped_column(String(100), index=True)
    project: Mapped[str] = mapped_column(String(100), index=True)
    owner: Mapped[str] = mapped_column(String(200))
    translation_id: Mapped[str] = mapped_column(String(80), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    stage: Mapped[str] = mapped_column(String(24), default="validating")
    source_id: Mapped[str] = mapped_column(String(80))
    snapshot_id: Mapped[str] = mapped_column(String(80))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created: Mapped[float] = mapped_column(Float, default=now)
    updated: Mapped[float] = mapped_column(Float, default=now, onupdate=now)
    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (UniqueConstraint("translation_id", "revision"),)


class Segment(Base):
    __tablename__ = "segments"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(80), index=True)
    source_id: Mapped[str] = mapped_column(String(80))
    index: Mapped[int] = mapped_column(Integer)
    raw_text: Mapped[str] = mapped_column(Text)
    locator: Mapped[dict] = mapped_column(JSON)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    version: Mapped[int] = mapped_column(Integer, default=1)
    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (UniqueConstraint("job_id", "index"),)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: uid("msg"))
    conversation_id: Mapped[str] = mapped_column(String(80), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    client_message_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created: Mapped[float] = mapped_column(Float, default=now)
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence"),
        UniqueConstraint("conversation_id", "client_message_id"),
    )


class Event(Base):
    __tablename__ = "events"
    id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: uid("evt"))
    tenant: Mapped[str] = mapped_column(String(100))
    project: Mapped[str] = mapped_column(String(100))
    subject_id: Mapped[str] = mapped_column(String(80), index=True)
    sequence: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(60))
    body: Mapped[dict] = mapped_column(JSON)
    raw_body: Mapped[str] = mapped_column(Text)
    created: Mapped[float] = mapped_column(Float, default=now)
    __table_args__ = (UniqueConstraint("subject_id", "sequence"),)


class Work(Base):
    __tablename__ = "work"
    id: Mapped[str] = mapped_column(String(140), primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    target: Mapped[str] = mapped_column(String(80))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    lease_token: Mapped[str | None] = mapped_column(String(80), nullable=True)
    lease_until: Mapped[float] = mapped_column(Float, default=0)
    available_at: Mapped[float] = mapped_column(Float, default=now)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(80), nullable=True)


class Idempotency(Base):
    __tablename__ = "idempotency"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    body_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict] = mapped_column(JSON)
    expires: Mapped[float] = mapped_column(Float)


class ModelCall(Base):
    __tablename__ = "model_calls"
    id: Mapped[str] = mapped_column(String(80), primary_key=True, default=lambda: uid("call"))
    execution_key: Mapped[str] = mapped_column(String(64), index=True)
    attempt: Mapped[int] = mapped_column(Integer)
    budget_key: Mapped[str] = mapped_column(String(180), index=True)
    role: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(32))
    config: Mapped[dict] = mapped_column(JSON)
    response: Mapped[dict] = mapped_column(JSON, default=dict)
    created: Mapped[float] = mapped_column(Float, default=now)
    __table_args__ = (UniqueConstraint("execution_key", "attempt"),)


class ModelSlot(Base):
    __tablename__ = "model_slots"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    token: Mapped[str] = mapped_column(String(80), default="")
    expires: Mapped[float] = mapped_column(Float, default=0)


class Database:
    def __init__(self, url):
        args = {}
        if url.startswith("sqlite"):
            args["connect_args"] = {"check_same_thread": False, "timeout": 30}
            if ":memory:" in url:
                args["poolclass"] = StaticPool
            else:
                Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(url, pool_pre_ping=True, **args)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine, "connect")
            def pragmas(conn, _):
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA journal_mode=WAL")

        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self):
        with self.sessions.begin() as session:
            yield session


class Budget(Base):
    __tablename__ = "call_budgets"
    key: Mapped[str] = mapped_column(String(180), primary_key=True)
    used: Mapped[int] = mapped_column(Integer, default=0)
