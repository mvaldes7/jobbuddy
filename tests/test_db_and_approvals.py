import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from jobbuddy.db import repository as repo
from jobbuddy.db.models import AppendOnlyViolation, ApplicationEvent, Approval, Job
from jobbuddy.db.states import AppStatus, InvalidTransition
from jobbuddy.engine.submitter import SubmissionRefused, issue_token
from jobbuddy.fetchers.base import JobPosting
from jobbuddy.vector_store import PythonVectorStore, get_vector_store


def make_app(session, status=AppStatus.AWAITING_APPROVAL):
    job, _ = repo.upsert_job(session, JobPosting(source="greenhouse", external_job_id="9", title="PSA",
                                                 apply_url="https://boards.greenhouse.io/x/jobs/9"))
    app = repo.get_or_create_application(session, job, "greenhouse")
    path = [AppStatus.TAILORED, AppStatus.FILLED, AppStatus.AWAITING_APPROVAL]
    for st in path:
        repo.transition(session, app, st)
        if st == status:
            break
    session.commit()
    return app


def test_state_machine_rejects_illegal_transitions(session):
    app = make_app(session, AppStatus.TAILORED)
    with pytest.raises(InvalidTransition):
        repo.transition(session, app, AppStatus.APPROVED)


def test_cannot_mark_submitted_without_consumed_approval(session):
    app = make_app(session)
    with pytest.raises(PermissionError):
        repo.transition(session, app, AppStatus.SUBMITTED)


def test_db_check_constraint_blocks_submitted_without_approval(session):
    app = make_app(session)
    with pytest.raises(IntegrityError):
        session.execute(text("UPDATE applications SET status='submitted' WHERE id=:i"), {"i": app.id})
        session.flush()
    session.rollback()


def test_events_are_append_only_orm_and_db(session):
    app = make_app(session)
    ev = session.query(ApplicationEvent).filter_by(application_id=app.id).first()
    ev.note = "tamper"
    with pytest.raises(AppendOnlyViolation):
        session.flush()
    session.rollback()
    with pytest.raises(Exception, match="append-only"):
        session.execute(text("DELETE FROM application_events"))
    session.rollback()


def test_nonce_is_single_use_and_must_match(session):
    app = make_app(session)
    nonce = repo.issue_nonce(app)
    assert repo.record_decision(session, app, decision="approve", via="telegram", nonce="wrong-nonce-123",
                                require_nonce_match=True) is None
    row = repo.record_decision(session, app, decision="approve", via="telegram", nonce=nonce,
                               telegram_user_id="42", require_nonce_match=True)
    assert row is not None and row.telegram_user_id == "42"
    # replay of the same callback
    assert repo.record_decision(session, app, decision="approve", via="telegram", nonce=nonce,
                                require_nonce_match=True) is None
    with pytest.raises(IntegrityError):  # DB-level uniqueness too
        session.add(Approval(application_id=app.id, nonce=nonce, decision="skip", decided_via="dashboard"))
        session.flush()
    session.rollback()


def test_reconcile_then_consume_once(session):
    app = make_app(session)
    # dashboard-style INSERT
    session.add(Approval(application_id=app.id, nonce="dash-nonce-0001", decision="approve", decided_via="dashboard"))
    session.flush()
    stats = repo.reconcile(session)
    assert stats["approved"] == 1 and app.status == "approved" and app.approved_via == "dashboard"
    tok = issue_token(session, app, daily_cap=5, dry_run=False)
    assert tok.valid_for(app.id)
    with pytest.raises(SubmissionRefused):  # approval already consumed
        issue_token(session, app, daily_cap=5, dry_run=False)
    app.submitted_at = repo.utcnow()
    repo.transition(session, app, AppStatus.SUBMITTED)
    session.commit()
    assert app.status == "submitted"


def test_issue_token_refusals(session):
    app = make_app(session)
    with pytest.raises(SubmissionRefused):
        issue_token(session, app, daily_cap=5, dry_run=False)  # not approved
    repo.record_approval(session, app, via="cli")
    with pytest.raises(SubmissionRefused):
        issue_token(session, app, daily_cap=5, dry_run=True)
    with pytest.raises(SubmissionRefused):
        issue_token(session, app, daily_cap=0, dry_run=False)


def test_edit_decision_triggers_refill_and_learning(session):
    app = make_app(session)
    app.filled_state = {"steps": [{"fields": [{"field_id": "question_1", "label": "Preferred pronouns?", "type": "text"},
                                              {"field_id": "q2", "label": "Favorite clinical domain", "type": "text"}]}]}
    session.add(Approval(application_id=app.id, nonce="dash-nonce-0002", decision="edit", decided_via="dashboard",
                         edits={"q2": "Oncology"}))
    session.flush()
    learned = []
    repo.reconcile(session, save_learned=lambda a, e: learned.append(e))
    assert app.status == "tailored" and app.human_edits == {"q2": "Oncology"} and learned


def test_outcome_events_reconciled(session):
    app = make_app(session)
    repo.report_outcome(session, app, "interview", actor="dashboard")
    assert repo.reconcile(session)["outcomes"] == 1
    assert app.outcome == "interview"
    assert repo.reconcile(session)["outcomes"] == 0  # idempotent


def test_vector_store_python_backend(session, embedder):
    for i, d in enumerate(["clinical trial software product", "restaurant line cook"]):
        repo.upsert_job(session, JobPosting(source="lever", external_job_id=str(i), title=d, apply_url="x"))
    session.flush()
    store = get_vector_store(session)
    assert isinstance(store, PythonVectorStore)
    jobs = session.query(Job).all()
    for j, v in zip(jobs, embedder.embed([j.title for j in jobs])):
        store.set_embedding(j, v)
    session.flush()
    q = embedder.embed(["clinical trial product"])[0]
    hits = store.search(q, k=2)
    assert hits[0].job_id == jobs[0].id and hits[0].similarity > hits[1].similarity
    # stored as JSON text and round-trips
    assert len(session.get(Job, jobs[0].id).embedding) == 384
