"""Stage 3: LLM rubric score 0-100 with structured JSON reasons (pydantic-validated)."""
from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, field_validator

from jobbuddy.llm.client import LLMClient, chat_json
from jobbuddy.profile import Profile

RUBRIC = """Score how well the candidate fits the job from 0 to 100 using this rubric:
- 35 pts role fit: responsibilities overlap with the candidate's actual experience
- 20 pts domain fit: healthcare / clinical / life sciences / regulated SaaS
- 20 pts skills fit: required skills the candidate demonstrably has
- 15 pts seniority fit: level matches ~8 years of experience
- 10 pts logistics: location / remote compatibility
Be strict. Only credit experience that is explicitly in the candidate profile.
Reply with JSON only:
{"score": int, "reasons": [str, ...], "strengths": [str, ...], "gaps": [str, ...],
 "seniority_fit": "under|match|over", "domain_fit": "strong|partial|none"}"""


class RubricResult(BaseModel):
    score: int = Field(ge=0, le=100)
    reasons: list[str] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    seniority_fit: str | None = None
    domain_fit: str | None = None

    @field_validator("score", mode="before")
    @classmethod
    def _clamp(cls, v: Any) -> int:
        try:
            return max(0, min(100, int(round(float(v)))))
        except (TypeError, ValueError):
            raise ValueError("score must be numeric")


def profile_brief(profile: Profile) -> dict[str, Any]:
    """Facts for the LLM. Contact details are deliberately excluded from prompts."""
    return {
        "headline": profile.headline,
        "years_experience": profile.years_experience,
        "summary": profile.summary,
        "experience": [
            {"company": e.company, "roles": [{"title": r.title, "start": r.start, "end": r.end,
                                              "bullets": [b.text for b in r.bullets]} for r in e.roles]}
            for e in profile.experience
        ],
        "education": [f"{ed.degree}, {ed.school} ({ed.year})" for ed in profile.education],
        "certifications": [f"{c.name} - {c.issuer} ({c.year})" for c in profile.certifications],
        "skills": profile.skills,
        "target_titles": profile.targets.titles,
        "target_locations": profile.targets.locations,
    }


def score_job(llm: LLMClient, profile: Profile, job: Any, max_desc_chars: int = 6000) -> RubricResult:
    job_blob = {"title": job.title, "company": job.company_name, "location": job.location,
                "remote": job.remote, "description": (job.description or "")[:max_desc_chars]}
    messages = [
        {"role": "system", "content": "You are a rigorous technical recruiter. " + RUBRIC},
        {"role": "user", "content": "CANDIDATE:\n" + json.dumps(profile_brief(profile), ensure_ascii=False)
                                    + "\n\nJOB:\n" + json.dumps(job_blob, ensure_ascii=False)},
    ]
    return chat_json(llm, messages, RubricResult, purpose="score", max_tokens=500)


def heuristic_score(similarity: float) -> RubricResult:
    """No-LLM fallback: map similarity to a conservative score so nothing auto-advances blindly."""
    s = int(max(0.0, min(1.0, similarity)) * 100 * 0.9)
    return RubricResult(score=s, reasons=[f"heuristic from embedding similarity {similarity:.2f} (no LLM configured)"])
