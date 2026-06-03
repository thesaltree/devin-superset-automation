"""SQLAlchemy models for tasks and metrics."""
from __future__ import annotations

from datetime import datetime
from sqlalchemy import String, Integer, DateTime, Text, JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# Task status state machine:
#   queued -> running -> awaiting_ci -> completed
#                                   \-> failed
#   running can also -> failed (Devin gave up)
class Task(Base):
    """A single remediation unit handled by one Devin session."""
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # What kind of session this is
    role: Mapped[str] = mapped_column(String(20))  # "orchestrator" | "worker"

    # Issue context (workers only)
    issue_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    issue_title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    issue_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Devin session
    session_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    session_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Lifecycle
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    pr_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    pr_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    branch_name: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Self-correction tracking
    ci_retries: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Devin returned structured output (for orchestrator: the remediation plan)
    structured_output: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def duration_seconds(self) -> int | None:
        if self.completed_at:
            return int((self.completed_at - self.created_at).total_seconds())
        return int((datetime.utcnow() - self.created_at).total_seconds())
