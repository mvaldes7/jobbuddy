"""The ONLY place an ApprovalToken is created.

Requires an unconsumed `approve` row in `approvals`; that row is consumed atomically
(UPDATE ... WHERE consumed_at IS NULL) right here, before the filler is allowed to click Submit.
One approval => at most one submission attempt.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from jobbuddy.db.models import Application
from jobbuddy.db.repository import active_approval, consume_approval, submissions_today
from jobbuddy.db.states import AppStatus
from jobbuddy.engine.filler import ApprovalToken


class SubmissionRefused(RuntimeError):
    pass


def issue_token(session: Session, app: Application, *, daily_cap: int, dry_run: bool) -> ApprovalToken:
    if dry_run:
        raise SubmissionRefused("dry-run mode: submissions disabled")
    if app.status != AppStatus.APPROVED.value or app.approved_at is None:
        raise SubmissionRefused(f"application {app.id} is {app.status}, not approved")
    approval = active_approval(session, app)
    if approval is None:
        raise SubmissionRefused(f"application {app.id} has no unconsumed approval")
    if submissions_today(session) >= daily_cap:
        raise SubmissionRefused(f"daily application cap reached ({daily_cap})")
    if not consume_approval(session, approval.id):
        raise SubmissionRefused(f"approval {approval.id} was already consumed")
    session.commit()  # make consumption durable BEFORE any click happens
    return ApprovalToken(application_id=app.id, approved_via=approval.decided_via, _issued_by_submitter=True)
