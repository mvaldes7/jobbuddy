import json
import shutil

import pytest
from docx import Document

from jobbuddy.config import TailoringConfig
from jobbuddy.fetchers.base import JobPosting
from jobbuddy.tailoring.cover_letter import generate_cover_letter, template_cover_letter
from jobbuddy.tailoring.questions import answer_question
from jobbuddy.tailoring.render import docx_to_pdf, render_cover_letter_docx, render_resume_docx
from jobbuddy.tailoring.resume import tailor_resume
from jobbuddy.tailoring.style import extract_cover_letter_sample
from jobbuddy.tailoring.validator import ClaimValidator, extract_numbers

JOB = JobPosting(source="greenhouse", external_job_id="1", title="Senior Product Solution Architect",
                 company_name="Acme Health",
                 description="Lead AI agent adoption and LLM development for clinical trial software. FHIR a plus.")


def test_number_extraction_normalizes_words_and_units():
    nums = extract_numbers("cut cycle time 66%, 7 releases, over 3 million samples, built in three months, 3M+ rows")
    assert ("pct", 66.0) in nums and ("num", 7.0) in nums and ("num", 3_000_000.0) in nums
    assert ("num", 3.0) in nums


def test_validator_catches_fabricated_numbers_and_tools(profile):
    v = ClaimValidator(profile)
    ok = v.validate_bullet("Decreased bug resolution cycle time by 66% and improved overall product stability.",
                           "Cut bug resolution cycle time by 66%, improving product stability.")
    assert ok.ok, ok.summary()
    bad_num = v.validate_bullet("Decreased bug resolution cycle time by 66% and improved overall product stability.",
                                "Decreased bug resolution cycle time by 80% and improved overall product stability.")
    assert not bad_num.ok and any("80%" in s for s in bad_num.summary())
    bad_tool = v.validate_bullet("Improved data loading performance by 85% through KNIME analytics.",
                                 "Improved data loading performance by 85% through KNIME and Snowflake analytics.")
    assert not bad_tool.ok and any("Snowflake" in s for s in bad_tool.summary())


def test_tailor_resume_reverts_fabrications(profile, fake_llm_factory):
    reply = {
        "summary": "Product Solution Architect with 12 years in HealthTech.",  # 12 is fabricated
        "skills_order": ["AI Agent Development", "Kubernetes", "KNIME"],      # Kubernetes is fabricated
        "bullets": [
            {"source_id": "ex-ai-agents", "text": "Spearheaded adoption of AI agents across DevOps and product "
                                                  "workflows, including agents for bug triage, automated "
                                                  "documentation, and test data generation."},
            {"source_id": "ex-bug-cycle", "text": "Decreased bug resolution cycle time by 90%."},
            {"source_id": "made-up", "text": "Won an award."},
        ],
    }
    llm = fake_llm_factory({"tailor_resume": json.dumps(reply)})
    t = tailor_resume(llm, profile, JOB, TailoringConfig())
    texts = {b.source_id: b for r in t.roles for b in r.bullets}
    assert texts["ex-ai-agents"].reworded is True
    assert "66%" in texts["ex-bug-cycle"].text and texts["ex-bug-cycle"].rejected_reasons
    assert "made-up" not in texts
    assert "12 years" not in t.summary
    assert "Kubernetes" not in t.skills and t.skills[0] == "AI Agent Development"
    assert t.roles[0].bullets[0].source_id == "ex-ai-agents"
    assert any("reverted" in n for n in t.validation_notes)


def test_tailor_without_llm_keeps_text_verbatim(profile):
    t = tailor_resume(None, profile, JOB, TailoringConfig())
    originals = {b.id: " ".join(b.text.split()) for _, _, b in profile.iter_bullets()}
    for r in t.roles:
        for b in r.bullets:
            assert b.text == originals[b.source_id]


def test_render_resume_docx(tmp_path, profile):
    t = tailor_resume(None, profile, JOB, TailoringConfig())
    path = render_resume_docx(profile, t, tmp_path / "r.docx")
    text = "\n".join(p.text for p in Document(path).paragraphs)
    assert profile.personal.full_name in text and "66%" in text and "TODO" not in text


@pytest.mark.slow
def test_pdf_conversion_with_libreoffice(tmp_path, profile):
    if not (shutil.which("soffice") or shutil.which("libreoffice")):
        pytest.skip("LibreOffice not installed")
    t = tailor_resume(None, profile, JOB, TailoringConfig())
    pdf = docx_to_pdf(render_resume_docx(profile, t, tmp_path / "r.docx"))
    assert pdf is not None and pdf.read_bytes()[:4] == b"%PDF"


def test_cover_letter_template_uses_hook_and_real_metrics(profile):
    text = template_cover_letter(profile, JOB)
    assert "Senior Product Solution Architect role at Acme Health" in text
    assert profile.voice.personal_hook.split()[0] in text
    assert "Sincerely,\nAlex Q. Example" in text
    rep = ClaimValidator(profile).validate_text(text, context_text=f"{JOB.title} {JOB.company_name} {JOB.description}",
                                                context_terms_ok_in_claims={"Acme Health"})
    assert rep.ok, rep.summary()


def test_cover_letter_llm_with_style_sample_and_validation(profile, fake_llm_factory):
    good = ("Dear Hiring Team,\n\nI am writing about the Senior Product Solution Architect role at Acme Health. "
            "A hard experience years ago showed me that healthcare technology either eases someone's worst moment "
            "or adds to it.\n\nWhen DevOps lacked bandwidth, I built it myself: a validated clinical trial site mobile "
            "application in three months. I also decreased bug resolution cycle time by 66%.\n\n"
            "I'd welcome the opportunity to talk further.\n\nSincerely,\nAlex Q. Example")
    bad = good.replace("66%", "75%")
    replies = iter([bad, good])
    llm = fake_llm_factory({"cover_letter": lambda m: next(replies)})
    cl = generate_cover_letter(llm, profile, JOB, style_sample="SAMPLE VOICE TEXT")
    assert cl.used_llm and "66%" in cl.text and "75%" not in cl.text
    first_prompt = llm.calls[0]["messages"][1]["content"]
    assert "SAMPLE VOICE TEXT" in first_prompt and "PERSONAL_HOOK" in first_prompt
    assert "75%" in llm.calls[1]["messages"][-1]["content"]  # correction request names the bad claim


def test_cover_letter_docx(tmp_path, profile):
    p = render_cover_letter_docx(profile, "Dear Team,\n\nHello.\n\nSincerely,\nAlex", tmp_path / "cl.docx")
    assert "Hello." in [x.text for x in Document(p).paragraphs]


def test_extract_style_from_docx(tmp_path):
    from docx import Document as D

    d = D()
    d.add_paragraph("Dear Someone,")
    d.add_paragraph("")
    d.add_paragraph("I built it myself.")
    src = tmp_path / "s.docx"
    d.save(src)
    out = tmp_path / "s.txt"
    assert extract_cover_letter_sample(src, out) == "Dear Someone,\n\nI built it myself."


def test_questions_sensitive_never_llm(profile, fake_llm_factory):
    llm = fake_llm_factory({})  # any call would raise
    a = answer_question(llm, profile, JOB, "Will you now or in the future require visa sponsorship?")
    assert a.text is None and a.needs_review and a.source == "human_required"
    a2 = answer_question(llm, profile, JOB, "What are your salary expectations?")
    assert a2.text is None


def test_questions_open_ended_llm(profile, fake_llm_factory):
    llm = fake_llm_factory({"question": '{"answer": "I personally built a validated clinical trial site mobile '
                                        'application in three months, and I want to bring that ownership to Acme '
                                        'Health.", "confidence": 0.95}'})
    a = answer_question(llm, profile, JOB, "Why do you want to work at Acme Health?", max_chars=500)
    assert a.source == "llm" and a.confidence <= 0.8 and "three months" in a.text


def test_cover_letter_from_template_keeps_layout(tmp_path):
    from docx import Document
    from docx.shared import Pt, RGBColor

    from jobbuddy.tailoring.render import render_cover_letter_from_template

    tpl = Document()
    tpl.add_table(rows=1, cols=2).cell(0, 0).text = "Header Name"
    for t in ["", "Dear Old Manager,", "", "Old body one.", "", "Old body two.", "", "Sincerely,", "Old Sig"]:
        r = tpl.add_paragraph().add_run(t)
        r.font.size = Pt(10)
        r.font.name = "Helvetica Neue"
        r.font.color.rgb = RGBColor(0x11, 0x22, 0x33)
    src = tmp_path / "tpl.docx"
    tpl.save(src)
    out = render_cover_letter_from_template(
        src, "Dear Hiring Team,\n\nNew one.\n\nNew two.\n\nNew three.\n\nSincerely,\nMichael A. Valdés", tmp_path / "o.docx")
    d = Document(out)
    texts = [p.text for p in d.paragraphs]
    assert texts == ["", "Dear Hiring Team,", "", "New one.", "", "New two.", "", "New three.", "",
                     "Sincerely,", "Michael A. Valdés"]
    assert d.tables[0].cell(0, 0).text == "Header Name"
    body = d.paragraphs[5].runs[0]
    assert body.font.name == "Helvetica Neue" and body.font.size == Pt(10)
    assert body.font.color.rgb == RGBColor(0x11, 0x22, 0x33)
