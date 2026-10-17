import json

import httpx
import respx

from jobbuddy.notifiers.base import ApprovalRequest, format_low_confidence
from jobbuddy.notifiers.telegram import TelegramNotifier, approval_keyboard

TOKEN = "123456:TEST_TOKEN_NOT_REAL_abcdefghijklmnop"


def cb(update_id, data, chat=777, user=777):
    return {"update_id": update_id, "callback_query": {"id": f"cq{update_id}", "data": data,
                                                       "from": {"id": user}, "message": {"chat": {"id": chat}}}}


def test_keyboard_carries_app_id_and_nonce_within_64_bytes():
    kb = approval_keyboard(12345, "n" * 24)
    datas = [b["callback_data"] for b in kb["inline_keyboard"][0]]
    assert datas[0] == "jb:approve:12345:" + "n" * 24 and all(len(d) <= 64 for d in datas)


def test_parse_updates_enforces_chat_and_user():
    t = TelegramNotifier(TOKEN, "777", allowed_user_id="777", client=httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"ok": True}))))
    updates = [
        cb(1, "jb:approve:5:goodnonce123"),
        cb(2, "jb:approve:6:goodnonce456", chat=999),          # wrong chat
        cb(3, "jb:approve:7:goodnonce789", user=555),          # right chat, wrong user (e.g. group member)
        cb(4, "jb:approve:8"),                                 # no nonce -> ignored
        {"update_id": 5, "message": {"chat": {"id": 777}, "from": {"id": 777},
                                     "text": "#5 q_1 = Yes\n#5 gender = Decline to answer"}},
        {"update_id": 6, "message": {"chat": {"id": 777}, "from": {"id": 555}, "text": "#9 q = hacked"}},
    ]
    decisions, max_id = t.parse_updates(updates)
    assert max_id == 6
    assert [(d.application_id, d.action) for d in decisions] == [(5, "edit"), (5, "approve")]
    assert decisions[0].edits == {"q_1": "Yes", "gender": "Decline to answer"}
    assert decisions[1].nonce == "goodnonce123" and decisions[1].telegram_user_id == "777"


@respx.mock
def test_send_approval_request_with_screenshot():
    photo = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendPhoto").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}}))
    msg = respx.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {}}))
    t = TelegramNotifier(TOKEN, "777")
    t.send_approval_request(ApprovalRequest(
        application_id=3, nonce="abcdefgh1234", job_title="PSA", company="Acme", job_url="https://x", score=88,
        screenshot_png=b"\x89PNG fake",
        low_confidence=[{"field_id": "auth", "label": "Authorized?", "required": True, "value": None,
                         "confidence": 0, "suggestions": ["Yes", "No"], "reason": "TODO"}]))
    req = photo.calls[0].request
    body = req.content
    assert b"jb:approve:3:abcdefgh1234" in body and b"\x89PNG fake" in body
    text = json.loads(msg.calls[0].request.content)["text"]
    assert "Authorized? *required*" in text and "try: Yes, No" in text


@respx.mock
def test_poll_uses_offset():
    route = respx.post(f"https://api.telegram.org/bot{TOKEN}/getUpdates").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": [cb(41, "jb:skip:2:nonce12345678")]}))
    respx.post(f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery").mock(
        return_value=httpx.Response(200, json={"ok": True}))
    t = TelegramNotifier(TOKEN, "777")
    state = {"telegram_offset": "40"}
    (d,) = t.poll_decisions(state)
    assert d.action == "skip" and state["telegram_offset"] == "41"
    assert json.loads(route.calls[0].request.content)["offset"] == 41


def test_format_low_confidence_empty():
    assert "high confidence" in format_low_confidence([])
