"""Extract Mike's cover-letter voice sample from a .docx into plain text."""
from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

from docx import Document


def docx_bytes_to_text(fp: BinaryIO | str | Path) -> str:
    doc = Document(fp)
    paras = [p.text.strip() for p in doc.paragraphs]
    # Collapse runs of blank lines, keep paragraph breaks. Header tables (name/contact) are skipped.
    out: list[str] = []
    for p in paras:
        if p:
            out.append(" ".join(p.split()))
    return "\n\n".join(out).strip()


def extract_cover_letter_sample(docx_path: str | Path, out_path: str | Path) -> str:
    text = docx_bytes_to_text(docx_path)
    Path(out_path).write_text(text + "\n", encoding="utf-8")
    return text
