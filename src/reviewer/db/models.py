"""SQLAlchemy 2.0 models. Schema changes must be accompanied by an Alembic migration."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

RUN_STATUSES = ("queued", "running", "succeeded", "failed")
FINDING_CATEGORIES = ("bug", "security", "performance", "quality", "edge_case", "missing_test")
FINDING_SEVERITIES = ("high", "medium", "low")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class PullRequest(Base):
    __tablename__ = "pull_requests"
    __table_args__ = (UniqueConstraint("repo_full_name", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    repo_full_name: Mapped[str] = mapped_column(String(255))
    number: Mapped[int] = mapped_column(Integer)

    runs: Mapped[list["Run"]] = relationship(back_populates="pull_request")


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (CheckConstraint(_in("status", RUN_STATUSES), name="status"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    pr_id: Mapped[int] = mapped_column(ForeignKey("pull_requests.id"), index=True)
    head_sha: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(16), default="queued")
    # GitHub's X-GitHub-Delivery header: makes webhook redeliveries idempotent.
    trigger_delivery_id: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    review_comment_id: Mapped[int | None] = mapped_column(BigInteger)
    summary: Mapped[str | None] = mapped_column(Text)
    verdict: Mapped[str | None] = mapped_column(String(16))
    # Newline-separated notes about a degraded run (failed optional steps, truncation, ...).
    warnings: Mapped[str | None] = mapped_column(Text)

    pull_request: Mapped[PullRequest] = relationship(back_populates="runs")
    findings: Mapped[list["Finding"]] = relationship(back_populates="run")
    llm_calls: Mapped[list["LLMCall"]] = relationship(back_populates="run")


class Finding(Base):
    __tablename__ = "findings"
    __table_args__ = (
        CheckConstraint(_in("category", FINDING_CATEGORIES), name="category"),
        CheckConstraint(_in("severity", FINDING_SEVERITIES), name="severity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), index=True)
    category: Mapped[str] = mapped_column(String(16))
    severity: Mapped[str] = mapped_column(String(8))
    file_path: Mapped[str | None] = mapped_column(String(1024))
    line_start: Mapped[int | None] = mapped_column(Integer)
    line_end: Mapped[int | None] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text)
    suggestion: Mapped[str | None] = mapped_column(Text)
    suggested_test_code: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)

    run: Mapped[Run] = relationship(back_populates="findings")


class LLMCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), index=True)
    node: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    retries: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    run: Mapped[Run] = relationship(back_populates="llm_calls")
