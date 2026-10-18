"""Render tailored resume / cover letter to DOCX (python-docx) and PDF (docx2pdf, else LibreOffice)."""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

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


COVER_LETTER_TEMPLATE = Path(__file__).resolve().parents[2] / "profile" / "cover_letter_template.docx"
_CLOSING = re.compile(r"^(sincerely|best( regards)?|kind regards|regards|warm regards|thank you|respectfully),?$", re.I)


def split_letter(text: str) -> tuple[str, list[str], str, str]:
    """Plain-text letter -> (greeting, body paragraphs, closing line, signature)."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    greeting = "Dear Hiring Team,"
    if paras and paras[0].lower().startswith("dear"):
        greeting = " ".join(paras.pop(0).split())
    closing, signature = "Sincerely,", ""
    if paras:
        last = [ln.strip() for ln in paras[-1].split("\n") if ln.strip()]
        if last and _CLOSING.match(last[0]):
            paras.pop()
            closing = last[0]
            signature = " ".join(last[1:])
        elif len(paras) >= 2 and _CLOSING.match(paras[-2].strip()):
            closing, signature = paras[-2].strip(), " ".join(paras[-1].split())
            paras = paras[:-2]
    body = [" ".join(p.split()) for p in paras]
    return greeting, body, closing, signature


def _set_paragraph_text(par: Any, text: str) -> None:
    """Replace the paragraph's text, keeping the first run's formatting (font, size, color)."""
    runs = par.runs
    if not runs:
        par.add_run(text)
        return
    runs[0].text = text
    for r in runs[1:]:
        r._element.getparent().remove(r._element)


def render_cover_letter_from_template(template: str | Path | bytes, text: str, path: str | Path,
                                      signer: str | None = None) -> Path:
    """Fill Mike's own cover-letter .docx with a new letter: header block, fonts, colors, margins, spacing
    and paragraph layout all come from the template; only greeting, body paragraphs, closing and
    signature are replaced."""
    import copy
    import io

    path = Path(path)
    doc = Document(io.BytesIO(template) if isinstance(template, (bytes, bytearray)) else str(template))
    pars = doc.paragraphs
    gi = next((i for i, p in enumerate(pars) if p.text.strip().lower().startswith("dear")), None)
    ci = next((i for i, p in enumerate(pars) if gi is not None and i > gi and _CLOSING.match(p.text.strip())), None)
    if gi is None or ci is None:
        raise ValueError("cover letter template needs a 'Dear ...' greeting and a closing such as 'Sincerely,'")
    between = pars[gi + 1:ci]
    proto_body = next((p for p in between if p.text.strip()), None)
    proto_gap = next((p for p in between if not p.text.strip()), None)
    if proto_body is None:
        raise ValueError("cover letter template has no body paragraph")
    body_xml = copy.deepcopy(proto_body._element)
    gap_xml = copy.deepcopy(proto_gap._element) if proto_gap is not None else None
    greeting, body, closing, signature = split_letter(text)
    for p in between:
        p._element.getparent().remove(p._element)
    anchor = pars[gi]._element
    from docx.text.paragraph import Paragraph

    new_elems = []
    if gap_xml is not None:
        new_elems.append(copy.deepcopy(gap_xml))
    for k, para in enumerate(body):
        el = copy.deepcopy(body_xml)
        _set_paragraph_text(Paragraph(el, pars[gi]._parent), para)
        new_elems.append(el)
        if gap_xml is not None:
            new_elems.append(copy.deepcopy(gap_xml))
    for el in new_elems:
        anchor.addnext(el)
        anchor = el
    _set_paragraph_text(pars[gi], greeting)
    _set_paragraph_text(pars[ci], closing)
    after = doc.paragraphs
    ci_new = next(i for i, q in enumerate(after) if q._element is pars[ci]._element)
    sig = next((q for q in after[ci_new + 1:] if q.text.strip()), None)
    if sig is not None and (signature or signer):
        _set_paragraph_text(sig, signature or signer or "")
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(path)
    return path


def find_cover_letter_template(explicit: str | Path | bytes | None = None) -> str | Path | bytes | None:
    if explicit:
        return explicit
    return COVER_LETTER_TEMPLATE if COVER_LETTER_TEMPLATE.exists() else None


def render_cover_letter_pdf(profile: Profile, text: str, docx_path: str | Path,
                            template: str | Path | bytes | None = None) -> Path | None:
    """Cover letters are always uploaded as PDF: render the DOCX (from Mike's template when available),
    convert it, and return the PDF, or None if no converter works (callers never fall back to the DOCX)."""
    docx = render_cover_letter_docx(profile, text, docx_path, template=template)
    pdf = docx_to_pdf(docx)
    if pdf is None:
        log.warning("cover letter PDF conversion failed; not uploading a DOCX")
    return pdf


def render_cover_letter_docx(profile: Profile, text: str, path: str | Path,
                             template: str | Path | bytes | None = None) -> Path:
    tpl = find_cover_letter_template(template)
    if tpl is not None:
        return render_cover_letter_from_template(tpl, text, path, signer=profile.personal.full_name)
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
