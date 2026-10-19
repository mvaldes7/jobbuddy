"""Open-ended application questions ("Why do you want to work here?", "Describe a project...").

Sensitive/legal questions (authorization, sponsorship, salary, start date, relocation, EEO) are
NEVER answered by the LLM: they come from profile.application_answers or go to Mike for review.
"""
from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field

from jobbuddy.llm.client import LLMClient, chat_json
from jobbuddy.matching.rubric import profile_brief
from jobbuddy.profile import Profile, is_todo
from jobbuddy.tailoring.validator import ClaimValidator

SENSITIVE_PATTERNS: list[tuple[str, str]] = [
    (r"authori[sz]ed|eligible to work|work authori[sz]ation|legally (?:able|permitted)", "application_answers.work_authorization"),
    (r"sponsor", "application_answers.requires_sponsorship"),
    (r"salary|compensation|pay (?:range|expectation)|desired pay|expected pay", "application_answers.salary_expectation_text"),
    (r"start date|when can you start|notice period|available to start", "application_answers.start_date"),
    (r"relocat|commut|are you (currently )?(located|based|living) (in|near|within)|live (in|near|within) .*(area|commut)|(work|be) (in|from|at) (the |our )?office|in[- ]office|on[- ]?site", "application_answers.willing_to_relocate"),
    (r"gender|race|ethnic|hispanic|latino|veteran|disabilit|sexual orientation|pronoun|transgender", "application_answers.eeo"),
    (r"criminal|convicted|felony|background check", "__human__"),
]


class Answer(BaseModel):
    text: str | None
    confidence: float = Field(ge=0, le=1)
    source: str  # profile | llm | human_required
    needs_review: bool = False
    notes: list[str] = Field(default_factory=list)


class _LLMAnswer(BaseModel):
    answer: str
    confidence: float = Field(ge=0, le=1)


def sensitive_key(question: str) -> str | None:
    q = question.lower()
    for pat, key in SENSITIVE_PATTERNS:
        if re.search(pat, q):
            return key
    return None


def answer_question(llm: LLMClient | None, profile: Profile, job: Any, question: str,
                    max_chars: int | None = None, cover_letter: str | None = None) -> Answer:
    key = sensitive_key(question)
    if key:
        if key == "__human__" or key.endswith(".eeo"):
            return Answer(text=None, confidence=0.0, source="human_required", needs_review=True,
                          notes=["sensitive question: answer manually"])
        val = profile.get(key)
        if val is None or is_todo(val):
            return Answer(text=None, confidence=0.0, source="human_required", needs_review=True,
                          notes=[f"{key} is a TODO in profile.yaml"])
        return Answer(text=str(val), confidence=0.95, source="profile")
    if llm is None:
        return Answer(text=None, confidence=0.0, source="human_required", needs_review=True,
                      notes=["no LLM configured"])
    limit = f"Keep it under {max_chars} characters." if max_chars else "Keep it to 80-150 words."
    messages = [
        {"role": "system", "content": "Answer job-application questions in first person as the candidate, warm and "
                                      "specific. Use ONLY the candidate facts provided; never invent numbers, tools, "
                                      "employers or experiences. If the facts cannot support an answer, say so and set "
                                      "confidence below 0.4. " + limit +
                                      ' Reply JSON: {"answer": str, "confidence": 0..1}'},
        {"role": "user", "content": json.dumps({
            "question": question, "job": {"title": job.title, "company": job.company_name,
                                          "description": (job.description or "")[:4000]},
            "candidate": profile_brief(profile), "cover_letter_context": (cover_letter or "")[:2500]},
            ensure_ascii=False)},
    ]
    out = chat_json(llm, messages, _LLMAnswer, purpose="question", max_tokens=400)
    text = out.answer.strip()
    if max_chars and len(text) > max_chars:
        text = text[: max_chars - 1].rsplit(" ", 1)[0] + "…"
    rep = ClaimValidator(profile).validate_text(text, context_text=f"{job.title} {job.company_name} {job.description or ''}",
                                                context_terms_ok_in_claims={job.company_name or ""})
    conf = min(out.confidence, 0.8)
    if not rep.ok:
        conf = min(conf, 0.3)
    return Answer(text=text, confidence=conf, source="llm", needs_review=conf < 0.75 or not rep.ok,
                  notes=rep.summary())
