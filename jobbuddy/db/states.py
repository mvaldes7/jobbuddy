"""Application state machine. jobbuddy no longer fills or submits forms: ready applications end in
`sent_to_user` (Mike applies himself). Legacy states are kept so existing rows stay valid."""
from __future__ import annotations

from enum import Enum


class JobStatus(str, Enum):
    FOUND = "found"
    FILTERED = "filtered"          # passed hard filters + embedded by CI -> waits for the on-box agent to SCORE it
    FILTERED_OUT = "filtered_out"  # failed hard filters
    SCORED = "scored"              # agent score at/above threshold -> an application is created (status 'scored')
    BELOW_THRESHOLD = "below_threshold"
    CLOSED = "closed"
    MANUAL_APPLY = "manual_apply"  # e-mail lead (e.g. Wellfound) with no employer-ATS match: Mike applies by hand


class AppStatus(str, Enum):
    FOUND = "found"
    FILTERED = "filtered"
    SCORED = "scored"            # waits for the on-box agent to TAILOR it (agent-pull --kind tailor)
    TAILORED = "tailored"        # resume/cover letter/answers pushed -> CI fills the form
    NEEDS_ANSWERS = "needs_answers"  # CI found form fields it can't fill -> agent answers field_questions
    FILLED = "filled"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    SUBMITTED = "submitted"
    FAILED = "failed"
    SKIPPED = "skipped"
    PAUSED = "paused"  # CAPTCHA / login / email-code: waiting for a human
    SENT_TO_USER = "sent_to_user"  # listing + cover letter PDF sent to Mike in Telegram; he applies himself


class QuestionStatus(str, Enum):
    PENDING = "pending"      # waiting for the on-box agent
    ANSWERED = "answered"    # agent (or Mike) answered; used by the next CI fill
    DISMISSED = "dismissed"  # not answerable by the agent (left for Mike on the approval card)


class Outcome(str, Enum):
    NONE = "none"
    INTERVIEW = "interview"
    REJECTED = "rejected"
    OFFER = "offer"
    GHOSTED = "ghosted"
    WITHDRAWN = "withdrawn"


APP_STATUSES = [s.value for s in AppStatus]
JOB_STATUSES = [s.value for s in JobStatus]
OUTCOMES = [o.value for o in Outcome]
QUESTION_STATUSES = [q.value for q in QuestionStatus]

# Who works each queue (see README "Queue contract").
AGENT_QUEUES = {
    "score": "jobs.status = 'filtered' (CI already hard-filtered and embedded them)",
    "tailor": "applications.status = 'scored'",
    "questions": "field_questions.status = 'pending' (application in 'needs_answers')",
}

TRANSITIONS: dict[AppStatus, set[AppStatus]] = {
    AppStatus.FOUND: {AppStatus.FILTERED, AppStatus.SKIPPED},
    AppStatus.FILTERED: {AppStatus.SCORED, AppStatus.SKIPPED},
    AppStatus.SCORED: {AppStatus.TAILORED, AppStatus.SKIPPED, AppStatus.FAILED},
    AppStatus.TAILORED: {AppStatus.SENT_TO_USER, AppStatus.FILLED, AppStatus.PAUSED, AppStatus.FAILED, AppStatus.SKIPPED, AppStatus.TAILORED,
                         AppStatus.NEEDS_ANSWERS},
    AppStatus.NEEDS_ANSWERS: {AppStatus.SENT_TO_USER, AppStatus.TAILORED, AppStatus.SKIPPED, AppStatus.FAILED},
    AppStatus.FILLED: {AppStatus.SENT_TO_USER, AppStatus.AWAITING_APPROVAL, AppStatus.FAILED, AppStatus.SKIPPED},
    AppStatus.AWAITING_APPROVAL: {AppStatus.SENT_TO_USER, AppStatus.APPROVED, AppStatus.SKIPPED, AppStatus.TAILORED, AppStatus.AWAITING_APPROVAL},
    AppStatus.APPROVED: {AppStatus.SENT_TO_USER, AppStatus.SUBMITTED, AppStatus.FAILED, AppStatus.PAUSED, AppStatus.SKIPPED},
    AppStatus.PAUSED: {AppStatus.SENT_TO_USER, AppStatus.TAILORED, AppStatus.APPROVED, AppStatus.SKIPPED, AppStatus.FAILED},
    AppStatus.FAILED: {AppStatus.SENT_TO_USER, AppStatus.TAILORED, AppStatus.SKIPPED},
    AppStatus.SENT_TO_USER: {AppStatus.SKIPPED, AppStatus.SENT_TO_USER},
    AppStatus.SUBMITTED: set(),
    AppStatus.SKIPPED: set(),
}


class InvalidTransition(RuntimeError):
    pass


def check_transition(current: str, new: str) -> None:
    cur, nxt = AppStatus(current), AppStatus(new)
    if nxt not in TRANSITIONS[cur]:
        raise InvalidTransition(f"illegal application transition {cur.value} -> {nxt.value}")
