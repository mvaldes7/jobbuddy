"""run-ci ordering: ready applications are filled and notified before the (slow) fetch/filter stage."""
import types

import pytest

from jobbuddy import pipeline
from jobbuddy.config import AppConfig


def _rt(time_left):
    cfg = AppConfig()
    return types.SimpleNamespace(run_id="t-order", cfg=cfg, time_left=time_left)


def _record(monkeypatch, calls):
    def sync(name):
        def f(session, rt, *a, **k):
            calls.append(name)
            return {}
        return f

    def asyn(name):
        async def f(session, rt, *a, **k):
            calls.append(name)
            return {}
        return f

    for name in ("stage_decisions", "stage_notify", "stage_filter_embed", "stage_manual_digest"):
        monkeypatch.setattr(pipeline, name, sync(name))
    for name in ("stage_fill", "stage_fetch", "stage_submit"):
        monkeypatch.setattr(pipeline, name, asyn(name))
    monkeypatch.setattr(pipeline, "build_report", lambda session: {})


@pytest.mark.asyncio
async def test_run_ci_fills_and_notifies_before_fetch(monkeypatch, session):
    calls: list[str] = []
    _record(monkeypatch, calls)
    await pipeline.run_ci(session, _rt(lambda: 1200.0))
    assert calls == ["stage_decisions", "stage_fill", "stage_notify", "stage_submit", "stage_fetch",
                     "stage_filter_embed", "stage_manual_digest"]


@pytest.mark.asyncio
async def test_run_ci_still_fills_and_notifies_when_budget_is_short(monkeypatch, session):
    calls: list[str] = []
    _record(monkeypatch, calls)
    await pipeline.run_ci(session, _rt(lambda: 100.0))  # almost no budget: fetch is the stage that gives way
    assert calls[:3] == ["stage_decisions", "stage_fill", "stage_notify"]
    assert "stage_fetch" not in calls and "stage_submit" not in calls


@pytest.mark.asyncio
async def test_stage_fill_respects_its_time_slice(monkeypatch, session):
    """With an exhausted fill slice no browser is opened and the app stays 'tailored' for the next run."""
    from jobbuddy.db import repository as repo
    from jobbuddy.db.models import Application
    from jobbuddy.fetchers.base import JobPosting

    job, _ = repo.upsert_job(session, JobPosting(source="greenhouse", external_job_id="1", title="PM",
                                                 apply_url="https://example.com/apply"))
    app = Application(job_id=job.id, status="tailored", ats="greenhouse")
    session.add(app)
    session.flush()
    rt = _rt(lambda: 1200.0)
    rt.cfg.pipeline.fill_time_budget_minutes = 0
    opened = []

    class Ctx:
        async def new_page(self):
            opened.append(1)
            raise AssertionError("no fill should start")

    monkeypatch.setattr(pipeline, "_build_fill_context", lambda *a, **k: None)
    stats = await pipeline.stage_fill(session, rt, page_factory=Ctx())
    assert opened == [] and stats["skipped_time"] == 1 and app.status == "tailored"
