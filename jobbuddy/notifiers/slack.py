"""Slack notifier (same interface). Buttons need a Slack app Interactivity Request URL to deliver
clicks, so Slack is NOTIFICATION-ONLY here; approvals happen in Telegram or the dashboard."""
from __future__ import annotations

import logging
from typing import Any

import httpx

from jobbuddy.notifiers.base import ApprovalRequest, Decision, format_low_confidence

log = logging.getLogger(__name__)


class SlackNotifier:
    name = "slack"

    def __init__(self, token: str, channel: str, client: httpx.Client | None = None) -> None:
        self.token = token
        self.channel = channel
        self.http = client or httpx.Client(timeout=30, headers={"Authorization": f"Bearer {token}"})

    def _api(self, method: str, **kw: Any) -> dict[str, Any]:
        r = self.http.post(f"https://slack.com/api/{method}", **kw)
        data = r.json()
        if not data.get("ok"):
            log.warning("slack %s failed: %s", method, data.get("error"))
        return data

    def blocks(self, req: ApprovalRequest) -> list[dict[str, Any]]:
        return [
            {"type": "section", "text": {"type": "mrkdwn",
                                         "text": f"*#{req.application_id} {req.job_title}* @ {req.company or '?'}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": format_low_confidence(req.low_confidence)[:2900]}},
            {"type": "context", "elements": [{"type": "mrkdwn",
                                               "text": "Approve / Edit / Skip in Telegram or the dashboard."}]},
        ]

    def send_approval_request(self, req: ApprovalRequest) -> None:
        msg = self._api("chat.postMessage", json={"channel": self.channel, "text": f"Approve #{req.application_id}?",
                                                  "blocks": self.blocks(req)})
        if req.screenshot_png and msg.get("ts"):
            self._upload(req.screenshot_png, f"app{req.application_id}.png", thread_ts=msg["ts"])

    def _upload(self, content: bytes, filename: str, thread_ts: str | None = None) -> None:
        up = self._api("files.getUploadURLExternal", data={"filename": filename, "length": len(content)})
        if not up.get("ok"):
            return
        httpx.post(up["upload_url"], content=content, timeout=60)
        payload: dict[str, Any] = {"files": [{"id": up["file_id"], "title": filename}], "channel_id": self.channel}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        self._api("files.completeUploadExternal", json=payload)

    def send_alert(self, text: str, screenshot_png: bytes | None = None) -> None:
        msg = self._api("chat.postMessage", json={"channel": self.channel, "text": text[:3000]})
        if screenshot_png and msg.get("ts"):
            self._upload(screenshot_png, "alert.png", thread_ts=msg["ts"])

    def poll_decisions(self, state: dict[str, str]) -> list[Decision]:
        """Slack is notification-only: button clicks need an Interactivity endpoint (a server), and
        unauthenticated thread text is not accepted as an approval. Approve in Telegram or the dashboard."""
        return []
