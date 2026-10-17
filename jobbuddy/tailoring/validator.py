"""Fabrication guard: every number and every tool/proper-noun claim must trace to profile.yaml.

Used on tailored resume bullets, summaries, cover letters and open-ended answers.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from jobbuddy.profile import Profile

WORD_NUMS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "fifty": 50, "hundred": 100,
}
UNIT = (r"(?:months?|years?|weeks?|days?|releases?|apps?|applications?|teams?|clients?|environments?|"
        r"products?|platforms?|agents?|pipelines?|samples?|million|thousand|hundred|x)")
NUM_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(%|percent|\+|[mMkK]\b\+?|million|thousand)?")
WORD_NUM_RE = re.compile(r"\b(" + "|".join(WORD_NUMS) + r")[\s-]+(" + UNIT + r")\b", re.I)

# Capitalized words that are fine anywhere (sentence starters, generic business words).
COMMON_CAPS = {
    "i", "i'm", "i've", "i'd", "i'll", "my", "the", "a", "an", "and", "as", "at", "by", "for", "from", "in", "into",
    "of", "on", "or", "to", "with", "while", "when", "what", "where", "which", "who", "why", "how", "this", "that",
    "these", "those", "it", "its", "our", "we", "you", "your", "he", "she", "they", "their", "dear", "sincerely",
    "thank", "thanks", "hello", "hi", "best", "regards", "warm", "most", "more", "after", "before", "alongside",
    "during", "over", "through", "today", "recently", "years", "led", "built", "delivered", "designed", "drove",
    "reduced", "improved", "decreased", "configured", "engineered", "authored", "spearheaded", "resolved",
    "personally", "defined", "managed", "created", "developed", "owned", "partnered", "shipped", "launched",
    "team", "hiring", "manager", "company", "role", "position", "mission", "product", "products", "data",
    "healthcare", "health", "technology", "clinical", "us", "u.s.", "usa", "ceo", "cto", "vp", "hr", "ok",
    "monday", "tuesday", "wednesday", "thursday", "friday", "january", "february", "march", "april", "may",
    "june", "july", "august", "september", "october", "november", "december", "yes", "no", "not", "also",
    "both", "each", "every", "here", "there", "then", "now", "so", "if", "but", "because", "although",
    "having", "being", "across", "including", "one", "two", "three", "first", "next", "finally", "ai",
}


def _num_value(raw: str, suffix: str | None) -> tuple[str, float]:
    v = float(raw.replace(",", ""))
    s = (suffix or "").lower().rstrip("+")
    if s in ("%", "percent"):
        return ("pct", v)
    if s in ("m", "million"):
        return ("num", v * 1_000_000)
    if s in ("k", "thousand"):
        return ("num", v * 1_000)
    return ("num", v)


def extract_numbers(text: str) -> set[tuple[str, float]]:
    out: set[tuple[str, float]] = set()
    for m in NUM_RE.finditer(text or ""):
        out.add(_num_value(m.group(1), m.group(2)))
    for m in WORD_NUM_RE.finditer(text or ""):
        n = WORD_NUMS[m.group(1).lower()]
        unit = m.group(2).lower()
        out.add(("num", float(n * 1_000_000 if unit == "million" else n * 1_000 if unit == "thousand" else n)))
    # "3 million" style: NUM followed by the word million is already handled via suffix.
    return out


TERM_RE = re.compile(r"\b([A-Z][A-Za-z0-9+#.&/-]*[A-Za-z0-9+#]|[A-Z]{2,}|[A-Za-z]+[0-9][A-Za-z0-9]*)\b")


def _sentence_starts(text: str) -> set[int]:
    starts = {0}
    for m in re.finditer(r"(?:[.!?:;]\s+|\n+|^\s*[-•♦●]\s*)", text):
        starts.add(m.end())
    return starts


def extract_terms(text: str) -> set[str]:
    """Proper nouns / tool names / acronyms, excluding sentence-initial common words."""
    starts = _sentence_starts(text or "")
    out: set[str] = set()
    for m in TERM_RE.finditer(text or ""):
        tok = m.group(1).strip(".-/&")
        low = tok.lower()
        if not tok or low in COMMON_CAPS:
            continue
        if m.start() in starts and tok[1:].islower():
            continue  # ordinary capitalized sentence starter
        if tok.isdigit():
            continue
        out.add(tok)
    return out


@dataclass
class ValidationIssue:
    kind: str      # unknown_number | unknown_term | bad_source | low_overlap | job_term_in_claim
    detail: str
    severity: str = "error"  # error | warning


@dataclass
class ValidationReport:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not any(i.severity == "error" for i in self.issues)

    def summary(self) -> list[str]:
        return [f"{i.severity}:{i.kind}:{i.detail}" for i in self.issues]


STOP = set("a an and the of to in for with on by as at from into across our their its is are was were be been "
           "that this which while over through including improving reducing".split())


def content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in STOP and len(w) > 2}


class ClaimValidator:
    def __init__(self, profile: Profile, extra_allowed_text: str = "") -> None:
        self.profile = profile
        corpus = profile.fact_corpus()
        self.corpus = corpus
        self.corpus_lower = (corpus + "\n" + extra_allowed_text).lower()
        self.allowed_numbers = extract_numbers(corpus) | extract_numbers(extra_allowed_text)
        for e in profile.experience:
            for r in e.roles:
                for y in re.findall(r"\d{4}", f"{r.start} {r.end}"):
                    self.allowed_numbers.add(("num", float(y)))
        if profile.years_experience:
            self.allowed_numbers.add(("num", float(profile.years_experience)))

    def _known(self, term: str, haystack: str | None = None) -> bool:
        """Case-insensitive presence, tolerant of plurals ("LLMs") and hyphen compounds ("AI-driven")."""
        hay = self.corpus_lower if haystack is None else haystack
        low = term.lower()
        if low in hay:
            return True
        if low.endswith("s") and low[:-1] in hay:
            return True
        parts = [x for x in re.split(r"[-/]", low) if x]
        if len(parts) > 1:
            return all(x in hay or x in COMMON_CAPS or x in {"driven", "based", "powered", "facing", "ready", "led"}
                       for x in parts)
        return False

    def _number_allowed(self, n: tuple[str, float], allowed: set[tuple[str, float]]) -> bool:
        # Percentages must match as percentages; "eight years" and "8 years" both normalize to 8.
        return n in allowed

    def validate_text(self, text: str, *, context_text: str = "", context_terms_ok_in_claims: set[str] | None = None,
                      ) -> ValidationReport:
        """context_text: e.g. the job description - its terms may be mentioned, but not claimed as Mike's."""
        rep = ValidationReport()
        allowed_nums = self.allowed_numbers | (extract_numbers(context_text) if context_text else set())
        for n in sorted(extract_numbers(text)):
            if not self._number_allowed(n, allowed_nums):
                label = f"{n[1]:g}%" if n[0] == "pct" else f"{n[1]:g}"
                rep.issues.append(ValidationIssue("unknown_number", f"'{label}' not found in profile"))
        ctx_lower = (context_text or "").lower()
        exempt: set[str] = set()
        for t in (context_terms_ok_in_claims or set()):
            if t:
                exempt.add(t.lower())
                exempt.update(w.lower() for w in re.findall(r"[A-Za-z0-9+#&.-]+", t))
        for sent in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
            first_person = re.search(r"\b(I|I've|I'm|I'd|my|me)\b", sent) is not None
            for term in extract_terms(sent):
                low = term.lower()
                if self._known(term) or low in exempt:
                    continue
                if ctx_lower and self._known(term, ctx_lower):
                    if first_person:
                        rep.issues.append(ValidationIssue(
                            "job_term_in_claim", f"'{term}' comes from the job posting; confirm Mike has it",
                            severity="warning"))
                    continue
                rep.issues.append(ValidationIssue("unknown_term", f"'{term}' not found in profile"))
        return rep

    def validate_bullet(self, source_text: str, new_text: str, min_overlap: float = 0.35) -> ValidationReport:
        rep = ValidationReport()
        src_nums = extract_numbers(source_text)
        for n in extract_numbers(new_text):
            if n not in src_nums:
                label = f"{n[1]:g}%" if n[0] == "pct" else f"{n[1]:g}"
                rep.issues.append(ValidationIssue("unknown_number", f"'{label}' not in source bullet"))
        for term in extract_terms(new_text):
            if not self._known(term):
                rep.issues.append(ValidationIssue("unknown_term", f"'{term}' not found in profile"))
        new_words = content_words(new_text)
        if new_words:
            overlap = len(new_words & content_words(source_text)) / len(new_words)
            if overlap < min_overlap:
                rep.issues.append(ValidationIssue("low_overlap", f"only {overlap:.0%} of words trace to the source"))
        return rep
