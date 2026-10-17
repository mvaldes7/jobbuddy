"""Role-specific cover letters in Mike's voice.

Generated whenever a form has a cover-letter field or upload. The style sample (his real letter)
is passed as a VOICE example only; facts come exclusively from profile.yaml and are validated.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from jobbuddy.llm.client import LLMClient
from jobbuddy.matching.rubric import profile_brief
from jobbuddy.profile import Profile
from jobbuddy.tailoring.validator import ClaimValidator, ValidationReport, content_words

log = logging.getLogger(__name__)

SYSTEM = """You write cover letters in the candidate's own voice. Study the STYLE SAMPLE for tone,
rhythm, sentence length and structure, but NEVER copy its company-specific content.

Structure (4-5 short paragraphs, 300-420 words):
1. Opening: name the exact role and company, then the candidate's personal hook about why healthcare
   and technology matter to him. Use ONLY the hook provided in PERSONAL_HOOK (you may lightly rephrase);
   never invent personal or medical details.
2. Why this company: connect the hook to something specific in the job posting.
3. Proof: 2-3 concrete accomplishments with metrics, taken verbatim in substance from CANDIDATE FACTS.
   Emphasize "built it myself" ownership (e.g., personally building the validated app).
4. Fit: map his experience to the role's top 2-3 requirements, honestly. Do not claim tools or domains
   that are not in CANDIDATE FACTS.
5. Warm, direct close thanking them and inviting a conversation. Sign off "Sincerely," then his full name.

Hard rules: every number must appear in CANDIDATE FACTS. No placeholders like [Company]. No em-dash spam.
Plain text only, paragraphs separated by a blank line."""


@dataclass
class CoverLetter:
    text: str
    report: ValidationReport
    used_llm: bool
    notes: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return not self.report.ok or bool(self.report.issues)


def _salutation(hiring_manager: str | None) -> str:
    return f"Dear {hiring_manager}," if hiring_manager else "Dear Hiring Team,"


def _lower_first(s: str) -> str:
    return s[:1].lower() + s[1:] if s and not s[:2].isupper() else s


def _pick_bullets(profile: Profile, job: Any, n: int = 3) -> list[str]:
    jw = content_words(f"{job.title} {job.description or ''}")
    bullets = [b.text for _, _, b in profile.iter_bullets()]
    with_metrics = [b for b in bullets if re.search(r"\d|three|seven", b)]
    pool = with_metrics or bullets
    ranked = sorted(pool, key=lambda t: -len(content_words(t) & jw))
    return [" ".join(b.split()).rstrip(".") for b in ranked[:n]]


def template_cover_letter(profile: Profile, job: Any, hiring_manager: str | None = None) -> str:
    """Deterministic fallback used when no LLM is configured. Uses only profile text."""
    company = job.company_name or "your company"
    first_exp = profile.experience[0] if profile.experience else None
    hook = " ".join((profile.voice.personal_hook or "").split())
    p1 = f"I am writing to express my strong interest in the {job.title} role at {company}."
    if hook:
        p1 += f" {hook}"
    years = profile.years_experience
    p2 = ""
    if first_exp:
        p2 = (f"With {years} years at {first_exp.company}, " if years else f"At {first_exp.company}, ") + \
             "I've operated at the intersection of technical architecture, product strategy, and client delivery."
    picks = _pick_bullets(profile, job)
    p3 = " ".join(f"I {_lower_first(b)}." for b in picks)
    p4 = (f"I'd welcome the opportunity to discuss how my experience could contribute to your team at {company}. "
          "Thank you for your consideration, and I look forward to speaking further.")
    body = "\n\n".join(x for x in [_salutation(hiring_manager), p1, p2, p3, p4] if x)
    return f"{body}\n\nSincerely,\n{profile.personal.full_name}"


def _strip_fences(text: str) -> str:
    t = text.strip()
    t = re.sub(r"^```\w*\n|```$", "", t).strip()
    return t


def generate_cover_letter(llm: LLMClient | None, profile: Profile, job: Any, style_sample: str,
                          hiring_manager: str | None = None, max_attempts: int = 2) -> CoverLetter:
    job_text = f"{job.title}\n{job.company_name or ''}\n{job.description or ''}"
    validator = ClaimValidator(profile, extra_allowed_text=style_sample)
    exempt = {job.company_name} if job.company_name else set()
    if llm is None:
        text = template_cover_letter(profile, job, hiring_manager)
        rep = validator.validate_text(text, context_text=job_text, context_terms_ok_in_claims=exempt)
        return CoverLetter(text, rep, used_llm=False, notes=["template fallback (no LLM configured)"])

    user = (
        f"STYLE SAMPLE (voice only):\n<<<\n{style_sample[:5000]}\n>>>\n\n"
        f"PERSONAL_HOOK:\n{profile.voice.personal_hook or '(none provided - use a brief, non-specific line about why healthcare technology matters)'}\n\n"
        f"CANDIDATE FACTS:\n{json.dumps(profile_brief(profile), ensure_ascii=False)}\n"
        f"Full name for signature: {profile.personal.full_name}\n\n"
        f"JOB:\nTitle: {job.title}\nCompany: {job.company_name}\nLocation: {job.location}\n"
        f"Description:\n{(job.description or '')[:6000]}\n\n"
        f"Salutation to use: {_salutation(hiring_manager)}"
    )
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    notes: list[str] = []
    for attempt in range(max_attempts):
        try:
            resp = llm.chat(messages, purpose="cover_letter", max_tokens=900, temperature=0.5)
        except Exception as exc:
            notes.append(f"LLM failed: {type(exc).__name__}")
            break
        text = _strip_fences(resp.text)
        rep = validator.validate_text(text, context_text=job_text, context_terms_ok_in_claims=exempt)
        if rep.ok and "[" not in text:
            return CoverLetter(text, rep, used_llm=True, notes=notes)
        notes.append(f"attempt {attempt + 1} rejected: {'; '.join(rep.summary())[:300]}")
        messages = messages + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": "Revise: remove or fix these unsupported claims and keep everything else: "
                                        + "; ".join(rep.summary())},
        ]
    text = template_cover_letter(profile, job, hiring_manager)
    rep = validator.validate_text(text, context_text=job_text, context_terms_ok_in_claims=exempt)
    notes.append("fell back to template letter")
    return CoverLetter(text, rep, used_llm=False, notes=notes)
