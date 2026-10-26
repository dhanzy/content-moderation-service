import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # admin | member


class Media(Base):
    """A media item and the decision reached about it.

    `screened_at` is the single source of truth for "was this actually screened".
    A finished job is not proof: the job row can complete for reasons that never
    produced a provider decision, which is one of the things that went wrong in
    the incident.
    """

    __tablename__ = "media"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey("workspaces.id"), index=True, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False)

    type: Mapped[str] = mapped_column(String(16), nullable=False)  # image | video
    source: Mapped[str] = mapped_column(String(16), nullable=False)  # upload | generation
    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    frames: Mapped[list] = mapped_column(JSON, nullable=False)

    status: Mapped[str] = mapped_column(String(16), default="pending", index=True, nullable=False)
    explicit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reasons: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    # Set only when a provider decision was actually applied to this item.
    screened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    frames_scored: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)


class Job(Base):
    """One screening job per media item, leased by exactly one worker at a time."""

    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    media_id: Mapped[str] = mapped_column(ForeignKey("media.id"), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)

    # queued | running | done | failed | awaiting_callback
    state: Mapped[str] = mapped_column(String(24), default="queued", index=True, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(64), nullable=True)

    lease_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # One screening job per media item. Redelivery must not create a second row.
    __table_args__ = (UniqueConstraint("media_id", name="uq_jobs_media_id"),)


class JobAttempt(Base):
    """Audit trail, written *before* the provider call so a crash leaves a trace.

    An attempt stuck in `in_flight` with no ProviderResult is the signature of a
    worker that died between the provider scoring the media and the result being
    saved: the money was spent but we cannot prove what came back.
    """

    __tablename__ = "job_attempts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True, nullable=False)
    media_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)

    # in_flight | scored | accepted | failed | skipped_cached
    state: Mapped[str] = mapped_column(String(24), default="in_flight", nullable=False)
    error_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProviderResult(Base):
    """The provider's answer, stored once per media item.

    The primary key is the media ID, so a second successful score for the same
    media cannot be recorded, and any worker that finds a row here must reuse it
    instead of paying for another call.
    """

    __tablename__ = "provider_results"
    media_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    frames: Mapped[list] = mapped_column(JSON, nullable=False)
    via: Mapped[str] = mapped_column(String(16), nullable=False)  # sync | callback
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)


class ReviewAction(Base):
    __tablename__ = "review_actions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    media_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # approve | reject
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, nullable=False)
