"""Stage 1: cheap deterministic hard filters. Every rejection carries a human-readable reason."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from jobbuddy.config import MatchingConfig
from jobbuddy.profile import Profile, is_todo


@dataclass
class FilterResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)


def _contains_phrase(text: str, phrase: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase.lower()) + r"(?![a-z0-9])", text) is not None


def effective_min_salary(cfg: MatchingConfig, profile: Profile | None) -> int | None:
    if cfg.min_salary:
        return int(cfg.min_salary)
    if profile is not None:
        floor = profile.application_answers.salary_floor
        if floor is not None and not is_todo(floor):
            digits = re.sub(r"[^\d]", "", str(floor))
            if digits:
                return int(digits)
    return None


def apply_filters(job: Any, cfg: MatchingConfig, profile: Profile | None = None,
                  now: datetime | None = None) -> FilterResult:
    """`job` is a Job row or JobPosting (title, location, remote, description, salary_*, posted_at)."""
    now = now or datetime.now(timezone.utc)
    reasons: list[str] = []
    title = (job.title or "").lower()
    desc = (job.description or "").lower()
    loc = (job.location or "").lower()

    keywords = [k.lower() for k in cfg.title_keywords_any]
    if profile is not None:
        keywords += [re.sub(r"\s*\(.*?\)", "", t).lower() for t in profile.targets.titles]
    if keywords and not any(_contains_phrase(title, k) for k in keywords):
        reasons.append("title: no target keyword")
    for ex in cfg.title_exclude:
        if _contains_phrase(title, ex.lower()):
            reasons.append(f"title: excluded term '{ex}'")

    is_remote = bool(job.remote) or "remote" in loc
    local_ok = any(_contains_phrase(loc, l.lower()) for l in cfg.locations_any) if cfg.locations_any else True
    if is_remote:
        remote_ok = cfg.allow_remote
        # "Remote - UK" style postings: require an allowed country when a country is named
        if remote_ok and re.search(r"remote\s*[-–(,:]\s*\w+", loc):
            remote_ok = any(_contains_phrase(loc, c.lower()) for c in cfg.remote_country_allow) or local_ok
        if not remote_ok and not local_ok:
            reasons.append(f"location: remote outside allowed regions ({job.location})")
    elif not local_ok:
        reasons.append(f"location: '{job.location or 'unknown'}' not in target locations")

    floor = effective_min_salary(cfg, profile)
    if floor and job.salary_max and job.salary_max < floor:
        reasons.append(f"salary: max {job.salary_max} below floor")

    if job.posted_at and cfg.max_posting_age_days:
        posted = job.posted_at if job.posted_at.tzinfo else job.posted_at.replace(tzinfo=timezone.utc)
        if now - posted > timedelta(days=cfg.max_posting_age_days):
            reasons.append(f"age: posted {(now - posted).days} days ago")

    if cfg.required_keywords_any and desc:
        if not any(_contains_phrase(desc, k.lower()) for k in cfg.required_keywords_any):
            reasons.append("description: none of the required keywords")
    for ex in cfg.excluded_keywords:
        if _contains_phrase(desc, ex.lower()) or _contains_phrase(title, ex.lower()):
            reasons.append(f"description: excluded keyword '{ex}'")

    return FilterResult(passed=not reasons, reasons=reasons)
