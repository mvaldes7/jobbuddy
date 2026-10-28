"""End-to-end: CI stages (SQLite + local HTML fixtures + mocked HTTP/Telegram) and the on-box agent queues
(mocked Neon HTTP endpoint). No LLM API, no real job site, nothing leaves the box."""
import json
from pathlib import Path

import httpx
import pytest
import respx

from jobbuddy.db.models import Application, ApplicationEvent, Approval, CandidateDocument, FieldQuestion, Screenshot
from jobbuddy.notifiers.telegram import TelegramNotifier
from jobbuddy.pipeline import (build_report, build_runtime, stage_decisions, stage_fetch, stage_fill,
                               stage_filter_embed, stage_notify, stage_submit)
from tests.conftest import FIXTURES, fixture_url

pytestmark = pytest.mark.browser
TOKEN = "123456:TEST_TOKEN_NOT_REAL_abcdefghijklmnop"
GOOD_LETTER = ("Dear Hiring Team,\n\nI am writing about the Senior Product Solution Architect role at Acme Health. "
               "A hard experience years ago showed me that healthcare technology either eases someone's worst moment "
               "or adds to it.\n\nI built it myself: a validated clinical trial site mobile application in three "
               "months. I also decreased bug resolution cycle time by 66%.\n\nI'd welcome the opportunity to talk "
               "further.\n\nSincerely,\nAlex Q. Example")


@pytest.fixture
def gh_payload():
    data = json.loads((FIXTURES / "greenhouse_jobs.json").read_text())
    data["jobs"][0]["absolute_url"] = fixture_url("greenhouse_page.html")  # local fixture, never a real site
    return data


async def test_full_pipeline_ci_and_agent_split(tmp_path, settings, session, profile, embedder, gh_payload,
                                                browser_context, app_config, agent):
    Path(settings.companies_path).write_text(
        "companies:\n  - {name: Acme Health, ats: greenhouse, board_token: acmehealth, active: true}\n")
    tg = TelegramNotifier(TOKEN, "777", allowed_user_id="777")
    rt = build_runtime(settings, embedder=embedder, notifier=tg, profile=profile)
    rt.cfg.engine = app_config.engine
    rt.cfg.fetch.request_delay_seconds = 0
    rt.cfg.matching.similarity_threshold = 0.0
    rt.workdir = tmp_path / "work"
    rt.workdir.mkdir()

    sent: list[dict] = []
    updates: list[dict] = []

    def tg_handler(request: httpx.Request):
        method = request.url.path.rsplit("/", 1)[-1]
        sent.append({"method": method, "body": request.content})
        if method == "getUpdates":
            out, updates[:] = list(updates), []
            return httpx.Response(200, json={"ok": True, "result": out})
        return httpx.Response(200, json={"ok": True, "result": {}})

    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://boards-api.greenhouse.io/v1/boards/acmehealth/jobs?content=true").mock(
            return_value=httpx.Response(200, json=gh_payload))
        mock.route(host="api.telegram.org").mock(side_effect=tg_handler)
        no_llm = mock.route(path__regex=r".*/chat/completions").mock(return_value=httpx.Response(500))

        # ---- CI: fetch + hard filter + embed (no LLM) ----
        assert (await stage_fetch(session, rt))["new"] == 2
        assert (await stage_fetch(session, rt))["new"] == 0  # idempotent
        fe = stage_filter_embed(session, rt)
        assert fe["filtered_out"] == 1 and fe["queued_for_agent"] == 1
        session.commit()

        # ---- agent: score + tailor over Neon HTTP ----
        (item,) = agent.pull("score", 5)["items"]
        assert agent.push_scores([{"job_id": item["job_id"], "score": 88, "reasons": ["clinical + AI fit"]}])[
            "applications_created"] == 1
        (t,) = agent.pull("tailor", 5)["items"]
        payload = {"items": [{"application_id": t["application_id"], "skills_order": ["AI Agent Development"],
                              "bullets": t["baseline"]["bullets"][:4], "cover_letter": GOOD_LETTER}]}
        assert agent.validate_tailoring(payload)["ok"], agent.validate_tailoring(payload)
        assert agent.push_tailoring(payload, render_pdf=False)["tailored"] == 1
        session.expire_all()

        # ---- CI: fill -> unknown open question -> needs_answers ----
        f = await stage_fill(session, rt, page_factory=browser_context)
        app = session.query(Application).one()
        assert f["needs_answers"] == 1 and app.status == "needs_answers"
        q = session.query(FieldQuestion).one()
        assert q.label == "Why do you want to work at Acme Health?" and q.status == "pending"
        qfid = q.field_id
        # legal/EEO/TODO fields are never queued for the agent
        assert not session.query(FieldQuestion).filter(FieldQuestion.label.ilike("%authorized%")).count()
        session.commit()

        # ---- agent answers; app returns to 'tailored' ----
        (qi,) = agent.pull("questions", 10)["items"]
        ans = {"answers": [{"question_id": qi["questions"][0]["question_id"],
                            "answer": "I personally built a validated clinical trial site mobile application."}]}
        assert agent.push_answers(ans)["applications_ready"] == [app.id]
        session.expire_all()

        # ---- CI: refill -> stops before Submit -> Telegram card ----
        f2 = await stage_fill(session, rt, page_factory=browser_context)
        assert f2["filled"] == 1 and app.status == "awaiting_approval"
        why = [s for st in app.filled_state["steps"] for s in st["mappings"] if s["field_id"] == qfid]
        assert why and why[0]["source"] == "agent" and "validated clinical trial" in why[0]["value"]
        assert session.query(Screenshot).filter_by(application_id=app.id, tag="filled").count() == 1
        assert not list(rt.workdir.rglob("*.png"))  # screenshots never left on disk
        docs = {d.kind for d in session.query(CandidateDocument).filter_by(application_id=app.id)}
        assert docs == {"tailored_resume", "cover_letter"}  # uploaded by the agent, used by the filler

        assert stage_notify(session, rt)["notified"] == 1
        nonce1 = app.approval_nonce
        assert any(f"jb:approve:{app.id}:{nonce1}".encode() in c["body"] for c in sent if c["method"] == "sendPhoto")

        # Mike edits the TODO-backed required field from Telegram
        auth_id = "f1:job_application[answers_attributes][1][boolean_value]"
        updates.append({"update_id": 10, "message": {"chat": {"id": 777}, "from": {"id": 777},
                                                     "text": f"#{app.id} {auth_id} = Yes"}})
        d = stage_decisions(session, rt)
        assert d["edited"] == 1 and app.status == "tailored"

        await stage_fill(session, rt, page_factory=browser_context)
        assert app.status == "awaiting_approval"
        assert all(i["field_id"] != auth_id for i in app.low_confidence)
        stage_notify(session, rt)
        nonce2 = app.approval_nonce
        assert nonce2 and nonce2 != nonce1

        updates += [
            {"update_id": 11, "callback_query": {"id": "a", "data": f"jb:approve:{app.id}:{nonce1}",  # stale nonce
                                                 "from": {"id": 777}, "message": {"chat": {"id": 777}}}},
            {"update_id": 12, "callback_query": {"id": "b", "data": f"jb:approve:{app.id}:{nonce2}",
                                                 "from": {"id": 555}, "message": {"chat": {"id": 777}}}},  # wrong user
            {"update_id": 13, "callback_query": {"id": "c", "data": f"jb:approve:{app.id}:{nonce2}",
                                                 "from": {"id": 777}, "message": {"chat": {"id": 777}}}},
        ]
        d2 = stage_decisions(session, rt)
        assert d2["approved"] == 1 and d2["decisions_rejected"] == 1
        assert app.status == "approved" and app.approved_via == "telegram"

        # submission is retired: even an approved app with --confirm is never submitted
        assert (await stage_submit(session, rt, page_factory=browser_context))["submitted"] == 0
        out = await stage_submit(session, rt, confirm=True, page_factory=browser_context)
        assert out["submitted"] == 0 and out.get("disabled") is True
        assert app.status == "approved" and app.submitted_at is None
        assert not no_llm.called

    rep = build_report(session)
    assert rep["submitted_total"] == 0 and rep["applications_by_status"] == {"approved": 1}
    assert rep["agent_queues"] == {"score": 0, "tailor": 0, "questions": 0}
