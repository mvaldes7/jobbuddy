"""jobbuddy CLI.

CI (GitHub Actions):   python -m jobbuddy.cli run-ci
On-box AI agent:       agent-pull / validate-tailoring / validate-answers / agent-push-* / ingest-alert-email
                       (Neon SQL-over-HTTPS when DATABASE_TRANSPORT=neon_http or DATABASE_URL is Postgres)
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

import typer
from sqlalchemy import select

from jobbuddy.config import PROJECT_ROOT, get_settings
from jobbuddy.logging_utils import setup_logging

app = typer.Typer(add_completion=False, help="Human-in-the-loop job application pipeline.")
log = logging.getLogger("jobbuddy")


def _setup(profile_needed: bool = True):  # noqa: ANN202
    settings = get_settings()
    profile = None
    if profile_needed:
        from jobbuddy.profile import load_profile

        profile = load_profile()
    setup_logging(pii_literals=profile.pii_literals() if profile else ())
    return settings, profile


def _runtime(session, profile):  # noqa: ANN001, ANN202
    from jobbuddy.pipeline import build_runtime

    return build_runtime(session=session, profile=profile)


def _echo(obj) -> None:  # noqa: ANN001
    typer.echo(json.dumps(obj, indent=2, default=str))


@app.command()
def fetch() -> None:
    """Fetch jobs from the public ATS boards in companies.yaml (idempotent upserts)."""
    from jobbuddy.db.session import session_scope
    from jobbuddy.pipeline import stage_fetch

    _, profile = _setup()
    with session_scope() as s:
        _echo(asyncio.run(stage_fetch(s, _runtime(s, profile))))


@app.command("filter")
def filter_cmd() -> None:
    """Hard filters + local embedding similarity. Passing jobs wait in 'filtered' for the agent's score."""
    from jobbuddy.db.session import session_scope
    from jobbuddy.pipeline import stage_filter_embed

    _, profile = _setup()
    with session_scope() as s:
        _echo(stage_filter_embed(s, _runtime(s, profile)))


@app.command()
def fill(app_id: list[int] = typer.Option(None, "--app"),
         headed: bool = typer.Option(False, help="Show the browser window")) -> None:
    """Fill application forms and STOP before the final submit (screenshots go to the DB + Telegram)."""
    from jobbuddy.db.session import session_scope
    from jobbuddy.pipeline import stage_fill, stage_notify

    _, profile = _setup()
    with session_scope() as s:
        rt = _runtime(s, profile)
        if headed:
            rt.cfg.engine.headless = False
        out = asyncio.run(stage_fill(s, rt, app_id or None))
        out["notify"] = stage_notify(s, rt)
        _echo(out)


@app.command()
def approve(app_id: int, edit: list[str] = typer.Option(None, "--edit", help="field_id=value (repeatable)"),
            skip: bool = typer.Option(False, help="Skip instead of approve")) -> None:
    """Record Mike's decision from the terminal (inserts an approvals row, then reconciles)."""
    from jobbuddy.db import repository as repo
    from jobbuddy.db.models import Application
    from jobbuddy.db.session import session_scope

    _setup(profile_needed=False)
    edits = dict(e.split("=", 1) for e in (edit or []) if "=" in e)
    with session_scope() as s:
        a = s.get(Application, app_id)
        if a is None:
            raise typer.BadParameter(f"no application {app_id}")
        if skip:
            repo.record_decision(s, a, decision="skip", via="cli")
            repo.reconcile(s)
        elif edits:
            repo.record_decision(s, a, decision="edit", via="cli", edits=edits)
            repo.reconcile(s)
            typer.echo("edits recorded; the form will be re-filled and re-sent for approval")
        else:
            repo.record_approval(s, a, via="cli")
        typer.echo(f"application {app_id}: {a.status}")


@app.command()
def submit(confirm: bool = typer.Option(False, "--confirm", help="Required: really submit approved apps"),
           app_id: list[int] = typer.Option(None, "--app")) -> None:
    """Submit APPROVED applications (each consumes its single-use approval)."""
    from jobbuddy.db.session import session_scope
    from jobbuddy.pipeline import stage_submit

    from jobbuddy.pipeline import SUBMISSION_RETIRED

    if SUBMISSION_RETIRED:
        typer.echo("Submission is retired: jobbuddy sends listings + cover letters to Mike; he applies himself.")
        raise typer.Exit(code=2)
    _, profile = _setup()
    if not confirm:
        typer.echo("Refusing to submit without --confirm (AUTO_SUBMIT is respected only by run-ci).")
        raise typer.Exit(code=2)
    with session_scope() as s:
        _echo(asyncio.run(stage_submit(s, _runtime(s, profile), confirm=True, app_ids=app_id or None)))


@app.command("send-to-user")
def send_to_user_cmd(app_id: list[int] = typer.Option(None, "--app"),
                     backfill: bool = typer.Option(False, "--backfill",
                                                   help="Also send open legacy apps (awaiting_approval/paused/...)")) -> None:
    """Send listing + cover letter PDF to Mike in Telegram and mark the app 'sent_to_user'. Never submits."""
    from jobbuddy.db.session import session_scope
    from jobbuddy.pipeline import SEND_READY_STATUSES, stage_send_to_user

    statuses = SEND_READY_STATUSES + (("needs_answers", "filled", "awaiting_approval", "approved", "paused")
                                      if backfill else ())
    _, profile = _setup()
    with session_scope() as s:
        _echo(stage_send_to_user(s, _runtime(s, profile), app_id or None, statuses=statuses))


@app.command("resend-cards")
def resend_cards_cmd() -> None:
    """Ask the next run to re-send a fresh approval card (new nonce) for every awaiting_approval app."""
    from jobbuddy.db import repository as repo
    from jobbuddy.db.session import session_scope
    from jobbuddy.pipeline import RESEND_CARDS_KEY

    _setup(profile_needed=False)
    with session_scope() as s:
        repo.kv_set(s, RESEND_CARDS_KEY, "1")
    typer.echo("fresh cards will be sent on the next run")


@app.command("run-ci")
def run_ci_cmd(trigger: str = typer.Option("cron")) -> None:
    """CI: decisions -> fetch -> filter/embed -> fill -> notify -> manual digest -> (submit if AUTO_SUBMIT) -> report."""
    from jobbuddy.db.session import session_scope
    from jobbuddy.pipeline import run_ci

    _, profile = _setup()
    with session_scope() as s:
        rt = _runtime(s, profile)
        stats = asyncio.run(run_ci(s, rt, trigger=trigger))
    log.info("run complete: %s", json.dumps(stats.get("report", {}), default=str))


# =============================== on-box agent ===============================
def _agent(transport: Optional[str] = None):  # noqa: ANN202
    """AgentIO over the configured transport. Postgres URLs default to neon_http (the box cannot reach
    port 5432); SQLite (dev/tests) uses SQLAlchemy. DATABASE_TRANSPORT / --transport override."""
    import os

    from jobbuddy.agent_io import AgentIO
    from jobbuddy.config import load_app_config
    from jobbuddy.db.transport import build_transport

    settings, profile = _setup()
    chosen = transport or os.environ.get("DATABASE_TRANSPORT")
    if not chosen:
        chosen = "neon_http" if settings.database_url.startswith("postgres") else "sqlalchemy"
    settings = settings.model_copy(update={"database_transport": chosen})
    return AgentIO(build_transport(settings), profile, load_app_config(settings=settings))


def _read_payload(path: Path):  # noqa: ANN202
    from jobbuddy.agent_io import read_json_file

    try:
        return read_json_file(path)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc))


def _guarded(fn, *args, **kw):  # noqa: ANN001, ANN202
    from pydantic import ValidationError

    from jobbuddy.agent_io import explain_validation_error

    try:
        return fn(*args, **kw)
    except ValidationError as exc:
        typer.echo(json.dumps({"ok": False, "schema_errors": explain_validation_error(exc)}, indent=2))
        raise typer.Exit(code=2)


TRANSPORT_OPT = typer.Option(None, "--transport", help="neon_http | sqlalchemy (default: auto)")


@app.command("agent-pull")
def agent_pull(kind: str = typer.Option(..., "--kind", help="score | tailor | questions"),
               limit: Optional[int] = typer.Option(None, "--limit"), transport: Optional[str] = TRANSPORT_OPT) -> None:
    """Print JSON work items (job descriptions, profile facts, prompt instructions) for the on-box agent."""
    if kind not in ("score", "tailor", "questions"):
        raise typer.BadParameter("kind must be score, tailor or questions")
    _echo(_agent(transport).pull(kind, limit))


@app.command("agent-push-scores")
def agent_push_scores(file: Path, transport: Optional[str] = TRANSPORT_OPT) -> None:
    """Write agent scores (prompts/score_rubric.md). >= threshold creates an application ('scored')."""
    _echo(_guarded(_agent(transport).push_scores, _read_payload(file)))


@app.command("validate-tailoring")
def validate_tailoring(file: Path, transport: Optional[str] = TRANSPORT_OPT) -> None:
    """Fabrication check for tailored bullets, summary, cover letter and answers. Exit 1 on any error."""
    out = _guarded(_agent(transport).validate_tailoring, _read_payload(file))
    _echo(out)
    if not out["ok"]:
        raise typer.Exit(code=1)


@app.command("agent-push-tailoring")
def agent_push_tailoring(file: Path, no_pdf: bool = typer.Option(False, "--no-pdf"),
                         transport: Optional[str] = TRANSPORT_OPT) -> None:
    """Validate, render DOCX/PDF locally, upload to candidate_documents, move applications to 'tailored'."""
    out = _guarded(_agent(transport).push_tailoring, _read_payload(file), render_pdf=not no_pdf)
    _echo(out)
    if out["rejected"]:
        raise typer.Exit(code=1)


@app.command("validate-answers")
def validate_answers(file: Path, transport: Optional[str] = TRANSPORT_OPT) -> None:
    """Check agent answers to field_questions (options, length, fabrication, no sensitive answers)."""
    out = _guarded(_agent(transport).validate_answers, _read_payload(file))
    _echo(out)
    if not out["ok"]:
        raise typer.Exit(code=1)


@app.command("agent-push-answers")
def agent_push_answers(file: Path, transport: Optional[str] = TRANSPORT_OPT) -> None:
    """Store answers; applications with no pending questions go back to 'tailored' for the next CI fill."""
    out = _guarded(_agent(transport).push_answers, _read_payload(file))
    _echo(out)
    if out["rejected"]:
        raise typer.Exit(code=1)


@app.command("ingest-alert-email")
def ingest_alert_email(file: Optional[Path] = typer.Argument(None, help="raw .eml file"),
                       json_file: Optional[Path] = typer.Option(None, "--json",
                                                                help="JSON {from, subject, html, text, date?, message_id?}"),
                       probe: Optional[bool] = typer.Option(None, "--probe/--no-probe",
                                                            help="check public Ashby/Greenhouse/Lever boards"),
                       transport: Optional[str] = TRANSPORT_OPT) -> None:
    """Parse a LinkedIn/Indeed/Wellfound alert e-mail (direct or forwarded) into leads resolved to ATS URLs."""
    import email as _email
    from email import policy

    from jobbuddy.fetchers.email_alerts import message_from_json

    if bool(file) == bool(json_file):
        raise typer.BadParameter("pass exactly one of FILE.eml or --json FILE.json")
    if json_file:
        data = _read_payload(json_file)
        if not isinstance(data, dict):
            raise typer.BadParameter("--json expects an object {from, subject, html, text}")
        msg = message_from_json(data)
    else:
        msg = _email.message_from_bytes(file.read_bytes(), policy=policy.compat32)  # type: ignore[union-attr]
    agent = _agent(transport)

    async def go():  # noqa: ANN202
        from jobbuddy.fetchers.base import PoliteHttp

        fc = agent.cfg.fetch
        do_probe = fc.probe_ats_for_email_leads if probe is None else probe
        if not do_probe:
            return await agent.ingest_alert(msg, probe=False)
        async with PoliteHttp(fc.user_agent, fc.request_delay_seconds, fc.max_retries, fc.timeout_seconds) as http:
            return await agent.ingest_alert(msg, http=http, probe=True)

    _echo(asyncio.run(go()))


@app.command()
def report() -> None:
    """Funnel, statuses and response rates."""
    from jobbuddy.db.session import session_scope
    from jobbuddy.pipeline import build_report

    _setup(profile_needed=False)
    with session_scope() as s:
        _echo(build_report(s))


@app.command()
def outcome(app_id: int, value: str = typer.Argument(..., help="interview|rejected|offer|ghosted|withdrawn|none")) -> None:
    """Report an outcome (appends an event; reconciled into the application)."""
    from jobbuddy.db import repository as repo
    from jobbuddy.db.models import Application
    from jobbuddy.db.session import session_scope

    _setup(profile_needed=False)
    with session_scope() as s:
        a = s.get(Application, app_id)
        if a is None:
            raise typer.BadParameter(f"no application {app_id}")
        repo.report_outcome(s, a, value)
        repo.reconcile(s)
        typer.echo(f"application {app_id}: outcome={a.outcome}")


@app.command("upload-document")
def upload_document(path: Path, kind: str = typer.Option("resume", help="resume | cover_letter_sample")) -> None:
    """Store the resume (or voice sample) in candidate_documents so PII never lives in the repo."""
    from jobbuddy.db import repository as repo
    from jobbuddy.db.session import session_scope

    settings, _ = _setup(profile_needed=False)
    if kind not in ("resume", "cover_letter_sample"):
        raise typer.BadParameter("kind must be resume or cover_letter_sample")
    data = path.read_bytes()
    if settings.database_transport == "neon_http":
        import hashlib

        from jobbuddy.db.neon_http import NeonHttpTransport

        t = NeonHttpTransport(settings.database_url, endpoint=settings.neon_http_endpoint)
        r = t.query("INSERT INTO candidate_documents (kind, filename, content_type, content, sha256, size_bytes, "
                    "created_at) VALUES ($1, $2, $3, $4, $5, $6, CURRENT_TIMESTAMP) RETURNING id",
                    [kind, path.name, repo._guess_type(path.name), data, hashlib.sha256(data).hexdigest(), len(data)])
        typer.echo(f"stored {kind} document id={r.rows[0][0]} ({len(data)} bytes) via neon_http")
        return
    with session_scope() as s:
        doc = repo.store_document(s, kind=kind, filename=path.name, content=data)
        typer.echo(f"stored {kind} document id={doc.id} ({doc.size_bytes} bytes, sha256={doc.sha256[:12]}...)")


@app.command("extract-style")
def extract_style(docx: Path = typer.Option(PROJECT_ROOT / "profile" / "resume_or_cl.docx"),
                  out: Path = typer.Option(PROJECT_ROOT / "profile" / "cover_letter_sample.txt")) -> None:
    """Extract the sample cover letter text (Mike's voice) to profile/cover_letter_sample.txt."""
    from jobbuddy.tailoring.style import extract_cover_letter_sample

    text = extract_cover_letter_sample(docx, out)
    typer.echo(f"wrote {out} ({len(text.split())} words)")


@app.command("check-profile")
def check_profile() -> None:
    """List profile.yaml fields still marked TODO."""
    from jobbuddy.profile import load_profile

    todos = load_profile().todo_fields()
    if not todos:
        typer.echo("profile complete")
    for t in todos:
        typer.echo(f"TODO: {t}")


@app.command("profile-json")
def profile_json(path: Optional[Path] = typer.Option(None)) -> None:
    """Print profile.yaml as compact JSON for the CANDIDATE_PROFILE_JSON secret (pipe it, don't log it)."""
    from jobbuddy.profile import load_profile, profile_to_json

    typer.echo(profile_to_json(load_profile(path or PROJECT_ROOT / "profile" / "profile.yaml")))


@app.command("init-dev-db")
def init_dev_db() -> None:
    """SQLite only: create tables locally. Postgres/Azure SQL: use `alembic upgrade head`."""
    from jobbuddy.db.session import create_all_for_dev, get_engine

    create_all_for_dev(get_engine(get_settings().database_url))
    typer.echo("dev database ready")


@app.command("list")
def list_apps(status: Optional[str] = typer.Option(None)) -> None:
    """List applications (id, status, title, company)."""
    from jobbuddy.db.models import Application
    from jobbuddy.db.session import session_scope

    _setup(profile_needed=False)
    with session_scope() as s:
        q = select(Application).order_by(Application.id)
        if status:
            q = q.where(Application.status == status)
        for a in s.scalars(q):
            typer.echo(f"{a.id:>5}  {a.status:<18} {a.outcome:<10} {a.job.title[:50]:<50} {a.job.company_name or ''}")


if __name__ == "__main__":
    app()
