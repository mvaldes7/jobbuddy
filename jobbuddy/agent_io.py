"""Work-queue I/O for the on-box AI agent.

There is no LLM API in jobbuddy. An AI assistant on Mike's machine runs on a schedule and does the
judgement work through these functions (exposed as `jobbuddy agent-*` CLI commands):

    agent-pull --kind score      jobs.status='filtered'               -> agent-push-scores
    agent-pull --kind tailor     applications.status='scored'         -> validate-tailoring -> agent-push-tailoring
    agent-pull --kind questions  field_questions.status='pending'     -> validate-answers   -> agent-push-answers
    ingest-alert-email           LinkedIn/Indeed alert e-mail (.eml or JSON) -> job leads resolved to ATS URLs

Every statement is plain DML with $n placeholders, run through a SqlTransport (Neon SQL-over-HTTPS on the
box; SQLAlchemy in tests/dev). Multi-statement writes go through `transaction()` (Neon batch = one
transaction) and every write is guarded by the expected current status, so a stale push is a no-op.
Nothing here submits anything or touches a job site; pushes only change rows that CI acts on later.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field, ValidationError, field_validator

from jobbuddy.config import AppConfig, PROJECT_ROOT
from jobbuddy.db.transport import SqlTransport, as_bool, as_float, as_int, as_json, cast
from jobbuddy.profile import Profile, is_todo

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
AGENT_MODEL_TAG = "onbox-agent"
CHOICE_TYPES = ("select", "custom_select", "radio", "checkbox_group")


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ts_param(t: SqlTransport, dt: datetime | None) -> str | None:
    """Timestamps as parameters: ISO-8601 for Postgres; SQLAlchemy's naive-UTC text format for SQLite."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
    if t.dialect == "sqlite":
        return dt.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S.%f")
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()


def agent_profile(profile: Profile) -> dict[str, Any]:
    """Facts for the agent. Bullet ids are included (tailoring must cite them); contact details are not."""
    return {
        "full_name": profile.personal.full_name,
        "headline": profile.headline,
        "years_experience": profile.years_experience,
        "summary": profile.summary,
        "experience": [
            {"company": e.company, "location": e.location,
             "roles": [{"title": r.title, "start": r.start, "end": r.end,
                        "bullets": [{"id": b.id, "text": " ".join(b.text.split())} for b in r.bullets]}
                       for r in e.roles]}
            for e in profile.experience
        ],
        "education": [f"{ed.degree}, {ed.school} ({ed.year})" for ed in profile.education],
        "certifications": [f"{c.name} - {c.issuer} ({c.year})" for c in profile.certifications],
        "skills": list(profile.skills),
        "targets": {"titles": profile.targets.titles, "locations": profile.targets.locations},
    }


# =============================== payload models ===============================
class ScoreItem(BaseModel):
    job_id: int
    score: int = Field(ge=0, le=100)
    reasons: list[str] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    seniority_fit: str | None = None
    domain_fit: str | None = None

    @field_validator("score", mode="before")
    @classmethod
    def _num(cls, v: Any) -> int:
        try:
            return int(round(float(v)))
        except (TypeError, ValueError):
            raise ValueError("score must be numeric")


class ScoresPayload(BaseModel):
    scores: list[ScoreItem]


class BulletIn(BaseModel):
    source_id: str
    text: str


class TailoringItem(BaseModel):
    application_id: int
    summary: str | None = None
    skills_order: list[str] = Field(default_factory=list)
    bullets: list[BulletIn] = Field(default_factory=list)
    cover_letter: str | None = None
    answers: dict[str, str] = Field(default_factory=dict)


class TailoringPayload(BaseModel):
    items: list[TailoringItem]


class AnswerItem(BaseModel):
    question_id: int
    answer: str | list[str] | bool | None = None
    skip: bool = False
    reusable: bool = False
    notes: str | None = None


class AnswersPayload(BaseModel):
    answers: list[AnswerItem]


def _coerce_payload(model: type[BaseModel], data: Any, list_key: str) -> Any:
    if isinstance(data, list):
        data = {list_key: data}
    return model.model_validate(data)


# =============================== AgentIO ===============================
class AgentIO:
    def __init__(self, transport: SqlTransport, profile: Profile, cfg: AppConfig,
                 workdir: Path | None = None, style_sample: str | None = None) -> None:
        self.t = transport
        self.profile = profile
        self.cfg = cfg
        self.workdir = workdir or Path(tempfile.mkdtemp(prefix="jobbuddy-agent-"))
        self._style = style_sample

    # ---------------- helpers ----------------
    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        return self.t.query(sql, list(params)).dicts()

    def cover_letter_template(self) -> Any:
        """Mike's cover-letter .docx: local profile/cover_letter_template.docx, else the latest .docx stored
        as kind 'cover_letter_sample' in candidate_documents."""
        from jobbuddy.tailoring.render import COVER_LETTER_TEMPLATE

        if COVER_LETTER_TEMPLATE.exists():
            return COVER_LETTER_TEMPLATE
        rows = self._rows("SELECT content FROM candidate_documents WHERE kind = 'cover_letter_sample' "
                          "AND lower(filename) LIKE '%.docx' ORDER BY id DESC LIMIT 1")
        if not rows:
            return None
        from jobbuddy.db.neon_http import decode_bytea

        return decode_bytea(rows[0]["content"])

    def style_sample(self) -> str:
        """Voice sample: profile inline text -> local profile file -> candidate_documents (via transport)."""
        if self._style is not None:
            return self._style
        from jobbuddy.profile import load_style_sample

        txt = load_style_sample(self.profile)
        if not txt:
            rows = self._rows("SELECT filename, content FROM candidate_documents WHERE kind = 'cover_letter_sample' "
                              "ORDER BY id DESC LIMIT 1")
            if rows:
                from jobbuddy.db.neon_http import decode_bytea

                data = decode_bytea(rows[0]["content"]) or b""
                if str(rows[0]["filename"]).lower().endswith(".docx"):
                    import io

                    from jobbuddy.tailoring.style import docx_bytes_to_text

                    txt = docx_bytes_to_text(io.BytesIO(data))
                else:
                    txt = data.decode("utf-8", errors="replace")
        self._style = (txt or "").strip()
        return self._style

    def _trim(self, text: Any, n: int | None = None) -> str:
        s = str(text or "")
        n = n or self.cfg.agent.max_description_chars
        return s if len(s) <= n else s[:n] + " …[truncated]"

    def _job_context(self, application_id: int) -> dict[str, Any] | None:
        rows = self._rows(
            "SELECT a.id AS application_id, a.status, a.ats, j.id AS job_id, j.title, j.company_name, "
            "j.location, j.description FROM applications a JOIN jobs j ON j.id = a.job_id WHERE a.id = $1",
            [application_id])
        return rows[0] if rows else None

    # =============================== pull ===============================
    def pull(self, kind: str, limit: int | None = None) -> dict[str, Any]:
        limit = int(limit or self.cfg.agent.default_pull_limit)
        if kind == "score":
            return self.pull_score(limit)
        if kind == "tailor":
            return self.pull_tailor(limit)
        if kind == "questions":
            return self.pull_questions(limit)
        raise ValueError("kind must be score, tailor or questions")

    def pull_score(self, limit: int) -> dict[str, Any]:
        rows = self._rows(
            "SELECT * FROM (SELECT j.id, j.source, j.title, j.company_name, j.location, j.remote, j.url, "
            "j.salary_min, j.salary_max, j.salary_currency, j.posted_at, j.description, "
            "(SELECT s.similarity FROM job_scores s WHERE s.job_id = j.id AND s.similarity IS NOT NULL "
            " ORDER BY s.id DESC LIMIT 1) AS similarity "
            "FROM jobs j WHERE j.status = 'filtered' AND j.apply_url IS NOT NULL) q "
            "ORDER BY COALESCE(q.similarity, -1) DESC, q.id LIMIT $1", [limit])
        items = [{
            "job_id": as_int(r["id"]), "title": r["title"], "company": r["company_name"], "location": r["location"],
            "remote": as_bool(r["remote"]), "source": r["source"], "url": r["url"],
            "salary": {"min": as_int(r["salary_min"]), "max": as_int(r["salary_max"]), "currency": r["salary_currency"]},
            "posted_at": str(r["posted_at"]) if r["posted_at"] is not None else None,
            "similarity": round(as_float(r["similarity"]) or 0.0, 4) if r["similarity"] is not None else None,
            "description": self._trim(r["description"]),
        } for r in rows]
        return {
            "kind": "score",
            "instructions": load_prompt("score_rubric.md"),
            "threshold": self.cfg.matching.agent_score_threshold,
            "profile": agent_profile(self.profile),
            "items": items,
            "output_schema": ScoresPayload.model_json_schema(),
            "push_command": "jobbuddy agent-push-scores FILE.json",
        }

    def pull_tailor(self, limit: int) -> dict[str, Any]:
        from jobbuddy.tailoring.resume import deterministic_tailor

        rows = self._rows(
            "SELECT * FROM (SELECT a.id AS application_id, a.ats, j.id AS job_id, j.title, j.company_name, "
            "j.location, j.url, j.description, "
            "(SELECT s.llm_score FROM job_scores s WHERE s.job_id = j.id AND s.llm_score IS NOT NULL "
            " ORDER BY s.id DESC LIMIT 1) AS score, "
            "(SELECT s.llm_reasons FROM job_scores s WHERE s.job_id = j.id AND s.llm_score IS NOT NULL "
            " ORDER BY s.id DESC LIMIT 1) AS score_reasons "
            "FROM applications a JOIN jobs j ON j.id = a.job_id WHERE a.status = 'scored') q "
            "ORDER BY COALESCE(q.score, -1) DESC, q.application_id LIMIT $1", [limit])
        items = []
        for r in rows:
            job = _JobView(r["title"], r["company_name"], r["description"])
            base = deterministic_tailor(self.profile, job, self.cfg.tailoring)
            items.append({
                "application_id": as_int(r["application_id"]), "job_id": as_int(r["job_id"]),
                "title": r["title"], "company": r["company_name"], "location": r["location"], "url": r["url"],
                "score": as_int(r["score"]), "score_reasons": as_json(r["score_reasons"]),
                "description": self._trim(r["description"]),
                "baseline": {"summary": base.summary, "skills_order": base.skills,
                             "bullets": [{"source_id": b.source_id, "text": b.text}
                                         for role in base.roles for b in role.bullets]},
            })
        cover = (load_prompt("cover_letter.md")
                 .replace("{{PERSONAL_HOOK}}", (self.profile.voice.personal_hook or "(none provided: use a brief, "
                          "non-specific line about why healthcare technology matters)").strip())
                 .replace("{{VOICE_SAMPLE}}", self.style_sample()[:6000] or "(no sample available)"))
        return {
            "kind": "tailor",
            "instructions": load_prompt("tailor_resume.md") + "\n\n---\n\n" + cover + "\n\n---\n\n"
                            + load_prompt("answer_questions.md"),
            "max_bullets_per_role": self.cfg.tailoring.max_bullets_per_role,
            "profile": agent_profile(self.profile),
            "items": items,
            "output_schema": TailoringPayload.model_json_schema(),
            "validate_command": "jobbuddy validate-tailoring FILE.json",
            "push_command": "jobbuddy agent-push-tailoring FILE.json",
        }

    def pull_questions(self, limit: int) -> dict[str, Any]:
        rows = self._rows(
            "SELECT q.id AS question_id, q.application_id, q.field_id, q.label, q.field_type, q.required, q.options, "
            "q.max_length, j.title, j.company_name, j.description, a.cover_letter_text "
            "FROM field_questions q JOIN applications a ON a.id = q.application_id JOIN jobs j ON j.id = a.job_id "
            "WHERE q.status = 'pending' AND a.status = 'needs_answers' ORDER BY q.application_id, q.id LIMIT $1",
            [limit])
        apps: dict[int, dict[str, Any]] = {}
        for r in rows:
            aid = as_int(r["application_id"]) or 0
            app = apps.setdefault(aid, {
                "application_id": aid, "title": r["title"], "company": r["company_name"],
                "description": self._trim(r["description"], 3000),
                "cover_letter": r["cover_letter_text"], "questions": []})
            app["questions"].append({
                "question_id": as_int(r["question_id"]), "label": r["label"], "type": r["field_type"],
                "required": bool(as_bool(r["required"])), "options": as_json(r["options"]) or [],
                "max_length": as_int(r["max_length"])})
        return {
            "kind": "questions",
            "instructions": load_prompt("answer_questions.md"),
            "profile": agent_profile(self.profile),
            "items": list(apps.values()),
            "output_schema": AnswersPayload.model_json_schema(),
            "validate_command": "jobbuddy validate-answers FILE.json",
            "push_command": "jobbuddy agent-push-answers FILE.json",
        }

    # =============================== scores ===============================
    def push_scores(self, data: Any) -> dict[str, Any]:
        payload = _coerce_payload(ScoresPayload, data, "scores")
        thr = self.cfg.matching.agent_score_threshold
        out = {"scored": 0, "below_threshold": 0, "stale": 0, "applications_created": 0}
        for it in payload.scores:
            cur = self._rows("SELECT status FROM jobs WHERE id = $1", [it.job_id])
            if not cur or cur[0]["status"] != "filtered":
                out["stale"] += 1
                continue
            passed = it.score >= thr
            reasons = it.model_dump(exclude={"job_id"})
            stmts: list[tuple[str, list[Any]]] = [
                ("INSERT INTO job_scores (job_id, filter_passed, similarity, embedding_model, llm_score, llm_reasons, "
                 "llm_model, created_at) VALUES ($1, $2, "
                 "(SELECT s.similarity FROM job_scores s WHERE s.job_id = $1 AND s.similarity IS NOT NULL "
                 " ORDER BY s.id DESC LIMIT 1), "
                 "(SELECT s.embedding_model FROM job_scores s WHERE s.job_id = $1 AND s.similarity IS NOT NULL "
                 " ORDER BY s.id DESC LIMIT 1), $3, " + cast(self.t, "$4", "JSONB") + ", $5, CURRENT_TIMESTAMP)",
                 [it.job_id, True, it.score, reasons, AGENT_MODEL_TAG]),
                ("UPDATE jobs SET status = $2 WHERE id = $1 AND status = 'filtered'",
                 [it.job_id, "scored" if passed else "below_threshold"]),
            ]
            if passed:
                stmts += [
                    ("INSERT INTO applications (job_id, status, outcome, ats, created_at, updated_at) "
                     "SELECT j.id, 'scored', 'none', j.source, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP FROM jobs j "
                     "WHERE j.id = $1 AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id = $1)",
                     [it.job_id]),
                    ("INSERT INTO application_events (application_id, to_status, event_type, actor, note, created_at) "
                     "SELECT a.id, 'scored', 'transition', 'agent', $2, CURRENT_TIMESTAMP FROM applications a "
                     "WHERE a.job_id = $1 AND a.status = 'scored' AND NOT EXISTS "
                     "(SELECT 1 FROM application_events e WHERE e.application_id = a.id)",
                     [it.job_id, f"agent score {it.score} >= {thr}"]),
                ]
            res = self.t.transaction(stmts)
            if res[1].row_count == 0:
                out["stale"] += 1
                continue
            out["scored" if passed else "below_threshold"] += 1
            if passed and res[2].row_count:
                out["applications_created"] += 1
        return out

    # =============================== tailoring ===============================
    def validate_tailoring(self, data: Any, contexts: dict[int, dict[str, Any]] | None = None) -> dict[str, Any]:
        from jobbuddy.tailoring.questions import sensitive_key
        from jobbuddy.tailoring.validator import ClaimValidator

        payload = _coerce_payload(TailoringPayload, data, "items")
        by_id = self.profile.bullet_by_id()
        skills = {s.lower() for s in self.profile.skills}
        validator = ClaimValidator(self.profile)
        voice = ClaimValidator(self.profile, extra_allowed_text=self.style_sample())
        results = []
        for it in payload.items:
            errors: list[str] = []
            warnings: list[str] = []
            ctx = (contexts or {}).get(it.application_id) or self._job_context(it.application_id)
            if ctx is None:
                results.append({"application_id": it.application_id, "ok": False,
                                "errors": ["application not found"], "warnings": []})
                continue
            company = ctx.get("company_name") or ""
            job_text = f"{ctx.get('title') or ''}\n{company}\n{ctx.get('description') or ''}"
            exempt = {company} if company else set()
            # bullets: reorder/reword only, each against its own source bullet
            seen: set[str] = set()
            for b in it.bullets:
                src = by_id.get(b.source_id)
                if src is None:
                    errors.append(f"bullet {b.source_id!r}: unknown source_id")
                    continue
                if b.source_id in seen:
                    warnings.append(f"bullet {b.source_id!r} listed twice (second ignored)")
                    continue
                seen.add(b.source_id)
                rep = validator.validate_bullet(src.text, b.text, self.cfg.tailoring.min_bullet_overlap)
                for s in rep.summary():
                    (errors if s.startswith("error") else warnings).append(f"bullet {b.source_id}: {s}")
            invented = [s for s in it.skills_order if s.lower() not in skills]
            if invented:
                errors.append(f"skills_order contains skills not in profile: {invented}")
            if it.summary:
                for s in validator.validate_text(it.summary).summary():
                    (errors if s.startswith("error") else warnings).append(f"summary: {s}")
            if it.cover_letter is not None:
                cl = it.cover_letter.strip()
                if not cl:
                    errors.append("cover_letter is empty")
                if re.search(r"\[[^\]]{2,40}\]|\{\{|\}\}", cl):
                    errors.append("cover_letter contains a placeholder like [Company] or {{...}}")
                words = len(cl.split())
                if words > self.cfg.agent.max_cover_letter_words:
                    errors.append(f"cover_letter has {words} words (max {self.cfg.agent.max_cover_letter_words})")
                if self.profile.personal.full_name and self.profile.personal.full_name not in cl:
                    warnings.append("cover_letter is not signed with the full name")
                rep = voice.validate_text(cl, context_text=job_text, context_terms_ok_in_claims=exempt)
                for s in rep.summary():
                    (errors if s.startswith("error") else warnings).append(f"cover_letter: {s}")
            for q, a in it.answers.items():
                if sensitive_key(q):
                    errors.append(f"answer to sensitive question {q[:60]!r} is not allowed (Mike answers it)")
                    continue
                for s in validator.validate_text(a, context_text=job_text, context_terms_ok_in_claims=exempt).summary():
                    (errors if s.startswith("error") else warnings).append(f"answer {q[:40]!r}: {s}")
            results.append({"application_id": it.application_id, "ok": not errors, "errors": errors,
                            "warnings": warnings, "status": ctx.get("status")})
        return {"ok": all(r["ok"] for r in results), "items": results}

    def push_tailoring(self, data: Any, *, render_pdf: bool = True) -> dict[str, Any]:
        from jobbuddy.db.repository import _guess_type
        from jobbuddy.tailoring.cover_letter import template_cover_letter
        from jobbuddy.tailoring.render import docx_to_pdf, render_cover_letter_pdf, render_resume_docx
        from jobbuddy.tailoring.resume import _LLMTailor, apply_llm_tailoring, deterministic_tailor

        payload = _coerce_payload(TailoringPayload, data, "items")
        contexts = {it.application_id: self._job_context(it.application_id) for it in payload.items}
        report = self.validate_tailoring(payload.model_dump(), contexts={k: v for k, v in contexts.items() if v})
        out: dict[str, Any] = {"tailored": 0, "rejected": 0, "stale": 0, "items": []}
        for it, check in zip(payload.items, report["items"]):
            ctx = contexts.get(it.application_id)
            if not check["ok"]:
                out["rejected"] += 1
                out["items"].append({"application_id": it.application_id, "result": "rejected",
                                     "errors": check["errors"]})
                continue
            if ctx is None or ctx["status"] != "scored":
                out["stale"] += 1
                out["items"].append({"application_id": it.application_id, "result": "stale",
                                     "status": ctx["status"] if ctx else None})
                continue
            job = _JobView(ctx["title"], ctx["company_name"], ctx["description"])
            base = deterministic_tailor(self.profile, job, self.cfg.tailoring)
            tailored = apply_llm_tailoring(
                self.profile, _LLMTailor(summary=it.summary, skills_order=it.skills_order,
                                         bullets=[{"source_id": b.source_id, "text": b.text} for b in it.bullets]),
                self.cfg.tailoring, base)
            tailored.validation_notes.append("tailored by on-box agent; validated by validate-tailoring")
            cover = (it.cover_letter or "").strip() or template_cover_letter(self.profile, job)
            appdir = self.workdir / f"app{it.application_id}"
            appdir.mkdir(parents=True, exist_ok=True)
            p = self.profile.personal
            resume_docx = render_resume_docx(self.profile, tailored, appdir / f"{p.first_name}_{p.last_name}_Resume.docx")
            want_pdf = render_pdf and "pdf" in self.cfg.tailoring.output_formats
            files = [("tailored_resume", (docx_to_pdf(resume_docx) if want_pdf else None) or resume_docx)]
            # The cover letter is always a PDF rendered from Mike's template, never the .docx. Without a
            # PDF converter (or with --no-pdf) only the text is stored and the filler renders it at fill time.
            if want_pdf:
                cl_pdf = render_cover_letter_pdf(self.profile, cover,
                                                 appdir / f"{p.first_name}_{p.last_name}_Cover_Letter.docx",
                                                 template=self.cover_letter_template())
                if cl_pdf is not None:
                    files.append(("cover_letter", cl_pdf))
            aid = it.application_id
            stmts: list[tuple[str, list[Any]]] = []
            for kind, path in files:
                blob = path.read_bytes()
                stmts.append((
                    "INSERT INTO candidate_documents (kind, filename, content_type, content, sha256, size_bytes, "
                    "application_id, created_at) SELECT $2, $3, $4, " + cast(self.t, "$5", "BYTEA") + ", $6, "
                    + cast(self.t, "$7", "INTEGER") + ", a.id, CURRENT_TIMESTAMP FROM applications a "
                    "WHERE a.id = $1 AND a.status = 'scored'",
                    [aid, kind, path.name, _guess_type(path.name), blob, hashlib.sha256(blob).hexdigest(), len(blob)]))
            stmts.append((
                "INSERT INTO application_events (application_id, from_status, to_status, event_type, actor, note, "
                "created_at) SELECT a.id, 'scored', 'tailored', 'transition', 'agent', $2, CURRENT_TIMESTAMP "
                "FROM applications a WHERE a.id = $1 AND a.status = 'scored'",
                [aid, "tailored by on-box agent (" + ", ".join(p.name for _, p in files) + ")"]))
            stmts.append((
                "UPDATE applications SET tailored_resume = " + cast(self.t, "$2", "JSONB") + ", cover_letter_text = $3, "
                "answers = " + cast(self.t, "$4", "JSONB") + ", status = 'tailored', error = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = $1 AND status = 'scored'",
                [aid, tailored.model_dump(), cover,
                 {q: {"text": a, "source": "agent"} for q, a in it.answers.items()}]))
            res = self.t.transaction(stmts)
            for _, path in files:
                path.unlink(missing_ok=True)
            if res[-1].row_count == 0:
                out["stale"] += 1
                out["items"].append({"application_id": aid, "result": "stale"})
                continue
            out["tailored"] += 1
            out["items"].append({"application_id": aid, "result": "tailored", "warnings": check["warnings"],
                                 "documents": [f"{k}:{p.suffix.lstrip('.')}" for k, p in files]})
        return out

    # =============================== answers ===============================
    def _questions(self, ids: list[int]) -> dict[int, dict[str, Any]]:
        out: dict[int, dict[str, Any]] = {}
        for qid in ids:
            rows = self._rows(
                "SELECT q.id, q.application_id, q.ats, q.label, q.label_normalized, q.field_type, q.options, "
                "q.max_length, q.status, a.status AS app_status, j.title, j.company_name, j.description "
                "FROM field_questions q JOIN applications a ON a.id = q.application_id JOIN jobs j ON j.id = a.job_id "
                "WHERE q.id = $1", [qid])
            if rows:
                out[qid] = rows[0]
        return out

    def validate_answers(self, data: Any, questions: dict[int, dict[str, Any]] | None = None) -> dict[str, Any]:
        from jobbuddy.tailoring.questions import sensitive_key
        from jobbuddy.tailoring.validator import ClaimValidator

        payload = _coerce_payload(AnswersPayload, data, "answers")
        qs = questions if questions is not None else self._questions([a.question_id for a in payload.answers])
        validator = ClaimValidator(self.profile)
        results = []
        for a in payload.answers:
            q = qs.get(a.question_id)
            errors: list[str] = []
            warnings: list[str] = []
            if q is None:
                errors.append("question not found")
            elif a.skip:
                pass
            elif sensitive_key(q["label"] or ""):
                errors.append("sensitive question: the agent must skip it (Mike answers on the approval card)")
            elif a.answer in (None, "", []):
                errors.append("empty answer (use \"skip\": true instead)")
            else:
                ftype = q["field_type"] or ""
                options = [str(o) for o in (as_json(q["options"]) or [])]
                if ftype in CHOICE_TYPES and options:
                    vals = a.answer if isinstance(a.answer, list) else [a.answer]
                    bad = [v for v in vals if str(v) not in options]
                    if bad:
                        errors.append(f"answer {bad} is not one of the options {options[:12]}")
                elif ftype == "checkbox" and not isinstance(a.answer, bool):
                    errors.append("checkbox answers must be true or false")
                elif isinstance(a.answer, str):
                    ml = as_int(q["max_length"])
                    if ml and len(a.answer) > ml:
                        errors.append(f"answer is {len(a.answer)} chars (max_length {ml})")
                    job_text = f"{q['title'] or ''} {q['company_name'] or ''} {q['description'] or ''}"
                    rep = validator.validate_text(a.answer, context_text=job_text,
                                                  context_terms_ok_in_claims={q["company_name"] or ""})
                    for s in rep.summary():
                        (errors if s.startswith("error") else warnings).append(s)
            results.append({"question_id": a.question_id, "ok": not errors, "errors": errors, "warnings": warnings})
        return {"ok": all(r["ok"] for r in results), "items": results}

    def push_answers(self, data: Any) -> dict[str, Any]:
        payload = _coerce_payload(AnswersPayload, data, "answers")
        qs = self._questions([a.question_id for a in payload.answers])
        report = self.validate_answers(payload.model_dump(), questions=qs)
        out: dict[str, Any] = {"answered": 0, "dismissed": 0, "rejected": 0, "stale": 0, "learned": 0,
                               "applications_ready": [], "items": []}
        touched: set[int] = set()
        conf = self.cfg.engine.agent_answer_confidence
        for a, check in zip(payload.answers, report["items"]):
            q = qs.get(a.question_id)
            if not check["ok"]:
                out["rejected"] += 1
                out["items"].append({"question_id": a.question_id, "result": "rejected", "errors": check["errors"]})
                continue
            if q is None or q["status"] != "pending":
                out["stale"] += 1
                continue
            status = "dismissed" if a.skip else "answered"
            stmts: list[tuple[str, list[Any]]] = [(
                "UPDATE field_questions SET status = $2, answer = " + cast(self.t, "$3", "JSONB") + ", "
                "answered_by = 'agent', reusable = $4, notes = $5, answered_at = CURRENT_TIMESTAMP "
                "WHERE id = $1 AND status = 'pending'",
                [a.question_id, status, None if a.skip else json.dumps(a.answer),
                 bool(a.reusable and not a.skip), (a.notes or None)])]
            learn = (status == "answered" and a.reusable and isinstance(a.answer, str) and q["label_normalized"])
            if learn:
                stmts.append((
                    "INSERT INTO form_field_mappings (ats, label_normalized, field_type, static_value, confidence, "
                    "times_used, times_confirmed, source, created_at) VALUES ($1, $2, $3, $4, $5, 0, 0, 'agent', "
                    "CURRENT_TIMESTAMP) ON CONFLICT (ats, label_normalized) DO NOTHING",
                    [q["ats"] or "generic", q["label_normalized"], q["field_type"], a.answer, conf]))
            res = self.t.transaction(stmts)
            if res[0].row_count == 0:
                out["stale"] += 1
                continue
            out[status] += 1
            if learn and res[1].row_count:
                out["learned"] += 1
            touched.add(as_int(q["application_id"]) or 0)
            out["items"].append({"question_id": a.question_id, "result": status, "warnings": check["warnings"]})
        for aid in sorted(touched):
            no_pending = ("NOT EXISTS (SELECT 1 FROM field_questions q WHERE q.application_id = $1 "
                          "AND q.status = 'pending')")
            res = self.t.transaction([
                ("INSERT INTO application_events (application_id, from_status, to_status, event_type, actor, note, "
                 "created_at) SELECT a.id, 'needs_answers', 'tailored', 'transition', 'agent', "
                 "'all field questions answered; CI will refill', CURRENT_TIMESTAMP FROM applications a "
                 "WHERE a.id = $1 AND a.status = 'needs_answers' AND " + no_pending, [aid]),
                ("UPDATE applications SET status = 'tailored', updated_at = CURRENT_TIMESTAMP "
                 "WHERE id = $1 AND status = 'needs_answers' AND " + no_pending, [aid]),
            ])
            if res[1].row_count:
                out["applications_ready"].append(aid)
        return out

    # =============================== alert e-mails ===============================
    async def ingest_alert(self, msg: Any, *, http: Any = None, probe: bool | None = None) -> dict[str, Any]:
        """Parse one LinkedIn/Indeed alert e-mail (raw or forwarded) into job leads and resolve them to the
        employer's ATS posting. Never requests linkedin.com or indeed.com."""
        from jobbuddy.db.repository import message_id_hash, posting_hash
        from jobbuddy.fetchers.email_alerts import (message_key, parse_alert_message, probe_public_boards,
                                                    resolve_against)

        key = message_key(msg)
        digest = message_id_hash(key)
        if self._rows("SELECT id FROM processed_emails WHERE message_id_sha256 = $1", [digest]):
            return {"duplicate": True, "leads": []}
        leads = parse_alert_message(msg)
        probe = self.cfg.fetch.probe_ats_for_email_leads if probe is None else probe
        cands = [_JobView(r["title"], r["company_name"], None, source=r["source"],
                          external_job_id=r["external_job_id"], apply_url=r["apply_url"])
                 for r in self._rows("SELECT source, external_job_id, title, company_name, apply_url FROM jobs "
                                     "WHERE apply_url IS NOT NULL AND source NOT IN ('linkedin_alert', 'indeed_alert', 'wellfound')")]
        out_leads = []
        stmts: list[tuple[str, list[Any]]] = []
        for lead in leads:
            res = resolve_against(lead, cands)
            if res is None and probe and http is not None:
                res = await probe_public_boards(http, lead)
                if res and res.posting:
                    stmts.append(self._insert_job_stmt(res.posting, "found", posting_hash(res.posting)))
                    cands.append(_JobView(res.posting.title, res.posting.company_name, None,
                                          source=res.posting.source, external_job_id=res.posting.external_job_id,
                                          apply_url=res.posting.apply_url))
            posting = lead.to_posting()
            if res:
                posting.lead_meta = {**(posting.lead_meta or {}), "resolved": True, "needs_resolution": False,
                                     "resolved_to": {"source": res.source, "external_job_id": res.external_job_id},
                                     "match_score": round(res.score, 3)}
            # The ATS row is the one CI filters/embeds and the agent scores; the alert row is just provenance.
            # Unresolved Wellfound leads become 'manual_apply': CI lists their link in Mike's Telegram digest
            # (Wellfound has no public API and in-app apply is never automated).
            if res:
                status = "closed"
            elif lead.origin == "wellfound":
                status = "manual_apply"
                posting.lead_meta = {**(posting.lead_meta or {}), "manual_apply": True, "digest_sent": False}
            else:
                status = "found"
            stmts.append(self._insert_job_stmt(posting, status, posting_hash(posting)))
            out_leads.append({"origin": lead.origin, "title": lead.title, "company": lead.company,
                              "location": lead.location, "status": status, "url": posting.url,
                              "salary": {k: lead.extra.get(k) for k in ("salary_min", "salary_max",
                                                                        "equity_min_pct", "equity_max_pct")
                                         if lead.extra.get(k) is not None} or None,
                              "resolved_to": ({"source": res.source, "external_job_id": res.external_job_id,
                                               "apply_url": res.apply_url, "score": round(res.score, 3)}
                                              if res else None)})
        stmts.append(("INSERT INTO processed_emails (message_id_sha256, origin, leads_found, processed_at) "
                      "VALUES ($1, $2, $3, CURRENT_TIMESTAMP) ON CONFLICT (message_id_sha256) DO NOTHING",
                      [digest, leads[0].origin if leads else None, len(leads)]))
        self.t.transaction(stmts)
        return {"duplicate": False, "leads": out_leads,
                "resolved": sum(1 for x in out_leads if x["resolved_to"]),
                "manual_apply": sum(1 for x in out_leads if x["status"] == "manual_apply"),
                "unresolved": sum(1 for x in out_leads if not x["resolved_to"])}

    def _insert_job_stmt(self, p: Any, status: str, content_hash: str) -> tuple[str, list[Any]]:
        now = ts_param(self.t, _now_utc())
        return ("INSERT INTO jobs (source, external_job_id, company_name, title, location, remote, url, apply_url, "
                "description, salary_min, salary_max, salary_currency, posted_at, first_seen_at, last_seen_at, "
                "content_hash, status, lead_meta) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, "
                "$14, $14, $15, $16, " + cast(self.t, "$17", "JSONB") + ") "
                "ON CONFLICT (source, external_job_id) DO NOTHING",
                [p.source, p.external_job_id, p.company_name, p.title, p.location, p.remote, p.url, p.apply_url,
                 p.description, p.salary_min, p.salary_max, p.salary_currency, ts_param(self.t, p.posted_at), now,
                 content_hash, status, p.lead_meta])


class _JobView:
    """Duck-typed stand-in for a Job row (title/company_name/description) used by tailoring helpers."""

    def __init__(self, title: Any, company_name: Any, description: Any, *, source: str = "", external_job_id: str = "",
                 apply_url: str | None = None) -> None:
        self.title = str(title or "")
        self.company_name = company_name
        self.description = description
        self.source = source
        self.external_job_id = external_job_id
        self.apply_url = apply_url
        self.location = None


def read_json_file(path: str | Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON ({exc.msg} at line {exc.lineno})") from None


def explain_validation_error(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:10])


def default_profile_path() -> Path:
    return PROJECT_ROOT / "profile" / "profile.yaml"


__all__ = ["AgentIO", "agent_profile", "load_prompt", "read_json_file", "explain_validation_error", "is_todo"]
