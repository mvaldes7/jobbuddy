"""Greenhouse public Job Board API: GET boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"""
from __future__ import annotations

from typing import Any

from jobbuddy.fetchers.base import CompanyTarget, Fetcher, JobPosting, guess_remote, html_to_text, parse_iso, parse_salary_text

API = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true"


def parse_greenhouse(data: dict[str, Any], company_name: str | None = None) -> list[JobPosting]:
    out = []
    for j in data.get("jobs", []):
        desc = html_to_text(j.get("content"))
        loc = (j.get("location") or {}).get("name")
        smin, smax = parse_salary_text(desc)
        out.append(JobPosting(
            source="greenhouse",
            external_job_id=str(j["id"]),
            title=j.get("title", "").strip(),
            company_name=company_name or j.get("company_name"),
            location=loc,
            remote=guess_remote(loc, j.get("title")),
            url=j.get("absolute_url"),
            apply_url=j.get("absolute_url"),
            description=desc,
            salary_min=smin, salary_max=smax, salary_currency="USD" if smin else None,
            posted_at=parse_iso(j.get("first_published") or j.get("updated_at")),
        ))
    return out


class GreenhouseFetcher(Fetcher):
    source = "greenhouse"

    async def fetch(self, target: CompanyTarget) -> list[JobPosting]:
        data = await self.http.get_json(API.format(token=target.board_token))
        return parse_greenhouse(data, target.name)
