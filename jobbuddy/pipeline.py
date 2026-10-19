"""CI pipeline stages (GitHub Actions: `python -m jobbuddy.cli run-ci`). No LLM calls anywhere in here.

CI does the mechanical work:        fetch (ATS APIs) -> hard filter -> embed (local MiniLM) -> fill forms
                                    -> Telegram card -> poll callbacks -> submit approved
The on-box agent does the judgement: score ('filtered' jobs) -> tailor ('scored' apps) -> answer
                                    field_questions ('needs_answers' apps)   (see jobbuddy/agent_io.py)

jobs:          found -> filtered (waits for agent score) -> scored | below_threshold | filtered_out
applications:  scored (waits for agent tailoring) -> tailored -> [needs_answers -> tailored] -> filled
               -> awaiting_approval -> approved -> submitted        (paused / failed / skipped: side exits)

`run_ci` uses ONE Session for the whole run and commits after each stage.
"""
from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from jobbuddy.config import AppConfig, PROJECT_ROOT, Settings, get_settings, load_app_config
from jobbuddy.db import repository as repo
from jobbuddy.db.models import (Application, Company, FieldQuestion, Job, JobScore, PipelineRun,
                                utcnow)
from jobbuddy.db.states import AppStatus, JobStatus
from jobbuddy.embeddings import Embedder, build_embedder
from jobbuddy.fetchers.base import CompanyTarget, PoliteHttp, load_companies
from jobbuddy.fetchers.email_alerts import ALERT_SOURCES
from jobbuddy.matching.filters import apply_filters
from jobbuddy.matching.similarity import embed_profile, job_text
from jobbuddy.notifiers.base import ApprovalRequest, Decision, Notifier, NullNotifier, build_notifier
from jobbuddy.profile import Profile, load_profile, load_style_sample
from jobbuddy.vector_store import get_vector_store

log = logging.getLogger(__name__)


@dataclass
class Runtime:
    settings: Settings
    cfg: AppConfig
    profile: Profile
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    workdir: Path = field(default_factory=lambda: Path(tempfile.mkdtemp(prefix="jobbuddy-")))
    notifier: Notifier = field(default_factory=NullNotifier)
    _embedder: Embedder | None = None
    started: float = field(default_factory=time.monotonic)
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = build_embedder(self.settings.embedding_model)
        return self._embedder

    def time_left(self) -> float:
        return self.cfg.pipeline.run_time_budget_minutes * 60 - (time.monotonic() - self.started)


def build_runtime(settings: Settings | None = None, *, session: Session | None = None,
                  embedder: Embedder | None = None, notifier: Notifier | None = None,
                  profile: Profile | None = None) -> Runtime:
    settings = settings or get_settings()
    cfg = load_app_config(settings=settings)
    profile = profile or load_profile()
    return Runtime(settings=settings, cfg=cfg, profile=profile, _embedder=embedder,
                   notifier=notifier or build_notifier(settings))


# =============================== fetch ===============================
FETCH_MIN_SECONDS_LEFT = 150     # stop starting new boards when the run budget is nearly spent
FILL_MIN_SECONDS_LEFT = 120      # stop starting new fills when the fill slice / run budget is nearly spent
def sync_companies(session: Session, targets: list[CompanyTarget]) -> dict[str, Company]:
    out = {}
    for t in targets:
        out[t.key] = repo.upsert_company(session, name=t.name, ats=t.ats, board_token=t.key,
                                         careers_url=t.careers_url,
                                         ats_config=t.workday.model_dump() if t.workday else None, active=t.active)
    return out


async def stage_fetch(session: Session, rt: Runtime, http: PoliteHttp | None = None) -> dict[str, int]:
    """Public ATS job-board APIs only (companies.yaml). Alert e-mails are ingested by the on-box agent."""
    from jobbuddy.fetchers.registry import build_fetcher

    fc = rt.cfg.fetch
    targets = [t for t in load_companies(rt.settings.companies_path) if t.active]
    companies = sync_companies(session, targets)
    stats = {"companies": len(targets), "new": 0, "updated": 0, "unchanged": 0, "errors": 0, "skipped_time": 0}
    own_http = http is None
    http = http or PoliteHttp(fc.user_agent, fc.request_delay_seconds, fc.max_retries, fc.timeout_seconds)
    try:
        for i, t in enumerate(targets):
            if rt.time_left() < FETCH_MIN_SECONDS_LEFT:
                stats["skipped_time"] = len(targets) - i
                log.info("time budget nearly exhausted; skipping fetch for %d remaining companies", len(targets) - i)
                break
            try:
                postings = await build_fetcher(t.ats, http, fc).fetch(t)
            except Exception as exc:
                stats["errors"] += 1
                log.warning("fetch failed for %s (%s): %s", t.name, t.ats, type(exc).__name__)
                continue
            for k, v in repo.upsert_jobs(session, postings, companies[t.key]).items():
                stats[k] += v
            session.commit()  # each board is durable even if a later one hits the workflow timeout
    finally:
        if own_http:
            await http.__aexit__(None, None, None)
    return stats


# =============================== filter + embed ===============================
def stage_filter_embed(session: Session, rt: Runtime) -> dict[str, int]:
    """Hard filters + local MiniLM embedding + cosine similarity vs. the profile. No LLM.

    Jobs that pass stay in 'filtered' with their similarity recorded: that is the on-box agent's
    score queue (`agent-pull --kind score`, best similarity first)."""
    mc = rt.cfg.matching
    stats = {"filtered_out": 0, "below_threshold": 0, "queued_for_agent": 0}
    jobs = session.scalars(select(Job).where(Job.status == JobStatus.FOUND.value, Job.apply_url.is_not(None),
                                             Job.source.not_in(list(ALERT_SOURCES)))).all()
    passed: list[Job] = []
    for job in jobs:
        fr = apply_filters(job, mc, rt.profile)
        if not fr.passed:
            job.status = JobStatus.FILTERED_OUT.value
            session.add(JobScore(job_id=job.id, filter_passed=False, filter_reasons=fr.reasons))
            stats["filtered_out"] += 1
        else:
            passed.append(job)
    if not passed:
        session.flush()
        return stats
    need = [j for j in passed if j.embedding is None]
    if need:
        vecs = rt.embedder.embed([job_text(j) for j in need])
        store = get_vector_store(session)
        for j, v in zip(need, vecs):
            store.set_embedding(j, v)
        session.flush()
    pvec = embed_profile(rt.embedder, rt.profile)
    sims = get_vector_store(session).similarity_for(pvec, [j.id for j in passed])
    for job in passed:
        sim = sims.get(job.id, 0.0)
        session.add(JobScore(job_id=job.id, filter_passed=True, similarity=sim, embedding_model=rt.embedder.name))
        if sim < mc.similarity_threshold:
            job.status = JobStatus.BELOW_THRESHOLD.value
            stats["below_threshold"] += 1
        else:
            job.status = JobStatus.FILTERED.value
            stats["queued_for_agent"] += 1
    session.flush()
    return stats


# =============================== fill ===============================
def _materialize(doc: Any, workdir: Path) -> Path | None:
    if doc is None:
        return None
    path = workdir / doc.filename
    path.write_bytes(doc.content)
    return path


def agent_answers_for(session: Session, app: Application) -> dict[str, Any]:
    """Answered field_questions -> {field_id: answer, label_normalized: answer} for the mapper."""
    out: dict[str, Any] = {}
    rows = session.scalars(select(FieldQuestion).where(FieldQuestion.application_id == app.id,
                                                       FieldQuestion.status == "answered")).all()
    for q in rows:
        if q.answer in (None, "", []):
            continue
        out[q.field_id] = q.answer
        if q.label_normalized:
            out.setdefault(q.label_normalized, q.answer)
    return out


def resume_file_for(session: Session, rt: Runtime, app: Application) -> Path | None:
    doc = repo.latest_document(session, "tailored_resume", application_id=app.id) or repo.latest_document(session, "resume")
    path = _materialize(doc, rt.workdir / f"app{app.id}")
    if path is None:
        local = PROJECT_ROOT / "profile" / "resume.pdf"
        path = local if local.exists() else None
    return path


def _build_fill_context(session: Session, rt: Runtime, app: Application, adapter: Any) -> Any:
    from jobbuddy.engine.field_mapper import MapperContext
    from jobbuddy.engine.filler import FillContext
    from jobbuddy.engine.human import Human
    from jobbuddy.engine.field_mapper import normalize_label
    from jobbuddy.tailoring.cover_letter import generate_cover_letter
    from jobbuddy.tailoring.questions import Answer, sensitive_key
    from jobbuddy.tailoring.render import render_cover_letter_pdf

    job = app.job
    appdir = rt.workdir / f"app{app.id}"
    appdir.mkdir(parents=True, exist_ok=True)
    style = load_style_sample(rt.profile, session)
    cl_cache: dict[str, Any] = {}

    def cover_text() -> str | None:
        if app.cover_letter_text:
            return app.cover_letter_text
        if "text" not in cl_cache:
            # Normally the agent pushed a letter at tailoring time; this deterministic template is a fallback.
            cl = generate_cover_letter(None, rt.profile, job, style)
            cl_cache["text"] = cl.text
            cl_cache["issues"] = cl.report.summary()
            app.cover_letter_text = cl.text
            repo.log_event(session, app, "cover_letter", None, None, payload={"used_llm": cl.used_llm,
                                                                              "issues": cl.report.summary()[:10]})
        return cl_cache["text"]

    def cover_path() -> Path | None:
        if "path" in cl_cache:
            return cl_cache["path"]
        stored = repo.latest_document(session, "cover_letter", application_id=app.id)
        if stored is not None and stored.filename.lower().endswith(".pdf"):  # uploaded by agent / Mike
            cl_cache["path"] = _materialize(stored, appdir)
            return cl_cache["path"]
        text = cover_text()
        if not text:
            return None
        # Always a PDF rendered from Mike's cover-letter template; never upload the .docx itself.
        p = rt.profile.personal
        pdf = render_cover_letter_pdf(rt.profile, text, appdir / f"{p.first_name}_{p.last_name}_Cover_Letter.docx",
                                      template=repo.cover_letter_template(session))
        cl_cache["path"] = pdf
        if pdf is not None:
            repo.store_document(session, kind="cover_letter", filename=pdf.name, content=pdf.read_bytes(),
                                application_id=app.id)
        return pdf

    tailoring_answers = {normalize_label(q): (v.get("text") if isinstance(v, dict) else v)
                         for q, v in (app.answers or {}).items()}
    ec = rt.cfg.engine

    def answer(question: str, max_chars: int | None) -> Any:
        """Open-ended questions: only answers the agent already wrote (tailoring `answers`). Unknown -> None,
        so the field becomes a field_question for the agent instead of being guessed here."""
        if sensitive_key(question):
            return None
        text = tailoring_answers.get(normalize_label(question))
        if not text:
            return None
        if max_chars and len(text) > max_chars:
            text = text[: max_chars - 1].rsplit(" ", 1)[0] + "…"
        return Answer(text=text, confidence=ec.agent_answer_confidence, source="agent")

    learned = repo.learned_mappings(session, adapter.name)
    mctx = MapperContext(profile=rt.profile, adapter=adapter, job_title=job.title, company=job.company_name or "",
                         learned=learned, overrides=dict(app.human_edits or {}), cover_letter_text=cover_text,
                         answer_question=answer, llm=None, llm_confidence_cap=ec.llm_confidence_cap,
                         agent_answers=agent_answers_for(session, app),
                         agent_answer_confidence=ec.agent_answer_confidence,
                         how_did_you_hear_default=ec.how_did_you_hear_default)
    return FillContext(mapper_ctx=mctx, engine_cfg=ec, screenshot_dir=appdir, application_id=app.id,
                       resume_path=resume_file_for(session, rt, app), cover_letter_path=cover_path,
                       human=Human(tuple(ec.typing_delay_ms), tuple(ec.action_delay_ms)),
                       on_cover_letter_needed=lambda: cover_text())


def _store_shot(session: Session, app: Application, path: str | None, tag: str) -> bytes | None:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    png = p.read_bytes()
    repo.store_screenshot(session, app, tag, png)
    p.unlink(missing_ok=True)  # screenshots live only in the DB (and Mike's Telegram), never on disk/artifacts
    return png


async def stage_fill(session: Session, rt: Runtime, app_ids: list[int] | None = None,
                     page_factory: Any = None) -> dict[str, int]:
    """Fill forms for tailored applications and STOP before submit."""
    from jobbuddy.engine.adapters.base import detect_adapter
    from jobbuddy.engine.browser import open_browser
    from jobbuddy.engine.filler import FormFiller

    pc = rt.cfg.pipeline
    stats = {"filled": 0, "needs_answers": 0, "paused": 0, "failed": 0, "skipped_cap": 0}
    q = select(Application).where(Application.status == AppStatus.TAILORED.value).order_by(Application.id)
    if app_ids:
        q = select(Application).where(Application.id.in_(app_ids), Application.status == AppStatus.TAILORED.value)
    apps = session.scalars(q).all()
    queue = session.scalar(select(func.count(Application.id)).where(
        Application.status.in_([AppStatus.AWAITING_APPROVAL.value, AppStatus.APPROVED.value]))) or 0
    room = max(0, pc.daily_application_cap - repo.submissions_today(session) - queue)
    limit = min(pc.max_fills_per_run, room) if not app_ids else len(apps)
    if len(apps) > limit:
        stats["skipped_cap"] = len(apps) - limit
    apps = apps[:limit]
    if not apps:
        return stats

    fill_started = time.monotonic()

    def fill_time_left() -> float:
        slice_left = pc.fill_time_budget_minutes * 60 - (time.monotonic() - fill_started)
        return min(slice_left, rt.time_left())

    async def process(context: Any) -> None:
        for app in apps:
            if fill_time_left() < FILL_MIN_SECONDS_LEFT:
                log.info("fill time slice nearly exhausted; stopping fills")
                stats["skipped_time"] = stats.get("skipped_time", 0) + 1
                break
            job = app.job
            apply_url = job.apply_url
            adapter = detect_adapter(apply_url or "", app.ats)
            ctx = _build_fill_context(session, rt, app, adapter)
            # Never sit "idle in transaction" while the browser works: Neon kills such connections after
            # 5 minutes, which crashed the whole run (and skipped notify) on 2026-10-18.
            session.commit()
            per_app = max(30.0, min(float(pc.fill_timeout_seconds), fill_time_left() - 30))
            page = await context.new_page()
            try:
                await page.goto(apply_url, wait_until="domcontentloaded",
                                timeout=rt.cfg.engine.navigation_timeout_ms)
                result = await asyncio.wait_for(FormFiller(ctx).run(page), timeout=per_app)
            except Exception as exc:
                timed_out = isinstance(exc, (asyncio.TimeoutError, TimeoutError))
                log.warning("fill %s for app %s: %s", "timed out" if timed_out else "crashed", app.id,
                            type(exc).__name__)
                session.rollback()
                app.error = f"fill timed out after {int(per_app)}s" if timed_out else f"fill: {type(exc).__name__}"
                repo.transition(session, app, AppStatus.FAILED,
                                note="fill timed out" if timed_out else "fill crashed")
                session.commit()
                stats["failed"] += 1
                continue
            finally:
                try:
                    await page.close()
                except Exception:
                    pass
            try:
                _persist_fill(session, rt, app, result, stats)
                session.commit()
            except Exception as exc:  # one app's DB hiccup must not abort the remaining fills or notify
                log.warning("could not save fill for app %s: %s", app.id, type(exc).__name__)
                session.rollback()
                stats["failed"] += 1

    if page_factory is not None:
        await process(page_factory)
    else:
        async with open_browser(headless=rt.cfg.engine.headless,
                                timeout_ms=rt.cfg.engine.navigation_timeout_ms) as context:
            await process(context)
    return stats


AGENT_UNANSWERABLE_REASONS = ("consent", "resume text field", "password", "cover letter could not")


def queue_field_questions(session: Session, app: Application, result: Any) -> int:
    """Insert 'pending' field_questions for fields the deterministic mapper could not fill and the agent
    may answer. Legal/demographic/consent fields and profile TODOs are never queued: those stay on the
    approval card for Mike. Fields that already have a row (answered/dismissed/pending) are not re-asked."""
    from jobbuddy.engine.field_mapper import normalize_label
    from jobbuddy.tailoring.questions import sensitive_key

    existing = {q.field_id for q in session.scalars(select(FieldQuestion)
                                                    .where(FieldQuestion.application_id == app.id)).all()}
    new = 0
    for step in result.steps:
        by_id = {m.field_id: m for m in step.mappings}
        for f in step.fields:
            m = by_id.get(f.field_id)
            label = (f.label or "").strip()
            if m is None or m.value not in (None, "", []) or not label or f.field_id in existing:
                continue
            if f.type in ("file", "password", "hidden") or sensitive_key(label):
                continue
            if m.profile_key or m.source in ("profile_todo", "human"):
                continue  # profile-driven gap: Mike fixes profile.yaml or edits on the card
            if any(r in (m.reason or "").lower() for r in AGENT_UNANSWERABLE_REASONS):
                continue
            open_ended = f.type == "textarea" and len(label) >= 12
            if not (f.required or open_ended):
                continue
            session.add(FieldQuestion(application_id=app.id, ats=app.ats, field_id=f.field_id, label=label[:2000],
                                      label_normalized=normalize_label(label)[:512], field_type=f.type,
                                      required=bool(f.required), options=[o.label for o in f.options][:60] or None,
                                      max_length=f.max_length, page_url=(step.url or "")[:2048] or None,
                                      status="pending"))
            existing.add(f.field_id)
            new += 1
    session.flush()
    return new


def _persist_fill(session: Session, rt: Runtime, app: Application, result: Any, stats: dict[str, int]) -> None:
    app.filled_state = result.filled_state()
    app.low_confidence = result.low_confidence
    blocked_by_human_check = result.status == "paused" and bool(result.blockers)
    new_questions = 0 if blocked_by_human_check or result.status == "failed" else \
        queue_field_questions(session, app, result)
    if new_questions:
        # Unknown fields: hand them to the on-box agent; the next CI run refills once they are answered.
        _store_shot(session, app, result.screenshot_path, "needs_answers")
        repo.transition(session, app, AppStatus.NEEDS_ANSWERS,
                        note=f"{new_questions} field question(s) queued for the agent",
                        payload={"fill_status": result.status})
        stats["needs_answers"] = stats.get("needs_answers", 0) + 1
        return
    png = _store_shot(session, app, result.screenshot_path, "filled" if result.status == "awaiting_approval"
                      else result.status)
    if result.status == "awaiting_approval":
        repo.transition(session, app, AppStatus.FILLED, note=f"{len(result.steps)} step(s) filled")
        repo.transition(session, app, AppStatus.AWAITING_APPROVAL,
                        note=("stopped before final submit" if not result.required_empty else
                              f"stopped before final submit; {len(result.required_empty)} required field(s) "
                              "still empty (listed on the card, submit blocked)"),
                        payload={"required_empty": result.required_empty[:20]} if result.required_empty else None)
        app.notified_at = None
        stats["filled"] += 1
    elif result.status == "paused":
        app.pause_reason = (result.error or "human needed")[:255]
        repo.transition(session, app, AppStatus.PAUSED, note=app.pause_reason,
                        payload={"blockers": [b.model_dump() for b in result.blockers]})
        _alert(rt, f"#{app.id} {app.job.title} @ {app.job.company_name}: paused - {app.pause_reason}. "
                   f"Open the posting yourself; jobbuddy never solves CAPTCHAs or logs in.", png)
        stats["paused"] += 1
    else:
        app.error = (result.error or "fill failed")[:2000]
        repo.transition(session, app, AppStatus.FAILED, note=app.error[:255])
        stats["failed"] += 1


def _alert(rt: Runtime, text: str, png: bytes | None = None) -> None:
    if rt.cfg.pipeline.dry_run:
        log.info("[dry-run] alert suppressed")
        return
    try:
        rt.notifier.send_alert(text, png)
    except Exception as exc:
        log.warning("alert failed: %s", type(exc).__name__)


def stage_manual_digest(session: Session, rt: Runtime, max_items: int = 15) -> dict[str, int]:
    """Telegram digest of 'manual_apply' leads (unresolved Wellfound alerts). jobbuddy never opens these
    links: Mike applies by hand. Hard filters still apply, so off-target leads are not sent."""
    stats = {"digested": 0, "filtered_out": 0}
    jobs = session.scalars(select(Job).where(Job.status == JobStatus.MANUAL_APPLY.value).order_by(Job.id)).all()
    fresh = [j for j in jobs if not (j.lead_meta or {}).get("digest_sent")]
    lines: list[Job] = []
    for job in fresh:
        fr = apply_filters(job, rt.cfg.matching, rt.profile)
        if not fr.passed:
            job.status = JobStatus.FILTERED_OUT.value
            session.add(JobScore(job_id=job.id, filter_passed=False, filter_reasons=fr.reasons))
            stats["filtered_out"] += 1
            continue
        lines.append(job)
    lines = lines[:max_items]
    if not lines or rt.cfg.pipeline.dry_run:
        session.flush()
        return stats
    text = "Apply by hand (Wellfound leads with no public ATS posting found):\n" + "\n".join(
        _digest_line(j) for j in lines)
    try:
        rt.notifier.send_alert(text, None)
    except Exception as exc:
        log.warning("manual-apply digest failed: %s", type(exc).__name__)
        return stats
    for job in lines:
        job.lead_meta = {**(job.lead_meta or {}), "digest_sent": True, "digest_sent_at": utcnow().isoformat()}
    stats["digested"] = len(lines)
    session.flush()
    return stats


def _digest_line(job: Job) -> str:
    meta = job.lead_meta or {}
    bits = [f"• {job.title} @ {job.company_name or '?'}"]
    if job.location:
        bits.append(job.location)
    if job.salary_min or job.salary_max:
        lo, hi = job.salary_min, job.salary_max
        bits.append(f"${(lo or hi) // 1000}k-${(hi or lo) // 1000}k" if lo != hi else f"${lo // 1000}k")
    if meta.get("equity_min_pct") is not None:
        a, b = meta["equity_min_pct"], meta.get("equity_max_pct")
        bits.append(f"{a:g}%-{b:g}% equity" if b and b != a else f"{a:g}% equity")
    return " · ".join(bits) + f"\n  {job.url or meta.get('wellfound_url') or ''}"


# =============================== approvals ===============================
def stage_notify(session: Session, rt: Runtime) -> dict[str, int]:
    stats = {"notified": 0}
    apps = session.scalars(select(Application).where(Application.status == AppStatus.AWAITING_APPROVAL.value,
                                                     Application.notified_at.is_(None))).all()
    for app in apps:
        if rt.cfg.pipeline.dry_run:
            continue
        shot = repo.latest_screenshot(session, app.id)
        score = session.scalar(select(JobScore.llm_score).where(JobScore.job_id == app.job_id)
                               .order_by(JobScore.id.desc()).limit(1))
        nonce = repo.issue_nonce(app)
        req = ApprovalRequest(application_id=app.id, nonce=nonce, job_title=app.job.title,
                              company=app.job.company_name, job_url=app.job.url, score=score,
                              screenshot_png=shot.png if shot else None, low_confidence=app.low_confidence or [],
                              cover_letter_excerpt=(app.cover_letter_text or "")[:300] or None)
        try:
            rt.notifier.send_approval_request(req)
        except Exception as exc:
            log.warning("approval notification failed for app %s: %s", app.id, type(exc).__name__)
            continue
        app.notified_at = utcnow()
        repo.log_event(session, app, "notify", app.status, app.status, note=f"sent via {rt.notifier.name}")
        stats["notified"] += 1
    session.flush()
    return stats


def _save_learned_from_edits(session: Session, app: Application, edits: dict[str, Any]) -> None:
    """Human edits teach the mapper: label -> static value, reused on future forms of the same ATS."""
    from jobbuddy.engine.field_mapper import normalize_label

    labels = {}
    for step in (app.filled_state or {}).get("steps", []):
        for f in step.get("fields", []):
            labels[f["field_id"]] = (f.get("label") or "", f.get("type"))
    for fid, value in edits.items():
        label, ftype = labels.get(fid, (fid, None))
        if not label or value in (None, ""):
            continue
        # Only learn non-sensitive, reusable answers.
        from jobbuddy.tailoring.questions import sensitive_key

        if sensitive_key(label) and not label.lower().startswith(("how did you hear",)):
            continue
        repo.save_learned_mapping(session, ats=app.ats or "generic", label_normalized=normalize_label(label),
                                  profile_key=None, static_value=str(value), field_type=ftype, source="human")


EDIT_PROMPTS_KEY = "telegram_edit_prompts"  # {prompt message_id: {"app_id": int, "fields": {"1": field_id}}}


def _load_edit_prompts(session: Session) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(repo.kv_get(session, EDIT_PROMPTS_KEY) or "{}")
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _fmt_value(v: Any) -> str:
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return "" if v is None else str(v)


def edit_field_items(app: Application) -> list[tuple[int, str, str, str]]:
    """(number, field_id, label, current value) for every filled field, human edits taking precedence."""
    edits = dict(app.human_edits or {})
    out: list[tuple[int, str, str, str]] = []
    seen: set[str] = set()
    for step in (app.filled_state or {}).get("steps", []):
        maps = {m.get("field_id"): m for m in step.get("mappings", []) if isinstance(m, dict)}
        for f in step.get("fields", []):
            fid = f.get("field_id")
            if not fid or fid in seen:
                continue
            seen.add(fid)
            m = maps.get(fid) or {}
            if fid in edits:
                value = _fmt_value(edits[fid])
            elif m.get("file_kind"):
                value = f"({m['file_kind'].replace('_', ' ')} file)"
            else:
                value = _fmt_value(m.get("value"))
            out.append((len(out) + 1, fid, (f.get("label") or fid).strip(), value[:200]))
    return out


def _send_edit_prompt(session: Session, rt: Runtime, prompts: dict[str, dict[str, Any]], app: Application) -> None:
    send = getattr(rt.notifier, "send_edit_prompt", None)
    if send is None:
        return
    items = edit_field_items(app)
    job = app.job
    title = f"{job.title} @ {job.company_name}" if job is not None else ""
    try:
        ids = send(app.id, title, [(n, label, value) for n, _fid, label, value in items])
    except Exception as exc:
        log.warning("sending edit prompt failed: %s", type(exc).__name__)
        return
    for k in [k for k, v in prompts.items() if v.get("app_id") == app.id]:
        del prompts[k]  # only the newest prompt per app is live
    fields = {str(n): fid for n, fid, _l, _v in items}
    for mid in ids:
        prompts[str(mid)] = {"app_id": app.id, "fields": fields}
    repo.log_event(session, app, "edit_prompt", app.status, app.status, actor="telegram",
                   payload={"fields": len(fields)})


def _resolve_numbered_edit(session: Session, rt: Runtime, prompts: dict[str, dict[str, Any]],
                           d: Decision) -> Decision | None:
    """Map a `<n> = value` reply to field_ids via the stored prompt. Never approves anything."""
    prompt = prompts.get(str(d.reply_to_message_id)) if d.reply_to_message_id is not None else None
    if prompt is None:
        apps = {v.get("app_id") for v in prompts.values()}
        if len(apps) != 1:
            if prompts or d.reply_to_message_id is None:
                _alert(rt, "Couldn't tell which application that edit is for: reply directly to the edit prompt "
                           "(or use `#<id> <field_id> = <value>`).")
            return None
        prompt = next(iter(prompts.values()))
    app_id = int(prompt["app_id"])
    fields = prompt.get("fields") or {}
    edits = {fields[str(n)]: v for n, v in d.numbered_edits.items() if str(n) in fields}
    bad = sorted(n for n in d.numbered_edits if str(n) not in fields)
    if bad:
        _alert(rt, f"#{app_id}: ignored unknown field number(s) {', '.join(map(str, bad))}.")
    if not edits:
        return None
    for k in [k for k, v in prompts.items() if v.get("app_id") == app_id]:
        del prompts[k]  # prompt consumed; tap Edit on the new card to edit again
    return Decision(application_id=app_id, action="edit", via=d.via, edits=edits,
                    telegram_user_id=d.telegram_user_id)


def stage_decisions(session: Session, rt: Runtime) -> dict[str, int]:
    """Poll Telegram (etc.), INSERT approvals rows, then reconcile approvals + dashboard events."""
    state = {"telegram_offset": repo.kv_get(session, "telegram_offset") or "0",
             "slack_oldest": repo.kv_get(session, "slack_oldest") or "0"}
    inserted = rejected = 0
    try:
        decisions = rt.notifier.poll_decisions(state)
    except Exception as exc:
        log.warning("polling decisions failed: %s", type(exc).__name__)
        decisions = []
    prompts = _load_edit_prompts(session)
    edit_taps: list[Application] = []
    for d in decisions:
        if d.numbered_edits:
            d = _resolve_numbered_edit(session, rt, prompts, d)
            if d is None:
                rejected += 1
                continue
        app = session.get(Application, d.application_id)
        if app is None:
            rejected += 1
            continue
        if d.nonce:  # button press: must match the app's current single-use nonce
            row = repo.record_decision(session, app, decision=d.action, via=d.via, nonce=d.nonce,
                                       telegram_user_id=d.telegram_user_id, require_nonce_match=True)
        elif d.action == "edit" and d.edits:
            row = repo.record_decision(session, app, decision="edit", via=d.via, edits=d.edits,
                                       telegram_user_id=d.telegram_user_id)
        else:
            row = None
        if row is None:
            rejected += 1
        else:
            inserted += 1
            if d.nonce and d.action == "edit":
                edit_taps.append(app)
    for app in edit_taps:
        _send_edit_prompt(session, rt, prompts, app)
    for k, v in state.items():
        repo.kv_set(session, k, v)
    repo.kv_set(session, EDIT_PROMPTS_KEY, json.dumps(prompts))
    stats = repo.reconcile(session, save_learned=lambda a, e: _save_learned_from_edits(session, a, e))
    stats.update({"decisions_inserted": inserted, "decisions_rejected": rejected})
    session.flush()
    return stats


# =============================== submit ===============================
def submit_failure_message(app: Application, result: Any) -> str:
    """Telegram text after a submit attempt that did not confirm: says plainly whether it went through."""
    job = app.job
    link = (job.url or job.apply_url or "") if job is not None else ""
    head = f"#{app.id} {job.title} @ {job.company_name}" if job is not None else f"#{app.id}"
    likely = getattr(result, "likely_submitted", None) if result is not None else None
    reason = (getattr(result, "error", None) if result is not None else None) or app.error or "submit failed"
    if likely is False:
        verdict = "❌ NOT submitted (it most likely did not go through). Apply manually here:"
    else:
        verdict = ("⚠️ Submit outcome UNCLEAR. It may or may not have gone through: check your email for a "
                   "confirmation before re-applying. Job link:")
    return f"{head}\n{verdict}\n{link}\nWhy: {reason[:300]}\njobbuddy will not retry this automatically."


async def stage_submit(session: Session, rt: Runtime, *, confirm: bool = False, app_ids: list[int] | None = None,
                       page_factory: Any = None) -> dict[str, int]:
    """Submits APPROVED applications. Requires AUTO_SUBMIT=true (cron) or an explicit --confirm (CLI)."""
    from jobbuddy.engine.adapters.base import detect_adapter
    from jobbuddy.engine.browser import open_browser
    from jobbuddy.engine.filler import FormFiller
    from jobbuddy.engine.submitter import SubmissionRefused, issue_token

    pc = rt.cfg.pipeline
    stats = {"submitted": 0, "refused": 0, "paused": 0, "failed": 0}
    if not (pc.auto_submit or confirm):
        log.info("submission disabled (AUTO_SUBMIT=false and no --confirm)")
        return stats
    if pc.dry_run:
        log.info("dry-run: no submissions")
        return stats
    q = select(Application).where(Application.status == AppStatus.APPROVED.value).order_by(Application.approved_at)
    if app_ids:
        q = q.where(Application.id.in_(app_ids))
    apps = session.scalars(q).all()
    if not apps:
        return stats

    async def process(context: Any) -> None:
        for app in apps:
            if rt.time_left() < 120:
                break
            try:
                token = issue_token(session, app, daily_cap=pc.daily_application_cap, dry_run=pc.dry_run)
            except SubmissionRefused as exc:
                log.info("submit refused for app %s: %s", app.id, exc)
                stats["refused"] += 1
                continue
            adapter = detect_adapter(app.job.apply_url or "", app.ats)
            ctx = _build_fill_context(session, rt, app, adapter)
            page = await context.new_page()
            try:
                await page.goto(app.job.apply_url, wait_until="domcontentloaded",
                                timeout=rt.cfg.engine.navigation_timeout_ms)
                result = await FormFiller(ctx).run(page, approval=token)
            except Exception as exc:
                result = None
                app.error = f"submit: {type(exc).__name__}"
            finally:
                await page.close()
            if result is not None:
                png = _store_shot(session, app, result.screenshot_path, result.status)
            else:
                png = None
            if result is not None and result.status == "submitted":
                app.submitted_at = utcnow()
                repo.transition(session, app, AppStatus.SUBMITTED, note=(result.confirmation_text or "")[:255])
                _alert(rt, f"#{app.id} submitted: {app.job.title} @ {app.job.company_name}", png)
                stats["submitted"] += 1
            elif result is not None and result.status == "paused":
                app.pause_reason = (result.error or "human needed")[:255]
                repo.transition(session, app, AppStatus.PAUSED, note=app.pause_reason,
                                payload={"submit_evidence": result.submit_evidence} if result.submit_evidence else None)
                _alert(rt, submit_failure_message(app, result), png)
                stats["paused"] += 1
            else:
                # Never auto-retry: the click may have gone through. Mike verifies manually.
                app.error = (result.error if result else app.error) or "submit failed"
                repo.transition(session, app, AppStatus.FAILED, note=app.error[:255],
                                payload={"submit_evidence": result.submit_evidence}
                                if result is not None and result.submit_evidence else None)
                _alert(rt, submit_failure_message(app, result), png)
                stats["failed"] += 1
            session.commit()

    if page_factory is not None:
        await process(page_factory)
    else:
        async with open_browser(headless=rt.cfg.engine.headless,
                                timeout_ms=rt.cfg.engine.navigation_timeout_ms) as context:
            await process(context)
    return stats


# =============================== report ===============================
def build_report(session: Session) -> dict[str, Any]:
    jobs = dict(session.execute(select(Job.status, func.count(Job.id)).group_by(Job.status)).all())
    apps = dict(session.execute(select(Application.status, func.count(Application.id))
                                .group_by(Application.status)).all())
    outcomes = dict(session.execute(select(Application.outcome, func.count(Application.id))
                                    .where(Application.submitted_at.is_not(None))
                                    .group_by(Application.outcome)).all())
    submitted = sum(outcomes.values())
    responded = sum(v for k, v in outcomes.items() if k in ("interview", "rejected", "offer"))
    pending_q = session.scalar(select(func.count(FieldQuestion.id)).where(FieldQuestion.status == "pending")) or 0
    return {
        "jobs_by_status": jobs,
        "applications_by_status": apps,
        "outcomes_of_submitted": outcomes,
        "submitted_total": submitted,
        "response_rate": round(responded / submitted, 3) if submitted else None,
        "interview_rate": round((outcomes.get("interview", 0) + outcomes.get("offer", 0)) / submitted, 3)
        if submitted else None,
        "agent_queues": {"score": int(jobs.get(JobStatus.FILTERED.value, 0)),
                         "tailor": int(apps.get(AppStatus.SCORED.value, 0)),
                         "questions": int(pending_q)},
    }


# =============================== run-ci ===============================
async def run_ci(session: Session, rt: Runtime, trigger: str = "cron") -> dict[str, Any]:
    """GitHub Actions entrypoint. Order: decisions -> notify -> fill -> notify -> submit -> fetch -> filter/embed ->
    manual-apply digest -> report.

    Ready ('tailored') applications are filled and their approval cards sent FIRST, inside
    pipeline.fill_time_budget_minutes, so a slow fetch can never starve them; fetch/filter use the remaining
    budget (newly tailored apps from this run's fetch are filled next run anyway, after the on-box agent
    scores and tailors them). Submit only acts on Telegram/dashboard-approved applications and only when
    AUTO_SUBMIT=true."""
    run = PipelineRun(id=rt.run_id, trigger=trigger)
    session.add(run)
    session.commit()
    stats: dict[str, Any] = {}
    try:
        stats["decisions"] = stage_decisions(session, rt)
        session.commit()
        # 1) ready applications first: cards still owed from an earlier run -> fill (own time slice) ->
        #    approval cards -> approved submissions
        stats["notify_pending"] = stage_notify(session, rt)
        session.commit()
        stats["fill"] = await stage_fill(session, rt)
        session.commit()
        stats["notify"] = stage_notify(session, rt)
        session.commit()
        if rt.time_left() > 180:
            stats["submit"] = await stage_submit(session, rt)
            session.commit()
        # 2) discovery with whatever budget remains
        if rt.time_left() > FETCH_MIN_SECONDS_LEFT:
            stats["fetch"] = await stage_fetch(session, rt)
            session.commit()
        stats["filter_embed"] = stage_filter_embed(session, rt)
        session.commit()
        stats["manual_digest"] = stage_manual_digest(session, rt)
        session.commit()
        stats["report"] = build_report(session)
    except Exception as exc:
        session.rollback()
        run.error = type(exc).__name__
        raise
    finally:
        run.finished_at = datetime.now(timezone.utc)
        run.stats = _jsonable(stats)
        session.merge(run)
        session.commit()
    return stats


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (int, float, str, bool)) or obj is None:
        return obj
    return str(obj)
