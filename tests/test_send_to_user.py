"""New flow: tailored + score >= 70 -> Telegram sendDocument (listing + cover letter PDF) -> 'sent_to_user'."""
import httpx
import respx

from jobbuddy.config import AppConfig
from jobbuddy.db import repository as repo
from jobbuddy.db.models import Application, ApplicationEvent, JobScore
from jobbuddy.fetchers.base import JobPosting
from jobbuddy.notifiers.telegram import TelegramNotifier
from jobbuddy import pipeline

TOKEN = "123456:TEST_TOKEN_NOT_REAL_abcdefghijklmnop"


class _RT:
    def __init__(self, notifier, tmp_path):
        self.cfg = AppConfig()
        self.notifier = notifier
        self.workdir = tmp_path


def _app(session, ext, score, status="tailored", pdf=True):
    job, _ = repo.upsert_job(session, JobPosting(source="greenhouse", external_job_id=ext, title=f"PM {ext}",
                                                 company_name="Acme Health", url=f"https://example.com/jobs/{ext}",
                                                 apply_url=f"https://example.com/apply/{ext}"))
    session.add(JobScore(job_id=job.id, filter_passed=True, llm_score=score,
                         llm_reasons={"reasons": ["Healthcare AI PM role that fits"], "gaps": ["x"]}))
    app = Application(job_id=job.id, status=status, ats="greenhouse", cover_letter_text="Dear team")
    session.add(app)
    session.flush()
    if pdf:
        repo.store_document(session, kind="cover_letter", filename="Cover_Letter.pdf", content=b"%PDF-1.4 x" + ext.encode(),
                            application_id=app.id)
    return app


def test_sends_document_and_marks_sent_to_user(session, tmp_path):
    hi = _app(session, "1", 75)
    lo = _app(session, "2", 65)
    paused = _app(session, "3", 80, status="paused")
    sent = []

    def handler(request: httpx.Request):
        sent.append((request.url.path.rsplit("/", 1)[-1], request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    with respx.mock() as mock:
        mock.route(host="api.telegram.org").mock(side_effect=handler)
        stats = pipeline.stage_send_to_user(session, _RT(TelegramNotifier(TOKEN, "777"), tmp_path))
    assert stats["sent"] == 1 and stats["sent_ids"] == [hi.id] and stats["below_threshold"] == 1
    assert [m for m, _ in sent] == ["sendDocument"]
    body = sent[0][1]
    assert b"Acme Health" in body and b"Score: 75" in body and b"https://example.com/jobs/1" in body
    assert b"Healthcare AI PM role" in body and b"Cover_Letter.pdf" in body and b"reply_markup" not in body
    assert hi.status == "sent_to_user" and lo.status == "tailored" and paused.status == "paused"
    ev = session.query(ApplicationEvent).filter_by(application_id=hi.id, to_status="sent_to_user").one()
    assert ev.created_at is not None and ev.payload["telegram_message_id"] == 42


def test_backfill_statuses_and_missing_pdf(session, tmp_path, monkeypatch):
    paused = _app(session, "4", 74, status="paused")
    nopdf = _app(session, "5", 90, pdf=False)
    monkeypatch.setattr(pipeline, "cover_letter_pdf_for",
                        lambda s, rt, app: None if app.id == nopdf.id else ("c.pdf", b"%PDF"))
    from jobbuddy.notifiers.base import NullNotifier
    n = NullNotifier()
    stats = pipeline.stage_send_to_user(session, _RT(n, tmp_path), statuses=("tailored", "paused"))
    assert stats["sent_ids"] == [paused.id] and stats["no_cover_letter"] == 1
    assert paused.status == "sent_to_user" and nopdf.status == "tailored"


def test_submit_is_retired():
    assert pipeline.SUBMISSION_RETIRED is True
