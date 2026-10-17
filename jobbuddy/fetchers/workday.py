"""Workday candidate-experience JSON API (the same endpoint the public careers site calls):
POST https://{host}/wday/cxs/{tenant}/{site}/jobs  then  GET .../{site}{externalPath} for details."""
from __future__ import annotations

import logging
from typing import Any

from jobbuddy.fetchers.base import (
    CompanyTarget, Fetcher, JobPosting, guess_remote, html_to_text, parse_iso, parse_relative_posted, parse_salary_text,
)

log = logging.getLogger(__name__)


def list_url(host: str, tenant: str, site: str) -> str:
    return f"https://{host}/wday/cxs/{tenant}/{site}/jobs"


def detail_url(host: str, tenant: str, site: str, external_path: str) -> str:
    return f"https://{host}/wday/cxs/{tenant}/{site}{external_path}"


def public_url(host: str, site: str, external_path: str) -> str:
    return f"https://{host}/en-US/{site}{external_path}"


def parse_workday_list(data: dict[str, Any], host: str, site: str, company_name: str | None) -> list[JobPosting]:
    out = []
    for j in data.get("jobPostings", []):
        path = j.get("externalPath") or ""
        req = (j.get("bulletFields") or [None])[0] or path.rsplit("_", 1)[-1] or path
        loc = j.get("locationsText")
        url = public_url(host, site, path)
        out.append(JobPosting(
            source="workday",
            external_job_id=f"{host}:{req}",
            title=(j.get("title") or "").strip(),
            company_name=company_name,
            location=loc,
            remote=guess_remote(loc, j.get("remoteType")),
            url=url,
            apply_url=url + "/apply" if path else None,
            posted_at=parse_relative_posted(j.get("postedOn")),
            lead_meta={"externalPath": path},
        ))
    return out


def merge_workday_detail(p: JobPosting, detail: dict[str, Any]) -> JobPosting:
    info = detail.get("jobPostingInfo") or {}
    desc = html_to_text(info.get("jobDescription"))
    smin, smax = parse_salary_text(desc)
    upd = {
        "description": desc or p.description,
        "location": info.get("location") or p.location,
        "remote": guess_remote(info.get("location"), info.get("remoteType"), p.location) or p.remote,
        "posted_at": parse_iso(info.get("startDate")) or p.posted_at,
        "salary_min": smin, "salary_max": smax, "salary_currency": "USD" if smin else None,
    }
    if info.get("externalUrl"):
        upd["url"] = info["externalUrl"]
        upd["apply_url"] = info["externalUrl"].rstrip("/") + "/apply"
    return p.model_copy(update=upd)


class WorkdayFetcher(Fetcher):
    source = "workday"

    def __init__(self, http, page_size: int = 20, max_pages: int = 5) -> None:  # noqa: ANN001
        super().__init__(http)
        self.page_size = page_size
        self.max_pages = max_pages

    async def fetch(self, target: CompanyTarget) -> list[JobPosting]:
        wd = target.workday
        if wd is None:
            raise ValueError(f"{target.name}: workday config (host/tenant/site) required")
        postings: list[JobPosting] = []
        for page in range(self.max_pages):
            payload = {"appliedFacets": {}, "limit": self.page_size, "offset": page * self.page_size, "searchText": ""}
            data = await self.http.post_json(list_url(wd.host, wd.tenant, wd.site), payload)
            batch = parse_workday_list(data, wd.host, wd.site, target.name)
            postings.extend(batch)
            if len(batch) < self.page_size or len(postings) >= int(data.get("total") or 0):
                break
        detailed = []
        for p in postings:
            path = (p.lead_meta or {}).get("externalPath")
            try:
                d = await self.http.get_json(detail_url(wd.host, wd.tenant, wd.site, path)) if path else {}
                detailed.append(merge_workday_detail(p, d))
            except Exception as exc:  # keep the listing even if details fail
                log.warning("workday detail failed for %s: %s", target.name, type(exc).__name__)
                detailed.append(p)
        return detailed
