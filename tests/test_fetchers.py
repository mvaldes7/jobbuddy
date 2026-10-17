import json

import httpx
import pytest
import respx

from jobbuddy.db import repository as repo
from jobbuddy.db.models import Job
from jobbuddy.fetchers.ashby import parse_ashby
from jobbuddy.fetchers.bamboohr import BambooHRFetcher
from jobbuddy.fetchers.base import CompanyTarget, PoliteHttp, html_to_text, parse_relative_posted, parse_salary_text
from jobbuddy.fetchers.greenhouse import GreenhouseFetcher, parse_greenhouse
from jobbuddy.fetchers.lever import parse_lever
from jobbuddy.fetchers.workday import WorkdayFetcher
from tests.conftest import FIXTURES


def load(name):
    return json.loads((FIXTURES / name).read_text())


def test_parse_greenhouse_decodes_html_and_salary():
    jobs = parse_greenhouse(load("greenhouse_jobs.json"), "Acme Health")
    assert len(jobs) == 2
    j = jobs[0]
    assert j.source == "greenhouse" and j.external_job_id == "4012345"
    assert "AI agent" in j.description and "<" not in j.description
    assert (j.salary_min, j.salary_max) == (160000, 195000)
    assert j.posted_at is not None and j.location == "San Francisco, CA"


def test_parse_lever_remote_and_salary():
    (j,) = parse_lever(load("lever_postings.json"), "Example Bio")
    assert j.remote is True and j.salary_max == 185000
    assert "Healthcare experience" in j.description
    assert j.apply_url.endswith("/apply")


def test_parse_ashby_skips_unlisted():
    jobs = parse_ashby(load("ashby_board.json"), "Example AI")
    assert [j.title for j in jobs] == ["Solutions Architect (HealthTech)"]
    assert jobs[0].salary_min == 170000 and jobs[0].salary_currency == "USD"


def test_helpers():
    assert html_to_text("&lt;p&gt;Hi&lt;/p&gt;&lt;li&gt;x&lt;/li&gt;") == "Hi\nx"
    assert parse_salary_text("pay $150k-$190k") == (150000, 190000)
    assert parse_relative_posted("Posted 3 Days Ago") is not None
    assert parse_relative_posted("whenever") is None


@pytest.mark.asyncio
@respx.mock
async def test_greenhouse_fetcher_http():
    respx.get("https://boards-api.greenhouse.io/v1/boards/acmehealth/jobs?content=true").mock(
        return_value=httpx.Response(200, json=load("greenhouse_jobs.json")))
    async with PoliteHttp("test-agent", delay=0) as http:
        jobs = await GreenhouseFetcher(http).fetch(CompanyTarget(name="Acme Health", ats="greenhouse",
                                                                 board_token="acmehealth"))
    assert len(jobs) == 2


@pytest.mark.asyncio
@respx.mock
async def test_workday_fetcher_posts_cxs_and_merges_detail():
    host, tenant, site = "example.wd5.myworkdayjobs.com", "example", "External"
    route = respx.post(f"https://{host}/wday/cxs/{tenant}/{site}/jobs").mock(
        return_value=httpx.Response(200, json=load("workday_list.json")))
    respx.get(url__regex=rf"https://{host}/wday/cxs/{tenant}/{site}/job/.*").mock(
        return_value=httpx.Response(200, json=load("workday_detail.json")))
    target = CompanyTarget(name="Example Hospital", ats="workday",
                           workday={"host": host, "tenant": tenant, "site": site})
    async with PoliteHttp("test-agent", delay=0) as http:
        jobs = await WorkdayFetcher(http, page_size=20).fetch(target)
    body = json.loads(route.calls[0].request.content)
    assert body["limit"] == 20 and body["offset"] == 0
    assert jobs[0].external_job_id == f"{host}:R12345"
    assert jobs[0].salary_min == 155000
    assert jobs[0].apply_url.endswith("/apply")


@pytest.mark.asyncio
@respx.mock
async def test_bamboohr_fetcher():
    respx.get("https://exampleclinic.bamboohr.com/careers/list").mock(
        return_value=httpx.Response(200, json=load("bamboohr_list.json")))
    respx.get("https://exampleclinic.bamboohr.com/careers/57/detail").mock(
        return_value=httpx.Response(200, json=load("bamboohr_detail.json")))
    async with PoliteHttp("test-agent", delay=0) as http:
        (j,) = await BambooHRFetcher(http).fetch(CompanyTarget(name="Example Clinic", ats="bamboohr",
                                                               board_token="exampleclinic"))
    assert j.remote is True and j.salary_min == 140000 and "clinical data" in j.description


@pytest.mark.asyncio
@respx.mock
async def test_polite_http_retries_429():
    route = respx.get("https://api.lever.co/v0/postings/x?mode=json").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "0"}), httpx.Response(200, json=[])])
    async with PoliteHttp("t", delay=0) as http:
        assert await http.get_json("https://api.lever.co/v0/postings/x?mode=json") == []
    assert route.call_count == 2


def test_upsert_is_idempotent(session):
    company = repo.upsert_company(session, name="Acme Health", ats="greenhouse", board_token="acmehealth")
    postings = parse_greenhouse(load("greenhouse_jobs.json"), "Acme Health")
    for p in postings:
        repo.upsert_job(session, p, company)
    session.commit()
    first_seen = {j.external_job_id: j.first_seen_at for j in session.query(Job)}
    changed = postings[0].model_copy(update={"title": "Senior Product Solution Architect II"})
    _, created = repo.upsert_job(session, changed, company)
    repo.upsert_job(session, postings[1], company)
    session.commit()
    assert created is False
    assert session.query(Job).count() == 2
    j = session.query(Job).filter_by(source="greenhouse", external_job_id="4012345").one()
    assert j.title.endswith("II") and j.first_seen_at == first_seen["4012345"]
