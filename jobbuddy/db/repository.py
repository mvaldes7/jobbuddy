"""Data-access helpers. All functions take the run's single Session and never commit themselves
(the caller's session_scope commits once per run / per stage)."""
from __future__ import annotations

import hashlib
import io
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from jobbuddy.db.models import (
    Application,
    ApplicationEvent,
    Approval,
    CandidateDocument,
    Company,
    FormFieldMapping,
    Job,
    KeyValue,
    LLMUsage,
    ProcessedEmail,
    Screenshot,
    utcnow,
)
from jobbuddy.db.states import OUTCOMES, AppStatus, check_transition
from jobbuddy.fetchers.base import JobPosting


# ---------------- companies & jobs ----------------
def upsert_company(session: Session, *, name: str, ats: str, board_token: str,
                   careers_url: str | None = None, ats_config: dict | None = None,
                   active: bool = True) -> Company:
    company = session.scalar(select(Company).where(Company.ats == ats, Company.board_token == board_token))
    if company is None:
        company = Company(name=name, ats=ats, board_token=board_token)
        session.add(company)
    company.name = name
    company.careers_url = careers_url
    company.ats_config = ats_config
    company.active = active
    session.flush()
    return company


def posting_hash(p: JobPosting) -> str:
    raw = "|".join([p.title or "", p.location or "", p.description or "", str(p.salary_min), str(p.salary_max)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def upsert_job(session: Session, posting: JobPosting, company: Company | None = None) -> tuple[Job, bool]:
    """Idempotent upsert keyed on (source, external_job_id). Returns (job, created)."""
    job = session.scalar(
        select(Job).where(Job.source == posting.source, Job.external_job_id == posting.external_job_id)
    )
    created = job is None
    h = posting_hash(posting)
    now = utcnow()
    if job is None:
        job = Job(source=posting.source, external_job_id=posting.external_job_id, first_seen_at=now, status="found")
        session.add(job)
    changed = job.content_hash != h
    job.company_id = company.id if company else job.company_id
    job.company_name = posting.company_name or (company.name if company else job.company_name)
    job.title = posting.title
    job.location = posting.location
    job.remote = posting.remote
    job.url = posting.url
    job.apply_url = posting.apply_url or job.apply_url
    job.description = posting.description
    job.salary_min = posting.salary_min
    job.salary_max = posting.salary_max
    job.salary_currency = posting.salary_currency
    job.posted_at = posting.posted_at
    job.last_seen_at = now
    if posting.lead_meta:
        job.lead_meta = {**(job.lead_meta or {}), **posting.lead_meta}
    if changed and not created:
        job.embedding = None  # re-embed changed descriptions
        if job.status in ("filtered_out", "below_threshold") and job.application is None:
            job.status = "found"
    job.content_hash = h
    session.flush()
    return job, created


# ---------------- applications ----------------
def get_or_create_application(session: Session, job: Job, ats: str | None) -> Application:
    app = session.scalar(select(Application).where(Application.job_id == job.id))
    if app is None:
        app = Application(job_id=job.id, status=AppStatus.SCORED.value, ats=ats)
        session.add(app)
        session.flush()
        log_event(session, app, "transition", None, AppStatus.SCORED.value, note="application created")
    return app


def log_event(session: Session, app: Application, event_type: str, from_status: str | None,
              to_status: str | None, *, actor: str = "system", note: str | None = None,
              payload: dict[str, Any] | None = None) -> ApplicationEvent:
    ev = ApplicationEvent(application_id=app.id, event_type=event_type, from_status=from_status,
                          to_status=to_status, actor=actor, note=note, payload=payload)
    session.add(ev)
    session.flush()
    return ev


def transition(session: Session, app: Application, new_status: AppStatus | str, *, actor: str = "system",
               note: str | None = None, payload: dict[str, Any] | None = None) -> Application:
    new = AppStatus(new_status).value
    if new == AppStatus.SUBMITTED.value and (app.approved_at is None or not has_consumed_approval(session, app)):
        raise PermissionError("refusing to mark submitted without a consumed approval record")
    check_transition(app.status, new)
    old = app.status
    app.status = new
    app.updated_at = utcnow()
    log_event(session, app, "transition", old, new, actor=actor, note=note, payload=payload)
    return app


# ---------------- approvals (nonce-based, single-use) ----------------
def issue_nonce(app: Application) -> str:
    """New nonce for the approval buttons sent to Telegram. Re-issued on every (re)notification."""
    app.approval_nonce = secrets.token_urlsafe(18)
    return app.approval_nonce


def record_decision(session: Session, app: Application, *, decision: str, via: str, nonce: str | None = None,
                    telegram_user_id: str | None = None, edits: dict[str, Any] | None = None,
                    require_nonce_match: bool = False) -> Approval | None:
    """INSERT an approvals row. Returns None (and records nothing) for stale or replayed nonces."""
    if require_nonce_match:
        if not nonce or not app.approval_nonce or not secrets.compare_digest(nonce, app.approval_nonce):
            return None
    nonce = nonce or secrets.token_urlsafe(18)
    if session.scalar(select(Approval.id).where(Approval.nonce == nonce)) is not None:
        return None  # single-use
    row = Approval(application_id=app.id, nonce=nonce, decision=decision, decided_via=via,
                   telegram_user_id=telegram_user_id, edits=edits or None)
    session.add(row)
    if require_nonce_match:
        app.approval_nonce = None  # button nonce is spent; a re-notification issues a new one
    session.flush()
    return row


def reconcile(session: Session, *, save_learned: Callable[[Application, dict[str, Any]], None] | None = None) -> dict[str, int]:
    """Apply unprocessed approvals rows and dashboard-reported outcome events to applications.

    The dashboard can only INSERT into approvals/application_events; this is where status changes happen.
    """
    stats = {"approved": 0, "edited": 0, "skipped": 0, "stale": 0, "outcomes": 0}
    rows = session.scalars(select(Approval).where(Approval.processed_at.is_(None)).order_by(Approval.id)).all()
    for row in rows:
        app = session.get(Application, row.application_id)
        row.processed_at = utcnow()
        if app is None:
            continue
        if row.decision == "edit":
            if row.edits:
                app.human_edits = {**(app.human_edits or {}), **row.edits}
                if save_learned:
                    save_learned(app, row.edits)
            log_event(session, app, "edit", app.status, app.status, actor=row.decided_via,
                      payload={"approval_id": row.id, "fields": sorted((row.edits or {}).keys())})
            if row.edits and app.status == AppStatus.AWAITING_APPROVAL.value:
                # Re-fill with the edits on the next run, then a fresh card (new screenshot + nonce) goes out.
                transition(session, app, AppStatus.TAILORED, actor=row.decided_via, note="re-fill with human edits")
                app.notified_at = None
            stats["edited"] += 1
        elif row.decision == "approve":
            if app.status != AppStatus.AWAITING_APPROVAL.value:
                stats["stale"] += 1
                log_event(session, app, "approval_ignored", app.status, app.status, actor=row.decided_via,
                          note=f"approve while {app.status}", payload={"approval_id": row.id})
                continue
            app.approved_at = row.decided_at or utcnow()
            app.approved_via = row.decided_via
            log_event(session, app, "approval", app.status, AppStatus.APPROVED.value, actor=row.decided_via,
                      payload={"approval_id": row.id})
            transition(session, app, AppStatus.APPROVED, actor=row.decided_via, note=f"approved via {row.decided_via}")
            stats["approved"] += 1
        elif row.decision == "skip":
            if app.status in (AppStatus.SUBMITTED.value, AppStatus.SKIPPED.value):
                stats["stale"] += 1
                continue
            try:
                transition(session, app, AppStatus.SKIPPED, actor=row.decided_via, note="skipped by Mike")
                stats["skipped"] += 1
            except Exception:
                stats["stale"] += 1
    # Outcomes reported from the dashboard as append-only events.
    evs = session.scalars(select(ApplicationEvent).where(ApplicationEvent.event_type == "outcome_reported")
                          .order_by(ApplicationEvent.id)).all()
    for ev in evs:
        app = session.get(Application, ev.application_id)
        outcome = (ev.payload or {}).get("outcome")
        if app is None or outcome not in OUTCOMES:
            continue
        ev_time = ev.created_at if ev.created_at.tzinfo else ev.created_at.replace(tzinfo=timezone.utc)
        cur = app.outcome_updated_at
        cur = cur if (cur is None or cur.tzinfo) else cur.replace(tzinfo=timezone.utc)
        if cur is None or ev_time > cur:
            app.outcome = outcome
            app.outcome_updated_at = ev_time
            stats["outcomes"] += 1
    session.flush()
    return stats


def active_approval(session: Session, app: Application) -> Approval | None:
    """The unconsumed, reconciled approve row that authorizes ONE submission attempt."""
    return session.scalar(
        select(Approval).where(Approval.application_id == app.id, Approval.decision == "approve",
                               Approval.consumed_at.is_(None), Approval.processed_at.is_not(None))
        .order_by(Approval.id.desc()).limit(1))


def has_approval(session: Session, app: Application) -> bool:
    return app.approved_at is not None and active_approval(session, app) is not None


def consume_approval(session: Session, approval_id: int) -> bool:
    """Atomic single-use consumption. True only for the caller that flipped consumed_at."""
    res = session.execute(update(Approval).where(Approval.id == approval_id, Approval.consumed_at.is_(None))
                          .values(consumed_at=utcnow()))
    session.flush()
    return res.rowcount == 1


def has_consumed_approval(session: Session, app: Application) -> bool:
    return session.scalar(select(Approval.id).where(Approval.application_id == app.id, Approval.decision == "approve",
                                                    Approval.consumed_at.is_not(None)).limit(1)) is not None


def record_approval(session: Session, app: Application, *, via: str = "cli",
                    edits: dict[str, Any] | None = None) -> Application:
    """CLI convenience: insert edit/approve rows, then reconcile immediately."""
    if app.status != AppStatus.AWAITING_APPROVAL.value:
        raise ValueError(f"application {app.id} is {app.status}, not awaiting_approval")
    if edits:
        record_decision(session, app, decision="edit", via=via, edits=edits)
    record_decision(session, app, decision="approve", via=via)
    reconcile(session)
    return app


def submissions_today(session: Session, now: datetime | None = None) -> int:
    now = now or utcnow()
    start = now - timedelta(hours=24)
    return int(session.scalar(
        select(func.count(Application.id)).where(Application.submitted_at.is_not(None),
                                                 Application.submitted_at >= start)
    ) or 0)


def report_outcome(session: Session, app: Application, outcome: str, actor: str = "cli") -> None:
    """Same path as the dashboard: append an event, then reconcile() applies it."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}")
    log_event(session, app, "outcome_reported", None, None, actor=actor, payload={"outcome": outcome})


# ---------------- screenshots & processed emails ----------------
def store_screenshot(session: Session, app: Application, tag: str, png: bytes) -> Screenshot:
    shot = Screenshot(application_id=app.id, tag=tag, png=png)
    session.add(shot)
    session.flush()
    return shot


def latest_screenshot(session: Session, app_id: int) -> Screenshot | None:
    return session.scalar(select(Screenshot).where(Screenshot.application_id == app_id)
                          .order_by(Screenshot.id.desc()).limit(1))


def message_id_hash(message_id: str) -> str:
    return hashlib.sha256((message_id or "").strip().encode("utf-8")).hexdigest()


def email_processed(session: Session, message_id: str) -> bool:
    return session.scalar(select(ProcessedEmail.id).where(
        ProcessedEmail.message_id_sha256 == message_id_hash(message_id))) is not None


def mark_email_processed(session: Session, message_id: str, origin: str | None, leads: int) -> None:
    session.add(ProcessedEmail(message_id_sha256=message_id_hash(message_id), origin=origin, leads_found=leads))
    session.flush()


# ---------------- documents ----------------
def store_document(session: Session, *, kind: str, filename: str, content: bytes,
                   content_type: str | None = None, application_id: int | None = None) -> CandidateDocument:
    sha = hashlib.sha256(content).hexdigest()
    existing = session.scalar(select(CandidateDocument).where(
        CandidateDocument.kind == kind, CandidateDocument.sha256 == sha,
        CandidateDocument.application_id.is_(None) if application_id is None
        else CandidateDocument.application_id == application_id))
    if existing:
        return existing
    ctype = content_type or _guess_type(filename)
    doc = CandidateDocument(kind=kind, filename=filename, content_type=ctype, content=content, sha256=sha,
                            size_bytes=len(content), application_id=application_id)
    session.add(doc)
    session.flush()
    return doc


def _guess_type(filename: str) -> str:
    f = filename.lower()
    if f.endswith(".pdf"):
        return "application/pdf"
    if f.endswith(".docx"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if f.endswith(".png"):
        return "image/png"
    if f.endswith(".txt"):
        return "text/plain"
    return "application/octet-stream"


def latest_document(session: Session, kind: str, application_id: int | None = None) -> CandidateDocument | None:
    q = select(CandidateDocument).where(CandidateDocument.kind == kind)
    if application_id is not None:
        q = q.where(CandidateDocument.application_id == application_id)
    return session.scalar(q.order_by(CandidateDocument.id.desc()).limit(1))


def latest_document_text(session: Session, kind: str) -> str | None:
    doc = latest_document(session, kind)
    if doc is None:
        return None
    if doc.content_type.startswith("text/"):
        return doc.content.decode("utf-8")
    if doc.filename.lower().endswith(".docx"):
        from jobbuddy.tailoring.style import docx_bytes_to_text

        return docx_bytes_to_text(io.BytesIO(doc.content))
    return None


# ---------------- learned field mappings ----------------
def learned_mappings(session: Session, ats: str) -> dict[str, FormFieldMapping]:
    rows = session.scalars(select(FormFieldMapping).where(FormFieldMapping.ats.in_([ats, "*"]))).all()
    out: dict[str, FormFieldMapping] = {}
    for r in sorted(rows, key=lambda r: r.ats != "*"):  # ATS-specific overrides global
        out[r.label_normalized] = r
    return out


def save_learned_mapping(session: Session, *, ats: str, label_normalized: str, profile_key: str | None,
                         static_value: str | None = None, field_type: str | None = None,
                         source: str = "human", confidence: float = 0.95) -> FormFieldMapping:
    row = session.scalar(select(FormFieldMapping).where(FormFieldMapping.ats == ats,
                                                        FormFieldMapping.label_normalized == label_normalized))
    if row is None:
        row = FormFieldMapping(ats=ats, label_normalized=label_normalized)
        session.add(row)
    row.profile_key = profile_key
    row.static_value = static_value
    row.field_type = field_type
    row.source = source
    row.confidence = confidence
    row.times_confirmed = (row.times_confirmed or 0) + 1
    session.flush()
    return row


def touch_mappings(session: Session, rows: Iterable[FormFieldMapping]) -> None:
    now = utcnow()
    for r in rows:
        r.times_used = (r.times_used or 0) + 1
        r.last_used_at = now


# ---------------- misc ----------------
def record_llm_usage(session: Session, **kw: Any) -> None:
    session.add(LLMUsage(**kw))


def kv_get(session: Session, key: str) -> str | None:
    row = session.get(KeyValue, key)
    return row.value if row else None


def kv_set(session: Session, key: str, value: str) -> None:
    row = session.get(KeyValue, key)
    if row is None:
        session.add(KeyValue(key=key, value=value, updated_at=datetime.now(timezone.utc)))
    else:
        row.value = value
