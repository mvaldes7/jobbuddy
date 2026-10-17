"""Resume tailoring: REORDER and REWORD existing bullets only. Never add facts.

The LLM proposes an ordering plus light rewording that mirrors the job's language; every rewrite is
checked by ClaimValidator and silently reverted to the original bullet if it fails.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from jobbuddy.config import TailoringConfig
from jobbuddy.llm.client import LLMClient, chat_json
from jobbuddy.profile import Profile
from jobbuddy.tailoring.validator import ClaimValidator, content_words

log = logging.getLogger(__name__)


class TailoredBullet(BaseModel):
    source_id: str
    text: str
    reworded: bool = False
    rejected_reasons: list[str] = Field(default_factory=list)


class TailoredRole(BaseModel):
    company: str
    location: str | None = None
    title: str
    official_title: str | None = None
    start: str | None = None
    end: str | None = None
    also_held: list[dict[str, Any]] = Field(default_factory=list)
    bullets: list[TailoredBullet]


class TailoredResume(BaseModel):
    summary: str
    skills: list[str]
    roles: list[TailoredRole]
    validation_notes: list[str] = Field(default_factory=list)


class _LLMBullet(BaseModel):
    source_id: str
    text: str


class _LLMTailor(BaseModel):
    summary: str | None = None
    skills_order: list[str] = Field(default_factory=list)
    bullets: list[_LLMBullet] = Field(default_factory=list)


SYSTEM = """You tailor a resume to a job. HARD RULES:
1. You may only REORDER and REWORD the candidate's existing bullets. Each output bullet must cite the
   source_id it came from. Never merge facts from different bullets.
2. Never add numbers, tools, employers, certifications, technologies or outcomes that are not in that bullet.
3. Rewording may mirror the job's vocabulary only where the meaning is identical.
4. skills_order must be a reordering of the given skills (you may drop irrelevant ones, never add).
5. The summary may only restate facts from the given summary and bullets.
Reply with JSON only: {"summary": str, "skills_order": [str], "bullets": [{"source_id": str, "text": str}]}
List bullets most-relevant first."""


def _relevance(text: str, job_words: set[str]) -> float:
    w = content_words(text)
    return len(w & job_words) / (len(w) ** 0.5 or 1)


def deterministic_tailor(profile: Profile, job: Any, cfg: TailoringConfig) -> TailoredResume:
    """No-LLM path: rank bullets and skills by keyword overlap; text stays verbatim."""
    job_words = content_words(f"{job.title} {job.description or ''}")
    roles = []
    for exp in profile.experience:
        for role in exp.roles:
            ranked = sorted(role.bullets, key=lambda b: -_relevance(b.text, job_words))[: cfg.max_bullets_per_role]
            roles.append(TailoredRole(company=exp.company, location=exp.location, title=role.title,
                                      official_title=role.official_title, start=role.start, end=role.end,
                                      also_held=role.also_held,
                                      bullets=[TailoredBullet(source_id=b.id, text=_norm_ws(b.text)) for b in ranked]))
    skills = sorted(profile.skills, key=lambda s: -_relevance(s, job_words))
    return TailoredResume(summary=_norm_ws(profile.summary or ""), skills=skills, roles=roles)


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def tailor_resume(llm: LLMClient | None, profile: Profile, job: Any, cfg: TailoringConfig) -> TailoredResume:
    base = deterministic_tailor(profile, job, cfg)
    if llm is None:
        base.validation_notes.append("no LLM configured: bullets reordered by keyword overlap, text verbatim")
        return base
    payload = {
        "job": {"title": job.title, "company": job.company_name, "description": (job.description or "")[:6000]},
        "summary": profile.summary,
        "skills": profile.skills,
        "bullets": [{"source_id": b.id, "role": r.title, "text": _norm_ws(b.text)} for _, r, b in profile.iter_bullets()],
    }
    try:
        out = chat_json(llm, [{"role": "system", "content": SYSTEM},
                              {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                        _LLMTailor, purpose="tailor_resume", max_tokens=1800)
    except Exception as exc:
        log.warning("tailor LLM failed (%s); using deterministic tailoring", type(exc).__name__)
        base.validation_notes.append(f"LLM tailoring failed: {type(exc).__name__}")
        return base
    return apply_llm_tailoring(profile, out, cfg, base)


def apply_llm_tailoring(profile: Profile, out: _LLMTailor, cfg: TailoringConfig,
                        base: TailoredResume) -> TailoredResume:
    validator = ClaimValidator(profile)
    by_id = profile.bullet_by_id()
    notes: list[str] = []
    proposed: dict[str, TailoredBullet] = {}
    order: list[str] = []
    for b in out.bullets:
        src = by_id.get(b.source_id)
        if src is None:
            notes.append(f"dropped bullet with unknown source_id {b.source_id!r}")
            continue
        if b.source_id in proposed:
            continue
        rep = validator.validate_bullet(src.text, b.text, cfg.min_bullet_overlap)
        if rep.ok:
            proposed[b.source_id] = TailoredBullet(source_id=b.source_id, text=_norm_ws(b.text),
                                                   reworded=_norm_ws(b.text) != _norm_ws(src.text))
        else:
            proposed[b.source_id] = TailoredBullet(source_id=b.source_id, text=_norm_ws(src.text),
                                                   rejected_reasons=rep.summary())
            notes.append(f"reverted {b.source_id}: {'; '.join(rep.summary())}")
        order.append(b.source_id)

    roles = []
    for exp in profile.experience:
        for role in exp.roles:
            ids = [b.id for b in role.bullets]
            ranked = [i for i in order if i in ids] + [i for i in
                                                       [tb.source_id for r in base.roles if r.title == role.title
                                                        for tb in r.bullets] if i not in order]
            bullets = [proposed.get(i) or TailoredBullet(source_id=i, text=_norm_ws(by_id[i].text)) for i in ranked]
            roles.append(TailoredRole(company=exp.company, location=exp.location, title=role.title,
                                      official_title=role.official_title, start=role.start, end=role.end,
                                      also_held=role.also_held, bullets=bullets[: cfg.max_bullets_per_role]))

    summary = base.summary
    if out.summary:
        rep = validator.validate_text(out.summary)
        if rep.ok:
            summary = _norm_ws(out.summary)
        else:
            notes.append("reverted summary: " + "; ".join(rep.summary()))
    allowed = {s.lower(): s for s in profile.skills}
    skills = [allowed[s.lower()] for s in out.skills_order if s.lower() in allowed]
    skills += [s for s in base.skills if s not in skills]
    dropped = [s for s in out.skills_order if s.lower() not in allowed]
    if dropped:
        notes.append(f"ignored invented skills: {dropped}")
    return TailoredResume(summary=summary, skills=list(dict.fromkeys(skills)), roles=roles, validation_notes=notes)
