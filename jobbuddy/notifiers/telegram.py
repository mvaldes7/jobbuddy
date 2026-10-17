"""Telegram approvals via the plain HTTP Bot API (no webhook server needed: getUpdates polling).

Callback data format: "jb:<action>:<application_id>:<nonce>" (<= 64 bytes). An update is honored ONLY
if chat.id == TELEGRAM_CHAT_ID AND from.id == TELEGRAM_ALLOWED_USER_ID (defaults to the chat id,
which equals the user id in a private chat). The nonce is single-use and checked against the DB.
Edit flow: reply to the bot with lines like  `#12 field_id = value`; the form is re-filled with
the edits and a new approval card (new nonce) is sent.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from jobbuddy.notifiers.base import ApprovalRequest, Decision, format_low_confidence

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{token}/{method}"
EDIT_LINE_RE = re.compile(r"^\s*#?(\d+)\s+([^=]+?)\s*=\s*(.*)$")


CALLBACK_RE = re.compile(r"^jb:(approve|edit|skip):(\d+):([A-Za-z0-9_-]{8,48})$")


def approval_keyboard(app_id: int, nonce: str) -> dict[str, Any]:
    def cb(action: str) -> str:
        data = f"jb:{action}:{app_id}:{nonce}"
        assert len(data.encode()) <= 64, "Telegram callback_data limit"
        return data

    return {"inline_keyboard": [[
        {"text": "✅ Approve", "callback_data": cb("approve")},
        {"text": "✏️ Edit", "callback_data": cb("edit")},
        {"text": "⏭ Skip", "callback_data": cb("skip")},
    ]]}


def build_caption(req: ApprovalRequest) -> str:
    head = f"#{req.application_id} {req.job_title} @ {req.company or '?'}"
    if req.score is not None:
        head += f" (score {req.score})"
    return head[:1000]


class TelegramNotifier:
    name = "telegram"

    def __init__(self, token: str, chat_id: str, allowed_user_id: str | None = None,
                 client: httpx.Client | None = None) -> None:
        self.token = token
        self.chat_id = str(chat_id)
        self.allowed_user_id = str(allowed_user_id or chat_id)
        self.http = client or httpx.Client(timeout=30)

    def _call(self, method: str, **kw: Any) -> dict[str, Any]:
        r = self.http.post(API.format(token=self.token, method=method), **kw)
        if r.status_code >= 400:
            log.warning("telegram %s failed: http %s", method, r.status_code)  # never log the URL (has token)
            r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method} not ok")
        return data

    def send_approval_request(self, req: ApprovalRequest) -> None:
        caption = build_caption(req)
        if not req.nonce:
            raise ValueError("approval request needs a nonce")
        kb = json.dumps(approval_keyboard(req.application_id, req.nonce))
        if req.screenshot_png:
            self._call("sendPhoto", data={"chat_id": self.chat_id, "caption": caption, "reply_markup": kb},
                       files={"photo": (f"app{req.application_id}.png", req.screenshot_png, "image/png")})
        else:
            self._call("sendMessage", json={"chat_id": self.chat_id, "text": caption,
                                            "reply_markup": approval_keyboard(req.application_id, req.nonce)})
        body = [format_low_confidence(req.low_confidence)]
        if req.job_url:
            body.append(f"Posting: {req.job_url}")
        if req.cover_letter_excerpt:
            body.append("Cover letter opens: " + req.cover_letter_excerpt[:300])
        body += req.notes
        body.append(f"To edit: reply `#{req.application_id} <field_id> = <value>` (one per line), then Approve.")
        self._call("sendMessage", json={"chat_id": self.chat_id, "text": "\n\n".join(body)[:4000],
                                        "disable_web_page_preview": True})

    def send_alert(self, text: str, screenshot_png: bytes | None = None) -> None:
        if screenshot_png:
            self._call("sendPhoto", data={"chat_id": self.chat_id, "caption": text[:1000]},
                       files={"photo": ("alert.png", screenshot_png, "image/png")})
        else:
            self._call("sendMessage", json={"chat_id": self.chat_id, "text": text[:4000]})

    def parse_updates(self, updates: list[dict[str, Any]]) -> tuple[list[Decision], int | None]:
        decisions: list[Decision] = []
        edits: dict[int, dict[str, str]] = {}
        max_id: int | None = None
        for u in updates:
            max_id = max(max_id or 0, int(u["update_id"]))
            cq = u.get("callback_query")
            if cq:
                chat = str(((cq.get("message") or {}).get("chat") or {}).get("id"))
                user = str((cq.get("from") or {}).get("id"))
                if chat != self.chat_id or user != self.allowed_user_id:
                    log.warning("ignoring telegram callback from unauthorized chat/user")
                    continue
                m = CALLBACK_RE.match(cq.get("data") or "")
                if m:
                    decisions.append(Decision(application_id=int(m.group(2)), action=m.group(1),  # type: ignore[arg-type]
                                              via="telegram", nonce=m.group(3), telegram_user_id=user))
                    try:
                        self._call("answerCallbackQuery", json={"callback_query_id": cq["id"],
                                                                "text": f"{m.group(1)} recorded"})
                    except Exception:
                        pass
                continue
            msg = u.get("message")
            if (msg and str((msg.get("chat") or {}).get("id")) == self.chat_id
                    and str((msg.get("from") or {}).get("id")) == self.allowed_user_id):
                for line in (msg.get("text") or "").splitlines():
                    em = EDIT_LINE_RE.match(line)
                    if em:
                        edits.setdefault(int(em.group(1)), {})[em.group(2).strip()] = em.group(3).strip()
        for app_id, e in edits.items():
            decisions.append(Decision(application_id=app_id, action="edit", via="telegram", edits=e,
                                      telegram_user_id=self.allowed_user_id))
        # Approve/skip after edits in the same batch: order edits first so they apply before approval.
        decisions.sort(key=lambda d: 0 if (d.action == "edit" and d.edits) else 1)
        return decisions, max_id

    def poll_decisions(self, state: dict[str, str]) -> list[Decision]:
        offset = int(state.get("telegram_offset", "0") or 0)
        payload: dict[str, Any] = {"timeout": 0, "allowed_updates": ["callback_query", "message"]}
        if offset:
            payload["offset"] = offset + 1
        data = self._call("getUpdates", json=payload)
        decisions, max_id = self.parse_updates(data.get("result", []))
        if max_id is not None:
            state["telegram_offset"] = str(max_id)
        return decisions
