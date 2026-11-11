"""Dashboard helpers: URL normalization, outcome values, and the INSERT-only outcome path reconciling."""
import importlib.util

import pytest

from tests.conftest import ROOT

st = pytest.importorskip("streamlit")  # dashboard deps are optional for the pipeline test env


@pytest.fixture
def dash(monkeypatch):
    spec = importlib.util.spec_from_file_location("jobbuddy_dashboard_app", ROOT / "dashboard" / "app.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_outcomes_match_cli(dash):
    from jobbuddy.db.states import OUTCOMES

    assert dash.OUTCOMES == OUTCOMES


def test_normalize_url(dash):
    n = dash.normalize_url
    assert n("postgres://u:p@h-pooler.x/db?sslmode=require&channel_binding=require") == \
        "postgresql+psycopg://u:p@h-pooler.x/db?sslmode=require"
    assert n("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db?sslmode=require"
    assert n("sqlite:///./x.db") == "sqlite:///./x.db"


def test_threshold_reads_config(dash):
    import yaml

    cfg = yaml.safe_load((ROOT / "config" / "config.yaml").read_text())
    assert dash.score_threshold() == int(cfg["matching"]["agent_score_threshold"])


def test_dashboard_outcome_event_reconciles(dash, session, monkeypatch):
    from jobbuddy.db import repository as repo
    from jobbuddy.db.states import AppStatus
    from jobbuddy.fetchers.base import JobPosting

    job, _ = repo.upsert_job(session, JobPosting(source="ashby", external_job_id="d1", title="PM",
                                                 apply_url="https://jobs.ashbyhq.com/x/d1"))
    app = repo.get_or_create_application(session, job, "ashby")
    repo.transition(session, app, AppStatus.TAILORED)
    repo.transition(session, app, AppStatus.SENT_TO_USER)
    session.commit()

    monkeypatch.setattr(dash, "engine", lambda: session.get_bind())
    dash.insert_applied_event(app.id)
    dash.insert_outcome_event(app.id, "interview")
    with pytest.raises(ValueError):
        dash.insert_outcome_event(app.id, "applied")
    df = dash.sent_apps()
    assert df.loc[0, "outcome"] == "interview" and bool(df.loc[0, "applied"])

    session.expire_all()
    stats = repo.reconcile(session)
    assert stats["outcomes"] == 1 and app.outcome == "interview" and app.status == "sent_to_user"
