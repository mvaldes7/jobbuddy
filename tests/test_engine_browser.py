"""Playwright tests against local HTML fixtures (no network, nothing leaves the box)."""
from pathlib import Path

import pytest

from jobbuddy.engine.adapters.base import get_adapter
from jobbuddy.engine.detectors import detect_blockers
from jobbuddy.engine.field_mapper import MapperContext
from jobbuddy.engine.filler import ApprovalToken, FillContext, FormFiller
from jobbuddy.engine.form_inspector import inspect_page
from jobbuddy.engine.human import Human
from tests.conftest import fixture_url

pytestmark = pytest.mark.browser


def make_ctx(profile, app_config, tmp_path, adapter, overrides=None, app_id=1):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test resume")
    cl = tmp_path / "cover.pdf"
    cl.write_bytes(b"%PDF-1.4 test cover letter")
    mctx = MapperContext(profile=profile, adapter=get_adapter(adapter), job_title="Senior PSA", company="Acme Health",
                         overrides=overrides or {}, cover_letter_text=lambda: "Dear Hiring Team, ...",
                         answer_question=lambda q, n: __import__("jobbuddy.tailoring.questions", fromlist=["Answer"])
                         .Answer(text="Because I built clinical software myself.", confidence=0.7, source="llm",
                                 needs_review=True))
    return FillContext(mapper_ctx=mctx, engine_cfg=app_config.engine, screenshot_dir=tmp_path / "shots",
                       application_id=app_id, resume_path=resume, cover_letter_path=lambda: cl,
                       human=Human((0, 0), (0, 0)))


async def gh_frame(page):
    return next(f for f in page.frames if f.url.endswith("greenhouse_embed.html"))


async def test_inspector_greenhouse_iframe_shadow_and_labels(browser_context):
    page = await browser_context.new_page()
    await page.goto(fixture_url("greenhouse_page.html"))
    await page.wait_for_load_state("load")
    snap = await inspect_page(page)
    by = {f.field_id.split(":", 1)[-1]: f for f in snap.fields}
    assert by["first_name"].label == "First Name" and by["first_name"].label_source == "for" and by["first_name"].required
    assert by["first_name"].frame_index == 1  # inside the Greenhouse iframe
    assert by["last_name"].required  # aria-required + '*'
    assert by["email"].label == "Email" and by["email"].label_source == "aria-label"
    assert by["phone"].label == "Phone" and by["phone"].label_source == "placeholder"
    li = next(f for f in snap.fields if f.html_id == "question_1001")
    assert li.label == "LinkedIn Profile" and li.label_source == "nearest-text"
    auth = next(f for f in snap.fields if f.html_id == "question_1002")
    assert auth.label_source == "aria-labelledby" and auth.required and [o.label for o in auth.options][1:] == ["Yes", "No"]
    web = next(f for f in snap.fields if f.html_id == "website_input")
    assert web.in_shadow and web.label == "Website"
    assert by["resume"].type == "file" and by["resume"].accept == ".pdf,.doc,.docx"
    consent = next(f for f in snap.fields if f.html_id == "consent")
    assert consent.type == "checkbox" and "privacy notice" in consent.label
    assert any(b.kind == "submit" and b.text == "Submit Application" for b in snap.buttons)


async def test_greenhouse_fill_stops_before_submit(browser_context, profile, app_config, tmp_path):
    page = await browser_context.new_page()
    await page.goto(fixture_url("greenhouse_page.html"))
    res = await FormFiller(make_ctx(profile, app_config, tmp_path, "greenhouse")).run(page)
    frame = await gh_frame(page)
    assert res.status == "awaiting_approval"
    assert await frame.evaluate("window.__submitted") is False
    assert await frame.input_value("#first_name") == "Alex"
    assert await frame.input_value("#email") == "alex.example@example.com"
    assert await frame.evaluate("document.getElementById('resume').files.length") == 1
    assert await frame.evaluate("document.getElementById('cover_letter').files.length") == 1
    assert await frame.evaluate("document.getElementById('gender').selectedOptions[0].text") == "Decline To Self Identify"
    assert await frame.evaluate("document.getElementById('question_1005').selectedOptions[0].text") == "Company Website"
    assert await frame.input_value("#website_input") == ""  # profile.website is null -> left blank
    assert Path(res.screenshot_path).exists()
    low = {i["label"]: i for i in res.low_confidence}
    assert "Are you legally authorized to work in the United States?" in low  # TODO in profile -> human
    assert any("Why do you want to work" in k for k in low)  # LLM answer 0.7 < 0.75 threshold
    state = res.filled_state()
    assert state["steps"][0]["mappings"] and "png" not in str(state)


async def test_workday_multistep_wizard(browser_context, profile, app_config, tmp_path):
    page = await browser_context.new_page()
    await page.goto(fixture_url("workday_form.html"))
    res = await FormFiller(make_ctx(profile, app_config, tmp_path, "workday")).run(page)
    assert res.status == "awaiting_approval", res.error
    assert [s.action.split(":")[0] for s in res.steps] == ["next", "next", "next", "stopped_before_submit"]
    assert res.submit_button.text == "Submit"
    assert await page.evaluate("window.__submitted") is False
    assert await page.text_content("[data-automation-id=sourceDropdown]") == "Company Careers Page"
    assert await page.is_checked("[data-automation-id=previousWorker-no]")
    assert await page.text_content("[data-automation-id=gender]") == "I do not wish to answer"
    assert await page.evaluate("document.querySelector('[data-automation-id=veteranStatus]').selectedOptions[0].text") \
        == "I decline to self-identify"
    assert await page.evaluate("document.getElementById('wd-file').files.length") == 1
    first = {m.field_id: m for m in res.steps[0].mappings}
    assert first["legalNameSection_firstName"].value == "Alex" and first["legalNameSection_firstName"].source == "adapter"


async def test_workday_validation_error_pauses(browser_context, profile, app_config, tmp_path):
    page = await browser_context.new_page()
    await page.goto(fixture_url("workday_form.html"))
    res = await FormFiller(make_ctx(profile, app_config, tmp_path, "workday", overrides={"email": ""})).run(page)
    assert res.status == "paused" and "validation" in res.error and "wd-email" in res.error


@pytest.mark.parametrize("fixture,kind", [("captcha_form.html", "captcha"), ("workday_signin.html", "login")])
async def test_blockers_pause_and_never_solve(browser_context, profile, app_config, tmp_path, fixture, kind):
    page = await browser_context.new_page()
    await page.goto(fixture_url(fixture))
    res = await FormFiller(make_ctx(profile, app_config, tmp_path, "generic")).run(page)
    assert res.status == "paused" and any(b.kind == kind for b in res.blockers)
    assert res.steps == []  # nothing typed once a blocker is seen
    assert Path(res.screenshot_path).exists()


async def test_invisible_recaptcha_badge_is_not_a_blocker(browser_context):
    page = await browser_context.new_page()
    await page.goto(fixture_url("invisible_recaptcha_form.html"))
    assert await detect_blockers(page) == []


async def test_submit_requires_real_token_and_complete_fields(browser_context, profile, app_config, tmp_path):
    # forged token (not issued by the submitter) -> stops before submit
    page = await browser_context.new_page()
    await page.goto(fixture_url("greenhouse_page.html"))
    forged = ApprovalToken(application_id=1, approved_via="dashboard")
    res = await FormFiller(make_ctx(profile, app_config, tmp_path, "greenhouse")).run(page, approval=forged)
    assert res.status == "awaiting_approval" and await (await gh_frame(page)).evaluate("window.__submitted") is False

    # real token but a required TODO field still empty -> refuses
    page2 = await browser_context.new_page()
    await page2.goto(fixture_url("greenhouse_page.html"))
    token = ApprovalToken(application_id=1, approved_via="dashboard", _issued_by_submitter=True)
    res2 = await FormFiller(make_ctx(profile, app_config, tmp_path, "greenhouse")).run(page2, approval=token)
    assert res2.status == "paused" and "required fields" in res2.error
    assert await (await gh_frame(page2)).evaluate("window.__submitted") is False

    # real token + Mike's edits for the required fields -> submits the LOCAL fixture form
    page3 = await browser_context.new_page()
    await page3.goto(fixture_url("greenhouse_page.html"))
    edits = {"f1:job_application[answers_attributes][1][boolean_value]": "Yes"}  # field ids carry the frame
    res3 = await FormFiller(make_ctx(profile, app_config, tmp_path, "greenhouse", overrides=edits)).run(page3, approval=token)
    assert res3.status == "submitted", res3.error
    assert await (await gh_frame(page3)).evaluate("window.__submitted") is True
