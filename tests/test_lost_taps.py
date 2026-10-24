"""Taps swallowed by another getUpdates consumer must not vanish silently: detect the gap, re-send fresh cards."""
import httpx

from jobbuddy.db import repository as repo
from jobbuddy.pipeline import RESEND_CARDS_KEY, stage_decisions, stage_notify
from tests.test_edit_prompt import setup, tap


def test_update_id_gap_flags_and_resends_fresh_cards(session):
    fake, rt, app, nonce = setup(session)
    from jobbuddy.pipeline import utcnow
    app.notified_at = utcnow()
    repo.kv_set(session, "telegram_offset", "100")
    session.commit()
    fake.updates = [{"update_id": 105, "message": {"chat": {"id": 777}, "from": {"id": 777}, "text": "hi"}}]
    stats = stage_decisions(session, rt)
    session.commit()
    assert stats["cards_resent"] == 1
    assert app.approval_nonce is None and app.notified_at is None and app.status == "awaiting_approval"
    alerts = [b["text"] for m, b in fake.sent if m == "sendMessage"]
    assert any("101-104" in t and "tap again" in t for t in alerts)
    assert repo.kv_get(session, "telegram_offset") == "105"
    stage_notify(session, rt)
    assert app.approval_nonce and app.approval_nonce != nonce and app.notified_at is not None
    # the old card's button can no longer approve
    fake.updates = [tap(106, app.id, nonce, action="approve")]
    assert stage_decisions(session, rt)["decisions_inserted"] == 0
    assert app.status == "awaiting_approval"


def test_contiguous_updates_do_not_resend(session):
    fake, rt, app, nonce = setup(session)
    repo.kv_set(session, "telegram_offset", "100")
    session.commit()
    fake.updates = [tap(101, app.id, nonce, action="skip")]
    stats = stage_decisions(session, rt)
    assert stats["cards_resent"] == 0 and stats["decisions_inserted"] == 1


def test_resend_flag_honored_once(session):
    fake, rt, app, nonce = setup(session)
    repo.kv_set(session, RESEND_CARDS_KEY, "1")
    session.commit()
    assert stage_decisions(session, rt)["cards_resent"] == 1
    assert app.approval_nonce is None and repo.kv_get(session, RESEND_CARDS_KEY) == "0"
    assert stage_decisions(session, rt)["cards_resent"] == 0
