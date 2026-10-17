"""Email notifier (same interface). Approvals happen in the dashboard or CLI (`jobbuddy approve`)."""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

from jobbuddy.notifiers.base import ApprovalRequest, Decision, format_low_confidence


class EmailNotifier:
    name = "email"

    def __init__(self, host: str, port: int, user: str | None, password: str | None, to: str) -> None:
        self.host, self.port, self.user, self.password, self.to = host, port, user, password, to

    def build(self, subject: str, body: str, png: bytes | None) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.user or self.to
        msg["To"] = self.to
        msg.set_content(body)
        if png:
            msg.add_attachment(png, maintype="image", subtype="png", filename="form.png")
        return msg

    def _send(self, msg: EmailMessage) -> None:
        with smtplib.SMTP(self.host, self.port, timeout=30) as s:
            s.starttls()
            if self.user and self.password:
                s.login(self.user, self.password)
            s.send_message(msg)

    def send_approval_request(self, req: ApprovalRequest) -> None:
        body = (f"{req.job_title} @ {req.company}\n{req.job_url or ''}\n\n{format_low_confidence(req.low_confidence)}"
                f"\n\nApprove in the dashboard or run: jobbuddy approve {req.application_id}")
        self._send(self.build(f"[jobbuddy] Approve #{req.application_id}: {req.job_title}", body, req.screenshot_png))

    def send_alert(self, text: str, screenshot_png: bytes | None = None) -> None:
        self._send(self.build("[jobbuddy] Action needed", text, screenshot_png))

    def poll_decisions(self, state: dict[str, str]) -> list[Decision]:
        return []
