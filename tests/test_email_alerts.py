import email


from jobbuddy.fetchers.base import JobPosting
from jobbuddy.fetchers.email_alerts import parse_alert_message, parse_alert_text, resolve_against
from tests.conftest import FIXTURES


def msg(name):
    return email.message_from_bytes((FIXTURES / name).read_bytes())


def test_linkedin_alert_parsing_never_keeps_linkedin_apply_urls():
    leads = parse_alert_message(msg("linkedin_alert.eml"))
    assert [l.origin_job_id for l in leads] == ["3999000111", "3999000222"]
    first = leads[0]
    assert first.title == "Senior Product Solution Architect"
    assert first.company == "Acme Health" and first.location.startswith("San Francisco")
    p = first.to_posting()
    assert p.source == "linkedin_alert" and p.apply_url is None and p.url is None
    assert p.lead_meta["needs_resolution"] is True


def test_indeed_alert_parsing():
    leads = parse_alert_message(msg("indeed_alert.eml"))
    assert [(l.origin_job_id, l.title, l.company) for l in leads] == [
        ("1a2b3c4d5e6f7a8b", "Product Manager (AI/LLM)", "Example Bio"),
        ("9f8e7d6c5b4a3f2e", "Solutions Architect", "Example AI"),
    ]
    assert leads[0].location == "Remote"


def test_plain_text_alert():
    text = ("Senior Product Solution Architect\nAcme Health\nSan Francisco, CA\n"
            "View job: https://www.linkedin.com/comm/jobs/view/123456/\n")
    (lead,) = parse_alert_text(text, "linkedin")
    assert lead.company == "Acme Health" and lead.origin_job_id == "123456"


def test_non_alert_sender_ignored():
    m = email.message_from_string("From: friend@example.com\nSubject: hi\n\nhello")
    assert parse_alert_message(m) == []


def test_resolve_lead_to_ats_job():
    lead = parse_alert_message(msg("linkedin_alert.eml"))[0]
    candidates = [
        JobPosting(source="greenhouse", external_job_id="1", title="Senior Product Solution Architect",
                   company_name="Acme Health, Inc.", apply_url="https://boards.greenhouse.io/acme/jobs/1"),
        JobPosting(source="greenhouse", external_job_id="2", title="Staff Accountant", company_name="Acme Health",
                   apply_url="https://boards.greenhouse.io/acme/jobs/2"),
        JobPosting(source="lever", external_job_id="3", title="Senior Product Solution Architect",
                   company_name="Other Co", apply_url="https://jobs.lever.co/other/3"),
    ]
    res = resolve_against(lead, candidates)
    assert res is not None and res.external_job_id == "1" and res.source == "greenhouse"
    assert resolve_against(parse_alert_message(msg("linkedin_alert.eml"))[1], candidates) is None




# ---------------- forwarded + JSON input ----------------
def test_forwarded_linkedin_alert_with_quoted_body():
    from jobbuddy.fetchers.email_alerts import is_forwarded

    m = msg("linkedin_alert_forwarded.eml")
    assert is_forwarded(m)
    (lead,) = parse_alert_message(m)
    assert (lead.origin, lead.origin_job_id, lead.title, lead.company) == \
        ("linkedin", "3999000111", "Senior Product Solution Architect", "Acme Health")
    assert lead.received_at.day == 15  # the original alert's date, not the forward's


def test_json_input_forwarded_html():
    from jobbuddy.fetchers.email_alerts import message_from_json, message_key

    html = ("<div>---------- Forwarded message ---------<br>From: Indeed &lt;alert@indeed.com&gt;<br></div>"
            "<div class='gmail_quote'><a href='https://www.indeed.com/rc/clk?jk=1a2b3c4d5e6f7a8b&from=ja'>"
            "Product Manager (AI/LLM)</a><p>Example Bio - Remote</p></div>")
    m = message_from_json({"from": "Alex <alex@example.com>", "subject": "Fwd: 2 new jobs", "html": html})
    (lead,) = parse_alert_message(m)
    assert lead.origin == "indeed" and lead.company == "Example Bio" and lead.location == "Remote"
    assert message_key(m).startswith("no-id:") and message_key(m) == message_key(message_from_json(
        {"from": "Alex <alex@example.com>", "subject": "Fwd: 2 new jobs", "html": html}))


# ---------------- Wellfound ----------------
def test_wellfound_digest_parsing():
    leads = parse_alert_message(msg("wellfound_alert.eml"))
    assert [l.origin_job_id for l in leads] == ["2901001", "2902002", "2903003"]
    a, b, c = leads
    assert (a.title, a.company, a.location) == ("Technical Product Manager", "Acme Health", "San Francisco; Remote (US)")
    assert (a.extra["salary_min"], a.extra["salary_max"]) == (150000, 190000)
    assert (a.extra["equity_min_pct"], a.extra["equity_max_pct"]) == (0.1, 0.5)
    assert (b.title, b.company, b.location) == ("Product Manager (AI/LLM)", "TinyClinic", "Remote")  # "X is hiring a Y"
    assert (c.company, c.location, c.extra["company_slug"]) == ("OldCo", "New York, NY", "oldco")  # legacy angel.co
    p = a.to_posting()
    assert p.source == "wellfound" and p.apply_url is None and p.url == "https://wellfound.com/jobs/2901001"
    assert p.remote is True and p.salary_min == 150000 and p.lead_meta["equity_max_pct"] == 0.5


def test_wellfound_forwarded_plain_text():
    m = email.message_from_string(
        "From: Alex <alex@example.com>\nSubject: Fwd: New job: Technical Product Manager\n\n"
        "---------- Forwarded message ---------\nFrom: Wellfound <jobs@angel.co>\n\n"
        "> Technical Product Manager\n> Acme Health · Remote\n> $150k - $190k\n"
        "> https://wellfound.com/company/acme-health/jobs/2901001-technical-product-manager\n")
    (lead,) = parse_alert_message(m)
    assert lead.origin == "wellfound" and lead.company == "Acme Health" and lead.extra["salary_max"] == 190000


async def test_ingest_wellfound_resolves_via_public_ats_or_marks_manual_apply(session, agent, settings, profile,
                                                                               embedder):
    import httpx
    import respx

    from jobbuddy.db.models import Job
    from jobbuddy.fetchers.base import PoliteHttp
    from jobbuddy.notifiers.base import NullNotifier
    from jobbuddy.pipeline import build_runtime, stage_filter_embed, stage_manual_digest

    board = (FIXTURES / "ashby_board.json").read_text()
    with respx.mock(assert_all_called=False) as mock:
        ashby = mock.get(url__regex=r"https://api\.ashbyhq\.com/posting-api/job-board/oldco.*").mock(
            return_value=httpx.Response(200, text=board, headers={"content-type": "application/json"}))
        mock.route(host__regex=r"(api\.ashbyhq\.com|boards-api\.greenhouse\.io|api\.lever\.co)").mock(
            return_value=httpx.Response(404))
        blocked = mock.route(host__regex=r"(.*\.)?(wellfound\.com|angel\.co|linkedin\.com|indeed\.com)").mock(
            return_value=httpx.Response(599))
        async with PoliteHttp("jobbuddy-test", delay=0, retries=0) as http:
            out = await agent.ingest_alert(msg("wellfound_alert.eml"), http=http, probe=True)
            again = await agent.ingest_alert(msg("wellfound_alert.eml"), http=http, probe=True)
    assert ashby.called and not blocked.called  # never touches Wellfound (or LinkedIn/Indeed)
    assert out["resolved"] == 1 and out["manual_apply"] == 2 and again["duplicate"] is True
    by = {l["title"]: l for l in out["leads"]}
    assert by["Solutions Architect (HealthTech)"]["resolved_to"]["source"] == "ashby"
    assert by["Technical Product Manager"]["status"] == "manual_apply"

    session.expire_all()
    wf = {j.external_job_id: j for j in session.query(Job).filter_by(source="wellfound")}
    assert wf["2903003"].status == "closed" and wf["2903003"].lead_meta["resolved"] is True
    assert wf["2901001"].status == "manual_apply" and wf["2901001"].url == "https://wellfound.com/jobs/2901001"
    assert wf["2901001"].apply_url is None
    ats = session.query(Job).filter_by(source="ashby").one()
    assert ats.status == "found" and ats.apply_url.endswith("/application")

    rt = build_runtime(settings, embedder=embedder, notifier=NullNotifier(), profile=profile)
    rt.cfg.matching.max_posting_age_days = 100000
    rt.cfg.matching.similarity_threshold = 0.0
    fe = stage_filter_embed(session, rt)  # the ATS row enters the normal queue; Wellfound rows never do
    assert fe["queued_for_agent"] == 1 and session.query(Job).filter_by(source="ashby").one().status == "filtered"
    d = stage_manual_digest(session, rt)
    assert d["digested"] == 2
    (text,) = rt.notifier.sent
    assert "https://wellfound.com/jobs/2901001" in text and "https://wellfound.com/jobs/2902002" in text
    assert "$150k-$190k" in text and "0.1%-0.5% equity" in text and "Acme Health" in text
    assert stage_manual_digest(session, rt)["digested"] == 0  # each lead is sent once
