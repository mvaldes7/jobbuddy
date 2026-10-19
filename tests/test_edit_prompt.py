"""Telegram Edit button -> numbered field list + ForceReply prompt -> `<n> = value` reply -> re-fill."""
import json
from types import SimpleNamespace

import httpx

from jobbuddy.db import repository as repo
from jobbuddy.db.models import Approval
from jobbuddy.engine.field_mapper import FieldMapper, MapperContext
from jobbuddy.engine.adapters.base import get_adapter
from jobbuddy.notifiers.telegram import TelegramNotifier
from jobbuddy.pipeline import EDIT_PROMPTS_KEY, edit_field_items, stage_decisions
from tests.test_db_and_approvals import make_app
from tests.test_field_mapper import F

TOKEN = "123456:TEST_TOKEN_NOT_REAL_abcdefghijklmnop"
FILLED = {"steps": [{"index": 0, "url": "u", "action": "", "errors": [],
                     "fields": [{"field_id": "first_name", "label": "First Name"},
                                {"field_id": "salary", "label": "Desired salary"},
                                {"field_id": "resume", "label": "Resume"},
                                {"field_id": "relo", "label": "Willing to relocate?"}],
                     "mappings": [{"field_id": "first_name", "value": "Alex"},
                                  {"field_id": "salary", "value": None},
                                  {"field_id": "resume", "file_kind": "resume"},
                                  {"field_id": "relo", "value": True}]}]}


class FakeTG:
    def __init__(self):
        self.sent, self.updates, self.next_id = [], [], 100

    def __call__(self, request):
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}") if request.headers.get("content-type", "").startswith(
            "application/json") else {}
        self.sent.append((method, body))
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return httpx.Response(200, json={"ok": True, "result": out})
        self.next_id += 1
        return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_id}})


def setup(session):
    fake = FakeTG()
    tg = TelegramNotifier(TOKEN, "777", allowed_user_id="777", client=httpx.Client(transport=httpx.MockTransport(fake)))
    app = make_app(session)
    app.filled_state = FILLED
    app.human_edits = {"salary": "$140,000"}
    nonce = repo.issue_nonce(app)
    session.commit()
    return fake, SimpleNamespace(notifier=tg, cfg=SimpleNamespace(pipeline=SimpleNamespace(dry_run=False))), app, nonce


def msg(uid, text, reply_to=None, user=777):
    m = {"chat": {"id": 777}, "from": {"id": user}, "text": text}
    if reply_to is not None:
        m["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": uid, "message": m}


def tap(uid, app_id, nonce, action="edit"):
    return {"update_id": uid, "callback_query": {"id": f"cq{uid}", "data": f"jb:{action}:{app_id}:{nonce}",
                                                 "from": {"id": 777}, "message": {"chat": {"id": 777}}}}


def test_edit_field_items_numbers_fields_with_human_edits(session):
    app = make_app(session)
    app.filled_state, app.human_edits = FILLED, {"salary": "$140,000"}
    assert edit_field_items(app) == [(1, "first_name", "First Name", "Alex"), (2, "salary", "Desired salary", "$140,000"),
                                     (3, "resume", "Resume", "(resume file)"), (4, "relo", "Willing to relocate?", "Yes")]


def test_edit_tap_sends_prompt_then_numbered_reply_refills_without_approving(session):
    fake, rt, app, nonce = setup(session)
    fake.updates = [tap(1, app.id, nonce)]
    stage_decisions(session, rt)
    session.commit()
    msgs = [b for m, b in fake.sent if m == "sendMessage"]
    assert "2. Desired salary: $140,000" in msgs[0]["text"] and "1. First Name: Alex" in msgs[0]["text"]
    assert msgs[1]["reply_markup"] == {"force_reply": True, "input_field_placeholder": "e.g. 3 = $150,000"}
    prompt_id = fake.next_id
    prompts = json.loads(repo.kv_get(session, EDIT_PROMPTS_KEY))
    assert prompts[str(prompt_id)]["app_id"] == app.id and prompts[str(prompt_id)]["fields"]["2"] == "salary"
    assert app.status == "awaiting_approval" and app.approval_nonce is None  # old card is dead

    # an outsider's reply is ignored; Mike's reply to the prompt becomes human_edits
    fake.updates = [msg(2, "2 = $999", reply_to=prompt_id, user=555), msg(3, "2 = $150,000\n1 = Alexandra",
                                                                           reply_to=prompt_id)]
    stats = stage_decisions(session, rt)
    session.commit()
    assert stats["edited"] == 1 and stats["approved"] == 0
    assert app.human_edits == {"salary": "$150,000", "first_name": "Alexandra"}
    assert app.status == "tailored" and app.approval_nonce is None and app.notified_at is None
    assert app.approved_at is None
    assert not session.query(Approval).filter_by(application_id=app.id, decision="approve").count()
    assert json.loads(repo.kv_get(session, EDIT_PROMPTS_KEY)) == {}
    # the old Approve button cannot approve the re-filled form
    fake.updates = [tap(4, app.id, nonce, "approve")]
    stage_decisions(session, rt)
    assert app.status == "tailored"


def test_bare_numbered_reply_uses_single_outstanding_prompt(session):
    fake, rt, app, nonce = setup(session)
    fake.updates = [tap(1, app.id, nonce)]
    stage_decisions(session, rt)
    fake.updates = [msg(2, "4 = No\n9 = junk")]
    stage_decisions(session, rt)
    session.commit()
    assert app.human_edits["relo"] == "No" and app.status == "tailored"
    assert any("9" in (b.get("text") or "") and "unknown" in b.get("text", "") for m, b in fake.sent)


def test_bare_numbered_reply_without_prompt_is_rejected(session):
    fake, rt, app, nonce = setup(session)
    fake.updates = [msg(1, "2 = $1")]
    stats = stage_decisions(session, rt)
    assert stats["decisions_rejected"] == 1 and app.human_edits == {"salary": "$140,000"}
    assert app.status == "awaiting_approval"


def test_legacy_hash_edit_syntax_still_works(session):
    fake, rt, app, nonce = setup(session)
    fake.updates = [msg(1, f"#{app.id} salary = $160,000")]
    stage_decisions(session, rt)
    assert app.human_edits["salary"] == "$160,000" and app.status == "tailored"


def test_located_in_or_relocate_questions_map_to_relocate_answer(profile):
    profile.application_answers.willing_to_relocate = "Yes"
    m = FieldMapper(MapperContext(profile=profile, adapter=get_adapter("greenhouse"), job_title="PSA", company="Acme",
                                  llm=None, learned={}, overrides={}, cover_letter_text=lambda: ""))
    labels = ["Are you located in or willing to relocate to the SF Bay Area?",
              "Are you able to commute to our San Francisco office?",
              "Are you currently based in the Bay Area?", "Are you open to working in-office 3 days a week?"]
    ms = m.map_fields([F(f"q{i}", lab, "select", ["Yes", "No"]) for i, lab in enumerate(labels)])
    assert [x.value for x in ms] == ["Yes"] * 4, [(x.value, x.source) for x in ms]
