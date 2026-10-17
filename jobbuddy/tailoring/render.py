"""Render tailored resume / cover letter to DOCX (python-docx) and PDF (docx2pdf, else LibreOffice)."""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

from jobbuddy.profile import Profile
from jobbuddy.tailoring.resume import TailoredResume

log = logging.getLogger(__name__)


def _contact_line(profile: Profile) -> str:
    p = profile.personal
    parts = [p.email, p.phone, p.linkedin, p.location.city]
    return " • ".join(x for x in parts if x and not str(x).upper().startswith("TODO"))


def _base_doc() -> Document:
    doc = Document()
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(10.5)
    for s in doc.sections:
        s.left_margin = s.right_margin = Pt(54)
        s.top_margin = s.bottom_margin = Pt(42)
    return doc


def _heading(doc: Document, text: str) -> None:
    p = doc.add_paragraph()
    r = p.add_run(text.upper())
    r.bold = True
    r.font.size = Pt(11)
    p.paragraph_format.space_before = Pt(8)
    p.paragraph_format.space_after = Pt(2)


def render_resume_docx(profile: Profile, tailored: TailoredResume, path: str | Path) -> Path:
    path = Path(path)
    doc = _base_doc()
    name = doc.add_paragraph()
    name.alignment = WD_ALIGN_PARAGRAPH.CENTER
    nr = name.add_run(profile.personal.full_name)
    nr.bold = True
    nr.font.size = Pt(18)
    c = doc.add_paragraph(_contact_line(profile))
    c.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if profile.headline:
        h = doc.add_paragraph()
        h.alignment = WD_ALIGN_PARAGRAPH.CENTER
        h.add_run(profile.headline).bold = True

    _heading(doc, "Summary")
    doc.add_paragraph(tailored.summary)

    _heading(doc, "Areas of Expertise")
    doc.add_paragraph(" • ".join(tailored.skills))

    _heading(doc, "Professional Experience")
    last_company = None
    for role in tailored.roles:
        if role.company != last_company:
            cp = doc.add_paragraph()
            cp.add_run(role.company).bold = True
            if role.location:
                cp.add_run(f" – {role.location}")
            last_company = role.company
        rp = doc.add_paragraph()
        title = role.title + (f" ({role.official_title})" if role.official_title else "")
        rp.add_run(title).italic = True
        rp.add_run(f"    {role.start or ''} – {role.end or ''}")
        for held in role.also_held:
            hp = doc.add_paragraph()
            hp.add_run(str(held.get("title", ""))).italic = True
            hp.add_run(f"    {held.get('start', '')} – {held.get('end', '')}")
        for b in role.bullets:
            doc.add_paragraph(b.text, style="List Bullet")

    _heading(doc, "Education")
    for ed in profile.education:
        line = ed.degree
        if ed.concentration:
            line += f", {ed.concentration}"
        if ed.minor:
            line += f" / Minor: {ed.minor}"
        line += f" | {ed.school}"
        if ed.location:
            line += f" – {ed.location}"
        if ed.year:
            line += f", {ed.year}"
        doc.add_paragraph(line)
        if ed.notes:
            doc.add_paragraph(" ".join(ed.notes.split()))

    if profile.certifications:
        _heading(doc, "Certifications & Courses")
        for cert in profile.certifications:
            doc.add_paragraph(f"{cert.name}, {cert.issuer} ({cert.year})")

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return path


def render_cover_letter_docx(profile: Profile, text: str, path: str | Path) -> Path:
    path = Path(path)
    doc = _base_doc()
    hp = doc.add_paragraph()
    hr = hp.add_run(profile.personal.full_name)
    hr.bold = True
    hr.font.size = Pt(14)
    doc.add_paragraph(_contact_line(profile))
    for para in [p.strip() for p in text.split("\n\n") if p.strip()]:
        doc.add_paragraph(" ".join(para.split("\n")))
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return path


def docx_to_pdf(docx_path: str | Path, out_dir: str | Path | None = None, timeout: int = 120) -> Path | None:
    """docx2pdf (Windows/macOS with Word) first, then LibreOffice headless. None if neither works."""
    docx_path = Path(docx_path)
    out_dir = Path(out_dir or docx_path.parent)
    target = out_dir / (docx_path.stem + ".pdf")
    try:
        from docx2pdf import convert  # type: ignore

        convert(str(docx_path), str(target))
        if target.exists():
            return target
    except Exception:
        pass
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        log.warning("no PDF converter available; DOCX only")
        return None
    try:
        subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(out_dir), str(docx_path)],
                       check=True, capture_output=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("LibreOffice conversion failed: %s", type(exc).__name__)
        return None
    return target if target.exists() else None
