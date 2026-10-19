from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass
class ApprovalRequest:
    application_id: int
    nonce: str | None
    job_title: str
    company: str | None
    job_url: str | None
    score: int | None
    screenshot_png: bytes | None
    low_confidence: list[dict[str, Any]] = field(default_factory=list)
    cover_letter_excerpt: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class Decision:
    application_id: int
    action: Literal["approve", "edit", "skip"]
    via: str
    edits: dict[str, Any] = field(default_factory=dict)
    actor: str = "mike"
    nonce: str | None = None             # from the button's callback data (Telegram)
    telegram_user_id: str | None = None
    # Reply to an Edit prompt: "<n> = value" lines, resolved to field_ids by the pipeline (pipeline state).
    numbered_edits: dict[int, str] = field(default_factory=dict)
    reply_to_message_id: int | None = None


class Notifier(Protocol):
    name: str

    def send_approval_request(self, req: ApprovalRequest) -> None: ...
    def send_alert(self, text: str, screenshot_png: bytes | None = None) -> None: ...
    def poll_decisions(self, state: dict[str, str]) -> list[Decision]: ...


class NullNotifier:
    name = "none"

    def __init__(self) -> None:
        self.sent: list[Any] = []

    def send_approval_request(self, req: ApprovalRequest) -> None:
        self.sent.append(req)

    def send_alert(self, text: str, screenshot_png: bytes | None = None) -> None:
        self.sent.append(text)

    def poll_decisions(self, state: dict[str, str]) -> list[Decision]:
        return []


def format_low_confidence(items: list[dict[str, Any]], limit: int = 12) -> str:
    if not items:
        return "All fields mapped with high confidence."
    lines = []
    blank = [it for it in items if it.get("source") == "verify"]
    if blank:
        lines.append(f"\u26a0\ufe0f {len(blank)} REQUIRED field(s) still EMPTY on the form - submit is blocked until "
                     "they are filled: " + "; ".join(str(it.get("label") or it["field_id"])[:80] for it in blank[:6]))
    lines.append("Fields needing review:")
    for it in items[:limit]:
        val = it.get("value")
        val_s = "(empty)" if val in (None, "", []) else str(val)[:60]
        req = " *required*" if it.get("required") else ""
        sugg = ", ".join(str(s) for s in (it.get("suggestions") or [])[:4])
        lines.append(f"- {it.get('label') or it['field_id']}{req}: {val_s} "
                     f"(conf {float(it.get('confidence') or 0):.2f}){' | try: ' + sugg if sugg else ''}"
                     f"{' | ' + it['reason'] if it.get('reason') else ''}")
    if len(items) > limit:
        lines.append(f"...and {len(items) - limit} more (see dashboard)")
    return "\n".join(lines)


def build_notifier(settings: Any) -> Notifier:
    choice = (settings.notifier or "").lower()
    if choice in ("", "telegram") and settings.telegram_bot_token and settings.telegram_chat_id:
        from jobbuddy.notifiers.telegram import TelegramNotifier

        return TelegramNotifier(settings.telegram_bot_token, settings.telegram_chat_id,
                                allowed_user_id=settings.telegram_allowed_user_id)
    if choice in ("", "slack") and settings.slack_bot_token and settings.slack_channel_id:
        from jobbuddy.notifiers.slack import SlackNotifier

        return SlackNotifier(settings.slack_bot_token, settings.slack_channel_id)
    # Email notifications are opt-in only (NOTIFIER=email); alert e-mails are ingested by the on-box agent; jobbuddy never sends mail unless NOTIFIER=email.
    if choice == "email" and settings.smtp_host and settings.notify_email_to:
        from jobbuddy.notifiers.email import EmailNotifier

        return EmailNotifier(settings.smtp_host, settings.smtp_port or 587, settings.smtp_user,
                             settings.smtp_password, settings.notify_email_to)
    return NullNotifier()
