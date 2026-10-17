"""On-box agent work queues over the MOCKED Neon HTTP endpoint (SQLite-backed). No network, no LLM API."""
import json

import pytest
from typer.testing import CliRunner

from jobbuddy.db.models import (Application, ApplicationEvent, CandidateDocument, FieldQuestion, FormFieldMapping,
                                Job, JobScore)
from jobbuddy.fetchers.base import JobPosting
from jobbuddy.db import repository as repo

LETTER = ("Dear Hiring Team,\n\nI am writing to express my strong interest in the Technical Product Manager role at "
          "Acme Health. Years ago, a hard experience showed me that healthcare technology either eases someone's "
          "worst moment or adds to it.\n\nI personally designed and built a clinical trial site mobile application "
          "using LLM-assisted development, delivering a fully validated app in three months. I also decreased bug "
          "resolution cycle time by 66%.\n\nThank you for your consideration.\n\nSincerely,\nAlex Q. Example")


def _filtered_job(session, title="Technical Product Manager", ext="j1", sim=0.61):
    job, _ = repo.upsert_job(session, JobPosting(
        source="greenhouse", external_job_id=ext, title=title, company_name="Acme Health",
        location="San Francisco, CA", apply_url=f"file:///tmp/{ext}.html",
        description="Own the roadmap for AI agents and clinical trial software. JIRA, agile, LLM experience."))
    session.flush()
    job.status = "filtered"
    session.add(JobScore(job_id=job.id, filter_passed=True, similarity=sim, embedding_model="hashing"))
    session.commit()
    return job


def test_score_queue_pull_and_push(session, agent):
    j1 = _filtered_job(session)
    j2 = _filtered_job(session, "Product Manager", "j2", sim=0.4)
    out = agent.pull("score", 10)
    assert [i["job_id"] for i in out["items"]] == [j1.id, j2.id]  # best similarity first
    assert "Score rubric" in out["instructions"] and out["threshold"] == 70
    assert "email" not in json.dumps(out["profile"]).lower()  # no contact details to the agent
    assert out["items"][0]["similarity"] == pytest.approx(0.61)

    res = agent.push_scores({"scores": [{"job_id": j1.id, "score": 84, "reasons": ["clinical + AI"]},
                                        {"job_id": j2.id, "score": 55, "gaps": ["thin description"]}]})
    assert res == {"scored": 1, "below_threshold": 1, "stale": 0, "applications_created": 1}
    session.expire_all()
    assert session.get(Job, j1.id).status == "scored" and session.get(Job, j2.id).status == "below_threshold"
    app = session.query(Application).one()
    assert app.job_id == j1.id and app.status == "scored" and app.ats == "greenhouse"
    sc = session.query(JobScore).filter_by(job_id=j1.id).order_by(JobScore.id.desc()).first()
    assert sc.llm_score == 84 and sc.llm_model == "onbox-agent" and sc.similarity == pytest.approx(0.61)
    assert sc.llm_reasons["reasons"] == ["clinical + AI"]
    assert [e.to_status for e in session.query(ApplicationEvent)] == ["scored"]
    # replaying the same push is a no-op (guarded by the expected status)
    assert agent.push_scores([{"job_id": j1.id, "score": 90}])["stale"] == 1
    assert session.query(Application).count() == 1
    # every write went over the Neon protocol as one batch transaction
    batches = [r for r in agent.t.mock.requests if "queries" in r["body"]]
    assert batches and all(r["headers"]["neon-batch-isolation-level"] == "ReadCommitted" for r in batches)


def _scored_app(session, agent):
    j = _filtered_job(session)
    agent.push_scores([{"job_id": j.id, "score": 80}])
    session.expire_all()
    return session.query(Application).one()


def test_validate_tailoring_blocks_fabrication(session, agent):
    app = _scored_app(session, agent)
    bad = {"items": [{"application_id": app.id,
                      "bullets": [{"source_id": "ex-bug-cycle",
                                   "text": "Decreased bug resolution cycle time by 80% using Snowflake."},
                                  {"source_id": "ex-made-up", "text": "Won an award."}],
                      "skills_order": ["Kubernetes", "JIRA"],
                      "cover_letter": LETTER.replace("66%", "75%") + "\nI am an expert in Salesforce CPQ.",
                      "answers": {"What are your salary expectations?": "$200k"}}]}
    rep = agent.validate_tailoring(bad)
    errs = " | ".join(rep["items"][0]["errors"])
    assert rep["ok"] is False
    for needle in ("80%", "ex-made-up", "Kubernetes", "75%", "sensitive question"):
        assert needle in errs, needle
    out = agent.push_tailoring(bad)
    assert out["rejected"] == 1 and out["tailored"] == 0
    session.expire_all()
    assert session.get(Application, app.id).status == "scored"
    assert session.query(CandidateDocument).count() == 0


def test_push_tailoring_renders_uploads_and_advances(session, agent):
    app = _scored_app(session, agent)
    good = {"items": [{"application_id": app.id,
                       "summary": None,
                       "skills_order": ["AI Agent Development", "JIRA"],
                       "bullets": [{"source_id": "ex-mobile-app",
                                    "text": "Personally designed and built a clinical trial site mobile application "
                                            "using LLM-assisted development, delivering a fully validated app in "
                                            "three months."},
                                   {"source_id": "ex-releases",
                                    "text": "Delivered 7 product releases with a 30% drop in post-release issues."}],
                       "cover_letter": LETTER,
                       "answers": {"Why do you want to work at Acme Health?":
                                   "I personally built a validated clinical trial site mobile application."}}]}
    assert agent.validate_tailoring(good)["ok"] is True
    out = agent.push_tailoring(good, render_pdf=False)
    assert out["tailored"] == 1, out
    session.expire_all()
    a = session.get(Application, app.id)
    assert a.status == "tailored" and a.cover_letter_text == LETTER
    assert a.tailored_resume["roles"][0]["bullets"][0]["source_id"] == "ex-mobile-app"
    assert a.answers["Why do you want to work at Acme Health?"]["source"] == "agent"
    docs = session.query(CandidateDocument).filter_by(application_id=app.id).all()
    assert sorted(d.kind for d in docs) == ["cover_letter", "tailored_resume"]
    assert all(d.content[:2] == b"PK" and d.size_bytes == len(d.content) for d in docs)  # real DOCX bytes
    assert not list(agent.workdir.rglob("*.docx"))  # local renders removed after upload
    assert agent.push_tailoring(good, render_pdf=False)["stale"] == 1


def _needs_answers_app(session, agent):
    app = _scored_app(session, agent)
    app.status = "needs_answers"
    session.add_all([
        FieldQuestion(application_id=app.id, ats="greenhouse", field_id="f1:q_why", label="Why do you want to work "
                      "at Acme Health?", label_normalized="why do you want to work at acme health?",
                      field_type="textarea", required=False, max_length=1500, status="pending"),
        FieldQuestion(application_id=app.id, ats="greenhouse", field_id="f1:q_tool", label="Which tracker do you use?",
                      label_normalized="which tracker do you use?", field_type="select", required=True,
                      options=["JIRA", "Linear", "Asana"], status="pending"),
        FieldQuestion(application_id=app.id, ats="greenhouse", field_id="f1:q_pay", label="Desired salary",
                      label_normalized="desired salary", field_type="text", required=True, status="pending"),
    ])
    session.commit()
    return app


def test_questions_queue_answers_and_requeue_for_fill(session, agent):
    app = _needs_answers_app(session, agent)
    pulled = agent.pull("questions", 20)
    (item,) = pulled["items"]
    assert item["application_id"] == app.id and len(item["questions"]) == 3
    qids = {q["label"]: q["question_id"] for q in item["questions"]}
    tool = next(q for q in item["questions"] if q["type"] == "select")
    assert tool["options"] == ["JIRA", "Linear", "Asana"] and tool["required"] is True

    bad = {"answers": [{"question_id": qids["Which tracker do you use?"], "answer": "Trello"},
                       {"question_id": qids["Desired salary"], "answer": "$190k"}]}
    rep = agent.validate_answers(bad)
    assert rep["ok"] is False
    assert "not one of the options" in rep["items"][0]["errors"][0]
    assert "sensitive" in rep["items"][1]["errors"][0]

    good = {"answers": [
        {"question_id": qids["Why do you want to work at Acme Health?"],
         "answer": "I personally built a validated clinical trial site mobile application in three months."},
        {"question_id": qids["Which tracker do you use?"], "answer": "JIRA", "reusable": True},
        {"question_id": qids["Desired salary"], "skip": True, "notes": "salary: Mike answers"}]}
    assert agent.validate_answers(good)["ok"] is True
    out = agent.push_answers(good)
    assert out["answered"] == 2 and out["dismissed"] == 1 and out["learned"] == 1
    assert out["applications_ready"] == [app.id]
    session.expire_all()
    assert session.get(Application, app.id).status == "tailored"
    rows = {q.field_id: q for q in session.query(FieldQuestion)}
    assert rows["f1:q_tool"].answer == "JIRA" and rows["f1:q_tool"].answered_by == "agent"
    assert rows["f1:q_pay"].status == "dismissed" and rows["f1:q_pay"].answer is None
    learned = session.query(FormFieldMapping).one()
    assert (learned.ats, learned.label_normalized, learned.static_value, learned.source) == \
        ("greenhouse", "which tracker do you use?", "JIRA", "agent")
    # the CI mapper sees answered questions by field id and label
    from jobbuddy.pipeline import agent_answers_for

    aa = agent_answers_for(session, session.get(Application, app.id))
    assert aa["f1:q_tool"] == "JIRA" and aa["which tracker do you use?"] == "JIRA" and "f1:q_pay" not in aa
    assert agent.push_answers(good)["stale"] == 3


def test_cli_agent_commands_over_neon_http(monkeypatch, tmp_path, session, neon, db_url):
    """The CLI defaults Postgres URLs to neon_http; here the transport factory returns the mocked one."""
    import jobbuddy.db.transport as tr
    import jobbuddy.profile as prof
    from jobbuddy.config import reset_settings_cache
    from jobbuddy.cli import app as cli
    from tests.conftest import NEON_TEST_URL, ROOT

    j = _filtered_job(session)
    monkeypatch.setenv("DATABASE_URL", NEON_TEST_URL)
    monkeypatch.delenv("DATABASE_TRANSPORT", raising=False)
    monkeypatch.setattr(prof, "_default_profile_path", lambda: ROOT / "profile" / "profile.example.yaml")
    chosen = {}

    def fake_build(settings):
        chosen["transport"] = settings.database_transport
        return neon

    monkeypatch.setattr(tr, "build_transport", fake_build)
    reset_settings_cache()
    try:
        r = CliRunner().invoke(cli, ["agent-pull", "--kind", "score", "--limit", "5"])
        assert r.exit_code == 0, r.output
        assert chosen["transport"] == "neon_http"
        assert json.loads(r.output)["items"][0]["job_id"] == j.id
        f = tmp_path / "scores.json"
        f.write_text(json.dumps({"scores": [{"job_id": j.id, "score": 77}]}))
        r2 = CliRunner().invoke(cli, ["agent-push-scores", str(f)])
        assert r2.exit_code == 0 and json.loads(r2.output)["applications_created"] == 1
        f.write_text(json.dumps({"scores": [{"job_id": j.id, "score": "lots"}]}))
        r3 = CliRunner().invoke(cli, ["agent-push-scores", str(f)])
        assert r3.exit_code == 2 and "schema_errors" in r3.output
    finally:
        reset_settings_cache()
