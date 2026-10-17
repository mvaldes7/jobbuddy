"""Logging with PII redaction. CI logs are visible to anyone with repo access, so redact aggressively."""
from __future__ import annotations

import logging
import re
import sys
from typing import Iterable

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)")
TOKEN_RE = re.compile(r"(?i)(bot\d{6,}:[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{16,}|xox[abp]-[A-Za-z0-9-]{10,})")
URL_CREDS_RE = re.compile(r"(\w+://)([^:/@\s]+):([^@/\s]+)@")


class PIIRedactingFilter(logging.Filter):
    """Masks emails, phone numbers, tokens, URL credentials and any extra literal strings (e.g. names)."""

    def __init__(self, extra_literals: Iterable[str] = ()) -> None:
        super().__init__()
        self.literals = sorted({s for s in extra_literals if s and len(s) >= 3}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        text = URL_CREDS_RE.sub(r"\1***:***@", text)
        text = TOKEN_RE.sub("[REDACTED_TOKEN]", text)
        text = EMAIL_RE.sub("[REDACTED_EMAIL]", text)
        text = PHONE_RE.sub("[REDACTED_PHONE]", text)
        for lit in self.literals:
            text = re.sub(re.escape(lit), "[REDACTED]", text, flags=re.IGNORECASE)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover
            return True
        record.msg = self.redact(msg)
        record.args = ()
        return True


_FILTER: PIIRedactingFilter | None = None


def setup_logging(level: str = "INFO", pii_literals: Iterable[str] = ()) -> logging.Logger:
    global _FILTER
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _FILTER = PIIRedactingFilter(pii_literals)
    handler.addFilter(_FILTER)
    root.addHandler(handler)
    # Third-party libraries can log full request URLs/bodies; keep them quiet.
    for noisy in ("httpx", "httpcore", "urllib3", "sentence_transformers", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return root


def add_pii_literals(literals: Iterable[str]) -> None:
    if _FILTER is not None:
        _FILTER.literals = sorted(set(_FILTER.literals) | {s for s in literals if s and len(s) >= 3},
                                  key=len, reverse=True)
