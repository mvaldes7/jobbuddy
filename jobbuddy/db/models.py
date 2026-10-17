"""SQLAlchemy 2.x models. Dialect-neutral: Postgres (primary), Azure SQL, SQLite."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from jobbuddy.db.states import APP_STATUSES, JOB_STATUSES, OUTCOMES, QUESTION_STATUSES
from jobbuddy.db.types import Embedding

JSONType = JSON().with_variant(JSONB(), "postgresql")
# BIGINT identity on Postgres/MSSQL, INTEGER on SQLite (so autoincrement works there).
PK = BigInteger().with_variant(Integer(), "sqlite")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _in(col: str, values: list[str]) -> str:
    return f"{col} IN (" + ", ".join(f"'{v}'" for v in values) + ")"


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "companies"
    __table_args__ = (UniqueConstraint("ats", "board_token", name="uq_companies_ats_board"),)

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255))
    ats: Mapped[str] = mapped_column(String(32))
    board_token: Mapped[str] = mapped_column(String(255))
    careers_url: Mapped[str | None] = mapped_column(String(1024))
    ats_config: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    jobs: Mapped[list["Job"]] = relationship(back_populates="company")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("source", "external_job_id", name="uq_jobs_source_external"),
        CheckConstraint(_in("status", JOB_STATUSES), name="ck_jobs_status"),
        Index("ix_jobs_status", "status"),
        Index("ix_jobs_company", "company_id"),
    )

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"))
    source: Mapped[str] = mapped_column(String(32))  # greenhouse|lever|ashby|workday|bamboohr|linkedin_alert|indeed_alert
    external_job_id: Mapped[str] = mapped_column(String(255))
    company_name: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(512))
    location: Mapped[str | None] = mapped_column(String(512))
    remote: Mapped[bool | None] = mapped_column(Boolean)
    url: Mapped[str | None] = mapped_column(String(2048))
    apply_url: Mapped[str | None] = mapped_column(String(2048))
    description: Mapped[str | None] = mapped_column(Text)
    salary_min: Mapped[int | None] = mapped_column(Integer)
    salary_max: Mapped[int | None] = mapped_column(Integer)
    salary_currency: Mapped[str | None] = mapped_column(String(8))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), default="found")
    lead_meta: Mapped[dict[str, Any] | None] = mapped_column(JSONType)  # email-alert leads: resolution info
    embedding: Mapped[list[float] | None] = mapped_column(Embedding(384))

    company: Mapped[Company | None] = relationship(back_populates="jobs")
    scores: Mapped[list["JobScore"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    application: Mapped["Application | None"] = relationship(back_populates="job", uselist=False)


class JobScore(Base):
    __tablename__ = "job_scores"
    __table_args__ = (Index("ix_job_scores_job", "job_id"),)

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"))
    filter_passed: Mapped[bool] = mapped_column(Boolean)
    filter_reasons: Mapped[list[str] | None] = mapped_column(JSONType)
    similarity: Mapped[float | None] = mapped_column(Float)
    embedding_model: Mapped[str | None] = mapped_column(String(255))
    llm_score: Mapped[int | None] = mapped_column(Integer)
    llm_reasons: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    llm_model: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    job: Mapped[Job] = relationship(back_populates="scores")


class CandidateDocument(Base):
    """Resume / cover-letter files stored in the DB (bytea) so PII never lives in the repo."""

    __tablename__ = "candidate_documents"
    __table_args__ = (
        CheckConstraint(
            _in("kind", ["resume", "cover_letter_sample", "tailored_resume", "cover_letter"]),
            name="ck_candidate_documents_kind",
        ),
        Index("ix_candidate_documents_kind", "kind"),
    )

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32))
    filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(128))
    content: Mapped[bytes] = mapped_column(LargeBinary)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    application_id: Mapped[int | None] = mapped_column(ForeignKey("applications.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Application(Base):
    __tablename__ = "applications"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_applications_job"),
        CheckConstraint(_in("status", APP_STATUSES), name="ck_applications_status"),
        CheckConstraint(_in("outcome", OUTCOMES), name="ck_applications_outcome"),
        CheckConstraint("status <> 'submitted' OR approved_at IS NOT NULL", name="ck_applications_submit_requires_approval"),
        Index("ix_applications_status", "status"),
    )

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"))
    status: Mapped[str] = mapped_column(String(32), default="scored")
    outcome: Mapped[str] = mapped_column(String(32), default="none")
    ats: Mapped[str | None] = mapped_column(String(32))
    tailored_resume: Mapped[dict[str, Any] | None] = mapped_column(JSONType)  # validated bullets + summary
    cover_letter_text: Mapped[str | None] = mapped_column(Text)
    answers: Mapped[dict[str, Any] | None] = mapped_column(JSONType)          # open-ended question answers
    filled_state: Mapped[dict[str, Any] | None] = mapped_column(JSONType)     # field_id -> mapping, per step
    low_confidence: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType)
    human_edits: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    pause_reason: Mapped[str | None] = mapped_column(String(255))
    error: Mapped[str | None] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_via: Mapped[str | None] = mapped_column(String(32))  # telegram|dashboard|cli (copied from approvals)
    approval_nonce: Mapped[str | None] = mapped_column(String(64))  # current nonce sent in Telegram buttons
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    job: Mapped[Job] = relationship(back_populates="application")
    events: Mapped[list["ApplicationEvent"]] = relationship(
        back_populates="application", order_by="ApplicationEvent.id"
    )


class ApplicationEvent(Base):
    """Append-only audit log (UPDATE/DELETE blocked by DB triggers and ORM hooks)."""

    __tablename__ = "application_events"
    __table_args__ = (Index("ix_application_events_app", "application_id"),)

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"))
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    event_type: Mapped[str] = mapped_column(String(64))  # transition|approval|edit|notify|outcome|error
    actor: Mapped[str] = mapped_column(String(64), default="system")
    note: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    application: Mapped[Application] = relationship(back_populates="events")


class Approval(Base):
    """Human decisions. Written by the pipeline (Telegram callbacks) and the dashboard (INSERT only).

    * nonce is unique and single-use: a replayed Telegram callback cannot create a second row.
    * Submission requires an unconsumed `approve` row; consumed_at is set atomically
      (UPDATE ... WHERE consumed_at IS NULL) immediately before the final Submit click.
    * processed_at marks rows the pipeline has reconciled into applications.status.
    """

    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint("nonce", name="uq_approvals_nonce"),
        CheckConstraint(_in("decision", ["approve", "edit", "skip"]), name="ck_approvals_decision"),
        CheckConstraint(_in("decided_via", ["telegram", "dashboard", "cli", "slack", "email"]), name="ck_approvals_via"),
        Index("ix_approvals_app", "application_id"),
    )

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"))
    nonce: Mapped[str] = mapped_column(String(64))
    decision: Mapped[str] = mapped_column(String(16))
    decided_via: Mapped[str] = mapped_column(String(16))
    telegram_user_id: Mapped[str | None] = mapped_column(String(32))
    edits: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Screenshot(Base):
    """Filled-form screenshots live ONLY here (bytea) and in Mike's private Telegram chat - never artifacts."""

    __tablename__ = "screenshots"
    __table_args__ = (Index("ix_screenshots_app", "application_id"),)

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"))
    tag: Mapped[str] = mapped_column(String(32))  # filled | blocked | validation | submitted | ...
    png: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProcessedEmail(Base):
    """Job-alert emails already parsed (sha256 of Message-ID). Mailbox flags are never touched."""

    __tablename__ = "processed_emails"
    __table_args__ = (UniqueConstraint("message_id_sha256", name="uq_processed_emails_msgid"),)

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    message_id_sha256: Mapped[str] = mapped_column(String(64))
    origin: Mapped[str | None] = mapped_column(String(16))
    leads_found: Mapped[int] = mapped_column(Integer, default=0)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class FieldQuestion(Base):
    """Form fields the deterministic mapper could not fill. CI inserts them as 'pending' and moves the
    application to 'needs_answers'; the on-box agent answers them; the next CI run refills the form."""

    __tablename__ = "field_questions"
    __table_args__ = (
        UniqueConstraint("application_id", "field_id", name="uq_field_questions_app_field"),
        CheckConstraint(_in("status", QUESTION_STATUSES), name="ck_field_questions_status"),
        Index("ix_field_questions_status", "status"),
    )

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"))
    ats: Mapped[str | None] = mapped_column(String(32))
    field_id: Mapped[str] = mapped_column(String(255))
    label: Mapped[str | None] = mapped_column(Text)
    label_normalized: Mapped[str | None] = mapped_column(String(512))
    field_type: Mapped[str | None] = mapped_column(String(32))
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    options: Mapped[list[str] | None] = mapped_column(JSONType)
    max_length: Mapped[int | None] = mapped_column(Integer)
    page_url: Mapped[str | None] = mapped_column(String(2048))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    answer: Mapped[Any | None] = mapped_column(JSONType)          # str | list[str] | bool
    answered_by: Mapped[str | None] = mapped_column(String(32))    # agent | human
    reusable: Mapped[bool] = mapped_column(Boolean, default=False)  # also saved to form_field_mappings
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FormFieldMapping(Base):
    """Learned label -> profile-key mappings, reused on later forms."""

    __tablename__ = "form_field_mappings"
    __table_args__ = (UniqueConstraint("ats", "label_normalized", name="uq_form_field_mappings_ats_label"),)

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    ats: Mapped[str] = mapped_column(String(32))
    label_normalized: Mapped[str] = mapped_column(String(512))
    field_type: Mapped[str | None] = mapped_column(String(32))
    profile_key: Mapped[str | None] = mapped_column(String(255))
    static_value: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.9)
    times_used: Mapped[int] = mapped_column(Integer, default=0)
    times_confirmed: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(32), default="human")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LLMUsage(Base):
    __tablename__ = "llm_usage"
    __table_args__ = (Index("ix_llm_usage_run", "run_id"),)

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(255))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    trigger: Mapped[str | None] = mapped_column(String(32))
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    error: Mapped[str | None] = mapped_column(Text)


class KeyValue(Base):
    """Small state store (e.g. Telegram getUpdates offset)."""

    __tablename__ = "kv_state"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AppendOnlyViolation(RuntimeError):
    pass


@event.listens_for(ApplicationEvent, "before_update")
def _no_update(mapper, connection, target):  # noqa: ANN001
    raise AppendOnlyViolation("application_events is append-only")


@event.listens_for(ApplicationEvent, "before_delete")
def _no_delete(mapper, connection, target):  # noqa: ANN001
    raise AppendOnlyViolation("application_events is append-only")
