"""Lever public Postings API: GET api.lever.co/v0/postings/{company}?mode=json"""
from __future__ import annotations

from typing import Any

from jobbuddy.fetchers.base import CompanyTarget, Fetcher, JobPosting, guess_remote, html_to_text, parse_iso

API = "https://api.lever.co/v0/postings/{company}?mode=json"


def parse_lever(data: list[dict[str, Any]], company_name: str | None = None) -> list[JobPosting]:
    out = []
    for j in data:
        cats = j.get("categories") or {}
        loc = cats.get("location") or ", ".join(cats.get("allLocations") or []) or None
        parts = [j.get("descriptionPlain") or html_to_text(j.get("description"))]
        for lst in j.get("lists") or []:
            parts.append(lst.get("text", ""))
            parts.append(html_to_text(lst.get("content")))
        parts.append(j.get("additionalPlain") or html_to_text(j.get("additional")))
        sal = j.get("salaryRange") or {}
        workplace = (j.get("workplaceType") or "").lower()
        out.append(JobPosting(
            source="lever",
            external_job_id=str(j["id"]),
            title=(j.get("text") or "").strip(),
            company_name=company_name,
            location=loc,
            remote=True if workplace == "remote" else guess_remote(loc),
            url=j.get("hostedUrl"),
            apply_url=j.get("applyUrl") or j.get("hostedUrl"),
            description="\n".join(p for p in parts if p).strip(),
            salary_min=int(sal["min"]) if sal.get("min") else None,
            salary_max=int(sal["max"]) if sal.get("max") else None,
            salary_currency=sal.get("currency"),
            posted_at=parse_iso(j.get("createdAt")),
        ))
    return out


class LeverFetcher(Fetcher):
    source = "lever"

    async def fetch(self, target: CompanyTarget) -> list[JobPosting]:
        data = await self.http.get_json(API.format(company=target.board_token))
        return parse_lever(data, target.name)
