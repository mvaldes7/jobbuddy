from __future__ import annotations

from jobbuddy.config import FetchConfig
from jobbuddy.fetchers.ashby import AshbyFetcher
from jobbuddy.fetchers.bamboohr import BambooHRFetcher
from jobbuddy.fetchers.base import Fetcher, PoliteHttp
from jobbuddy.fetchers.greenhouse import GreenhouseFetcher
from jobbuddy.fetchers.lever import LeverFetcher
from jobbuddy.fetchers.workday import WorkdayFetcher


def build_fetcher(ats: str, http: PoliteHttp, cfg: FetchConfig) -> Fetcher:
    if ats == "greenhouse":
        return GreenhouseFetcher(http)
    if ats == "lever":
        return LeverFetcher(http)
    if ats == "ashby":
        return AshbyFetcher(http)
    if ats == "workday":
        return WorkdayFetcher(http, page_size=cfg.workday_page_size, max_pages=cfg.workday_max_pages)
    if ats == "bamboohr":
        return BambooHRFetcher(http)
    raise ValueError(f"unsupported ATS: {ats}")
