"""BambooHR public careers JSON: GET https://{company}.bamboohr.com/careers/list (+ /careers/{id}/detail)."""
from __future__ import annotations

import logging
from typing import Any

from jobbuddy.fetchers.base import CompanyTarget, Fetcher, JobPosting, guess_remote, html_to_text, parse_iso, parse_salary_text

log = logging.getLogger(__name__)


def _loc(j: dict[str, Any]) -> str | None:
    loc = j.get("location") or {}
    ats = j.get("atsLocation") or {}
    parts = [loc.get("city") or ats.get("city"), loc.get("state") or ats.get("state") or ats.get("province"),
             ats.get("country")]
    s = ", ".join(p for p in parts if p)
    return s or None


def parse_bamboohr_list(data: dict[str, Any], company: str, company_name: str | None) -> list[JobPosting]:
    out = []
    for j in data.get("result", []):
        jid = str(j["id"])
        loc = _loc(j)
        remote = bool(j.get("isRemote")) or str(j.get("locationType")) == "1" or bool(guess_remote(loc))
        url = f"https://{company}.bamboohr.com/careers/{jid}"
        out.append(JobPosting(
            source="bamboohr", external_job_id=f"{company}:{jid}", title=(j.get("jobOpeningName") or "").strip(),
            company_name=company_name, location=loc or ("Remote" if remote else None), remote=remote,
            url=url, apply_url=url,
        ))
    return out


def merge_bamboohr_detail(p: JobPosting, detail: dict[str, Any]) -> JobPosting:
    jo = (detail.get("result") or {}).get("jobOpening") or {}
    desc = html_to_text(jo.get("description"))
    smin, smax = parse_salary_text(jo.get("compensation") or desc)
    return p.model_copy(update={"description": desc or p.description, "posted_at": parse_iso(jo.get("datePosted")),
                                "salary_min": smin, "salary_max": smax,
                                "salary_currency": "USD" if smin else None})


class BambooHRFetcher(Fetcher):
    source = "bamboohr"

    async def fetch(self, target: CompanyTarget) -> list[JobPosting]:
        company = target.board_token
        data = await self.http.get_json(f"https://{company}.bamboohr.com/careers/list")
        out = []
        for p in parse_bamboohr_list(data, company, target.name):
            jid = p.external_job_id.split(":", 1)[1]
            try:
                d = await self.http.get_json(f"https://{company}.bamboohr.com/careers/{jid}/detail")
                out.append(merge_bamboohr_detail(p, d))
            except Exception as exc:
                log.warning("bamboohr detail failed: %s", type(exc).__name__)
                out.append(p)
        return out
