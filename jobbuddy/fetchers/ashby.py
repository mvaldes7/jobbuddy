"""Ashby public Job Posting API: GET api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true"""
from __future__ import annotations

from typing import Any

from jobbuddy.fetchers.base import CompanyTarget, Fetcher, JobPosting, guess_remote, html_to_text, parse_iso

API = "https://api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true"


def _comp(job: dict[str, Any]) -> tuple[int | None, int | None, str | None]:
    comp = job.get("compensation") or {}
    for tier in comp.get("compensationTiers") or []:
        for c in tier.get("components") or []:
            if (c.get("compensationType") or "").lower() == "salary":
                lo, hi = c.get("minValue"), c.get("maxValue")
                return (int(lo) if lo else None, int(hi) if hi else None, c.get("currencyCode"))
    return None, None, None


def parse_ashby(data: dict[str, Any], company_name: str | None = None) -> list[JobPosting]:
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        loc = j.get("location")
        smin, smax, cur = _comp(j)
        remote = j.get("isRemote")
        if remote is None:
            remote = (j.get("workplaceType") or "").lower() == "remote" or guess_remote(loc)
        out.append(JobPosting(
            source="ashby",
            external_job_id=str(j.get("id") or j.get("jobUrl")),
            title=(j.get("title") or "").strip(),
            company_name=company_name,
            location=loc,
            remote=bool(remote) if remote is not None else None,
            url=j.get("jobUrl"),
            apply_url=j.get("applyUrl") or j.get("jobUrl"),
            description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml")),
            salary_min=smin, salary_max=smax, salary_currency=cur,
            posted_at=parse_iso(j.get("publishedAt")),
        ))
    return out


class AshbyFetcher(Fetcher):
    source = "ashby"

    async def fetch(self, target: CompanyTarget) -> list[JobPosting]:
        data = await self.http.get_json(API.format(name=target.board_token))
        return parse_ashby(data, target.name)
