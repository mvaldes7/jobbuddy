"""Job leads from LinkedIn / Indeed job-ALERT EMAILS.

There is no IMAP client here. The on-box agent reads Mike's JobAlerts mail itself and hands each message
to `jobbuddy ingest-alert-email FILE.eml` (raw RFC 822) or `--json` ({from, subject, html, text, date,
message_id}). Forwarded alerts are supported: the subject has a "Fwd:" prefix, the original sender is in a
"---------- Forwarded message ----------" header block, quoted ("> ") bodies, or an attached message/rfc822.

Why emails instead of the sites: LinkedIn's and Indeed's terms prohibit scraping and automated
use (including bots that apply), so jobbuddy never requests linkedin.com or indeed.com pages and
never uses Easy Apply. Alert emails Mike already receives are parsed locally for
title/company/location, then resolved to the employer's own ATS posting (Greenhouse/Lever/Ashby
public APIs or jobs already fetched from companies.yaml). Unresolved leads are kept for manual review.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from email.message import Message
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Iterable, Sequence

from jobbuddy.fetchers.base import JobPosting

log = logging.getLogger(__name__)

LINKEDIN_SENDERS = ("jobalerts-noreply@linkedin.com", "jobs-noreply@linkedin.com", "jobs-listings@linkedin.com")
INDEED_SENDERS = ("alert@indeed.com", "alerts@indeed.com", "noreply@indeed.com", "jobalerts@indeed.com")

WELLFOUND_SENDER_DOMAINS = ("wellfound.com", "angel.co", "angellist.com")
# ALERT_SOURCES rows are leads parsed from e-mails; they are never fetched, filtered as ATS jobs, or applied to
# automatically. Only the employer-ATS row they resolve to goes through the CI pipeline.
ALERT_SOURCES = ("linkedin_alert", "indeed_alert", "wellfound")

# wellfound.com/jobs/1234567-senior-pm, wellfound.com/company/acme/jobs/1234567-senior-pm, angel.co/... (legacy)
WF_JOB_RE = re.compile(r"(?:wellfound\.com|angel\.co)/(?:company/([a-z0-9][a-z0-9-]*)/)?jobs/(\d{3,})(?:-[a-z0-9-]*)?",
                       re.I)
WF_COMPANY_RE = re.compile(r"(?:wellfound\.com|angel\.co)/company/([a-z0-9][a-z0-9-]*)/?(?:[?#\"']|$)", re.I)
EQUITY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(?:[-–—to]+\s*(\d+(?:\.\d+)?)\s*%)?", re.I)
SALARY_HINT_RE = re.compile(r"\$\s?\d|\d\s?[kK]\b|\bequity\b|\d\s?%", re.I)
LI_JOB_RE = re.compile(r"linkedin\.com/(?:comm/)?jobs/view/(\d+)", re.I)
INDEED_JK_RE = re.compile(r"indeed\.com/[^\"'\s]*?[?&](?:jk|vjk)=([0-9a-f]{8,})", re.I)
SKIP_ANCHOR_TEXT = {"view job", "apply", "apply now", "see all jobs", "view all", "unsubscribe", "easy apply",
                    "see more jobs", "view similar jobs", ""}


@dataclass
class _Item:
    kind: str  # "a" | "text"
    text: str
    href: str | None = None


class _Linearizer(HTMLParser):
    """Flattens HTML into a sequence of anchors and text chunks in document order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[_Item] = []
        self._href: str | None = None
        self._buf: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("style", "script", "head"):
            self._skip += 1
        if tag == "a":
            self._flush_text()
            self._href = dict(attrs).get("href") or ""
        elif tag in ("br", "p", "div", "td", "tr", "li", "table", "span"):
            if self._href is None:
                self._flush_text()

    def handle_endtag(self, tag: str) -> None:
        if tag in ("style", "script", "head"):
            self._skip = max(0, self._skip - 1)
        if tag == "a" and self._href is not None:
            self.items.append(_Item("a", _clean(" ".join(self._buf)), self._href))
            self._buf, self._href = [], None
        elif tag in ("p", "div", "td", "tr", "li", "span") and self._href is None:
            self._flush_text()

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._buf.append(data)

    def _flush_text(self) -> None:
        t = _clean(" ".join(self._buf))
        self._buf = []
        if t:
            self.items.append(_Item("text", t))


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


@dataclass
class AlertLead:
    origin: str           # linkedin | indeed
    origin_job_id: str
    title: str
    company: str | None = None
    location: str | None = None
    origin_url: str | None = None
    received_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_posting(self) -> JobPosting:
        loc = self.location
        if self.origin == "wellfound":
            return self._wellfound_posting()
        return JobPosting(
            source=f"{self.origin}_alert",
            external_job_id=self.origin_job_id,
            title=self.title,
            company_name=self.company,
            location=loc,
            remote=True if loc and re.search(r"\bremote\b", loc, re.I) else None,
            url=None,           # we never fetch or store-and-follow LinkedIn/Indeed pages
            apply_url=None,     # set only after resolving to the employer's ATS
            description=None,
            posted_at=self.received_at,
            lead_meta={"origin": self.origin, "origin_job_id": self.origin_job_id,
                       "needs_resolution": True, "resolved": False},
        )


    def _wellfound_posting(self) -> JobPosting:
        """source='wellfound'. The Wellfound URL is stored as a link for Mike (manual apply) but is never
        requested by jobbuddy, and apply_url stays empty: there is no automated apply on Wellfound."""
        loc = self.location
        x = self.extra
        return JobPosting(
            source="wellfound",
            external_job_id=self.origin_job_id,
            title=self.title,
            company_name=self.company,
            location=loc,
            remote=True if (loc and re.search(r"\bremote\b", loc, re.I)) or x.get("remote") else None,
            url=self.origin_url,
            apply_url=None,
            description=None,
            salary_min=x.get("salary_min"),
            salary_max=x.get("salary_max"),
            salary_currency="USD" if x.get("salary_min") or x.get("salary_max") else None,
            posted_at=self.received_at,
            lead_meta={"origin": "wellfound", "origin_job_id": self.origin_job_id, "needs_resolution": True,
                       "resolved": False, "wellfound_url": self.origin_url, "company_slug": x.get("company_slug"),
                       "salary_text": x.get("salary_text"), "equity_min_pct": x.get("equity_min_pct"),
                       "equity_max_pct": x.get("equity_max_pct")},
        )


def _split_company_location(texts: list[str]) -> tuple[str | None, str | None]:
    texts = [t for t in texts if t and len(t) < 160]
    if not texts:
        return None, None
    first = texts[0]
    for sep in (" · ", " • ", " - ", " | "):
        if sep in first:
            a, b = first.split(sep, 1)
            return a.strip() or None, b.strip() or None
    company = first
    location = texts[1] if len(texts) > 1 else None
    return company, location


# ---------------- Wellfound ----------------
def _humanize_slug(slug: str | None) -> str | None:
    return " ".join(w.capitalize() for w in slug.split("-")) if slug else None


def parse_comp(text: str) -> dict[str, Any]:
    """'$150k – $190k • 0.1% – 0.5%' -> salary_min/max (USD) and equity_min/max_pct."""
    from jobbuddy.fetchers.base import parse_salary_text

    out: dict[str, Any] = {}
    lo, hi = parse_salary_text(text)
    if lo or hi:
        out.update(salary_min=lo, salary_max=hi)
    eq_part = text.split("$")[-1] if "$" in text else text
    m = EQUITY_RE.search(eq_part.replace(",", "."))
    if m and "%" in text:
        a = float(m.group(1))
        b = float(m.group(2)) if m.group(2) else a
        if a <= 100 and b <= 100:
            out.update(equity_min_pct=min(a, b), equity_max_pct=max(a, b))
    if out:
        out["salary_text"] = _clean(text)[:200]
    return out


def _wf_classify(texts: list[str]) -> dict[str, Any]:
    """Lines after a Wellfound job link: company, location/remote, compensation (any order, '·'-joined)."""
    info: dict[str, Any] = {}
    parts: list[str] = []
    for t in texts:
        parts += [p.strip() for p in re.split(r"\s[·•|]\s", t) if p.strip()]
    rest = []
    for part in parts:
        low = part.lower()
        if SALARY_HINT_RE.search(part) and ("$" in part or "%" in part or "equity" in low):
            comp = parse_comp(part) or {"salary_text": _clean(part)[:200]}
            prev = info.get("salary_text")
            info.update(comp)
            if prev:
                info["salary_text"] = f"{prev} · {comp['salary_text']}"[:200]
        elif re.search(r"\bremote\b|\bonsite\b|\bon-site\b|\bhybrid\b", low) or re.search(
                r",\s*[A-Z]{2}\b|united states|\busa?\b|\bbay area\b|san francisco|new york", part, re.I):
            info["location"] = f"{info['location']}; {part}" if info.get("location") else part
            if "remote" in low:
                info["remote"] = True
        elif re.match(r"(?i)^(view|apply|see|save|new|posted|\d+ (?:days?|hours?) ago)\b", part):
            continue
        else:
            rest.append(part)
    if rest:
        info["company_text"] = rest[0]
    return info


def parse_wellfound_html(html: str, received_at: datetime | None = None) -> list[AlertLead]:
    lin = _Linearizer()
    lin.feed(html)
    items = lin.items
    leads: dict[str, AlertLead] = {}
    last_company: str | None = None
    for i, it in enumerate(items):
        if it.kind == "a" and it.href and WF_COMPANY_RE.search(it.href) and not WF_JOB_RE.search(it.href):
            if it.text and it.text.lower() not in SKIP_ANCHOR_TEXT:
                last_company = it.text
            continue
        if it.kind != "a" or not it.href:
            continue
        m = WF_JOB_RE.search(it.href)
        if not m or it.text.lower() in SKIP_ANCHOR_TEXT or m.group(2) in leads:
            continue
        follow: list[str] = []
        for nxt in items[i + 1:i + 6]:
            if nxt.kind == "a" and nxt.href and (WF_JOB_RE.search(nxt.href) or WF_COMPANY_RE.search(nxt.href)):
                break
            if nxt.kind == "text" and nxt.text.lower() not in SKIP_ANCHOR_TEXT:
                follow.append(nxt.text)
        leads[m.group(2)] = _wf_lead(it.text, m, follow, last_company, received_at)
        last_company = None  # a company link only labels the job that directly follows it
    return list(leads.values())


def _wf_lead(title: str, m: re.Match[str], follow: list[str], company: str | None,
             received_at: datetime | None) -> AlertLead:
    title = _clean(title)
    hiring = re.match(r"(?i)^(.+?)\s+is hiring(?: an?| for)?\s+(.+)$", title)
    if hiring:  # digest style: "Acme Health is hiring a Product Manager"
        company, title = hiring.group(1).strip(), hiring.group(2).strip()
    info = _wf_classify(follow)
    slug = m.group(1)
    company = company or info.get("company_text") or _humanize_slug(slug)  # hiring-line > link > text > slug
    extra = {k: v for k, v in info.items() if k not in ("location", "company_text")}
    extra["company_slug"] = slug
    url = f"https://wellfound.com/jobs/{m.group(2)}"
    return AlertLead(origin="wellfound", origin_job_id=m.group(2), title=title, company=company,
                     location=info.get("location"), origin_url=url, received_at=received_at, extra=extra)


def parse_wellfound_text(text: str, received_at: datetime | None = None) -> list[AlertLead]:
    lines = [ln.strip() for ln in text.splitlines()]
    leads: dict[str, AlertLead] = {}
    for idx, ln in enumerate(lines):
        m = WF_JOB_RE.search(ln)
        if not m or m.group(2) in leads:
            continue
        block: list[str] = []
        for p in reversed(lines[max(0, idx - 5):idx]):  # walk back to the previous blank line or link
            if not p or "http" in p:
                break
            block.insert(0, p)
        prefix = re.sub(r"(?i)\s*(?:view job|apply)\s*:?\s*$", "", ln.split("http")[0]).strip()
        if prefix and not block:
            block = [prefix]
        if not block:
            continue
        title, follow = block[0], block[1:]
        leads[m.group(2)] = _wf_lead(title, m, follow, None, received_at)
    return list(leads.values())


def parse_alert_html(html: str, origin: str, received_at: datetime | None = None) -> list[AlertLead]:
    if origin == "wellfound":
        return parse_wellfound_html(html, received_at)
    pattern = LI_JOB_RE if origin == "linkedin" else INDEED_JK_RE
    lin = _Linearizer()
    lin.feed(html)
    items = lin.items
    leads: dict[str, AlertLead] = {}
    for i, it in enumerate(items):
        if it.kind != "a" or not it.href:
            continue
        m = pattern.search(it.href)
        if not m or it.text.lower() in SKIP_ANCHOR_TEXT:
            continue
        jid = m.group(1)
        if jid in leads:
            continue
        follow: list[str] = []
        for nxt in items[i + 1:i + 6]:
            if nxt.kind == "a" and nxt.href and pattern.search(nxt.href) and nxt.text.lower() not in SKIP_ANCHOR_TEXT:
                break
            if nxt.kind == "text" or (nxt.kind == "a" and nxt.text.lower() not in SKIP_ANCHOR_TEXT):
                follow.append(nxt.text)
            if len(follow) >= 2:
                break
        company, location = _split_company_location(follow)
        leads[jid] = AlertLead(origin=origin, origin_job_id=jid, title=it.text, company=company, location=location,
                               origin_url=f"https://www.linkedin.com/jobs/view/{jid}/" if origin == "linkedin"
                               else f"https://www.indeed.com/viewjob?jk={jid}",
                               received_at=received_at)
    return list(leads.values())


def parse_alert_text(text: str, origin: str, received_at: datetime | None = None) -> list[AlertLead]:
    """Plain-text alert bodies: 'Title\\nCompany\\nLocation\\nView job: <url>' blocks."""
    if origin == "wellfound":
        return parse_wellfound_text(text, received_at)
    pattern = LI_JOB_RE if origin == "linkedin" else INDEED_JK_RE
    lines = [ln.strip() for ln in text.splitlines()]
    leads: dict[str, AlertLead] = {}
    for idx, ln in enumerate(lines):
        m = pattern.search(ln)
        if not m or m.group(1) in leads:
            continue
        prev = [p for p in lines[max(0, idx - 4):idx] if p and not pattern.search(p) and "http" not in p]
        if not prev:
            continue
        block = prev[-3:]
        title = block[0]
        company, location = _split_company_location(block[1:])
        leads[m.group(1)] = AlertLead(origin, m.group(1), title, company, location, received_at=received_at)
    return list(leads.values())


FWD_SUBJECT_RE = re.compile(r"^\s*(?:(?:fwd?|fw|tr|wg|rv)\s*:\s*)+", re.I)
FWD_MARKER_RE = re.compile(r"-{2,}\s*(?:forwarded message|original message)\s*-{2,}|^begin forwarded message:",
                           re.I | re.M)
FWD_FROM_RE = re.compile(r"^\s*(?:>\s*)*(?:\*\s*)?from:\s*\**\s*(.+)$", re.I | re.M)
FWD_DATE_RE = re.compile(r"^\s*(?:>\s*)*(?:\*\s*)?(?:date|sent):\s*\**\s*(.+)$", re.I | re.M)


def _origin_from_text(text: str) -> str | None:
    t = (text or "").lower()
    if any(d in t for d in WELLFOUND_SENDER_DOMAINS):
        return "wellfound"
    if "linkedin.com" in t:
        return "linkedin"
    if "indeed.com" in t:
        return "indeed"
    return None


def unquote_forwarded(text: str) -> str:
    """Strip '> ' quoting used by plain-text forwards/replies (any depth)."""
    return "\n".join(re.sub(r"^\s*(?:>\s?)+", "", ln) for ln in (text or "").splitlines())


def _bodies(msg: Message) -> tuple[list[str], list[str]]:
    htmls, texts = [], []
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart" or part.get_content_type() == "message/rfc822":
            continue  # walk() descends into attached (forwarded-as-attachment) messages by itself
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if part.get_content_type() == "text/html":
            htmls.append(body)
        elif part.get_content_type() == "text/plain":
            texts.append(body)
    return htmls, texts


def is_forwarded(msg: Message) -> bool:
    if FWD_SUBJECT_RE.match(msg.get("Subject") or ""):
        return True
    htmls, texts = _bodies(msg)
    return any(FWD_MARKER_RE.search(b) for b in htmls + texts) or any(
        p.get_content_type() == "message/rfc822" for p in msg.walk())


def _origin_for(msg: Message) -> str | None:
    """Direct alert: the From header. Forwarded alert: the forwarded From line, then job-link domains."""
    origin = _origin_from_text(msg.get("From") or "")
    if origin:
        return origin
    htmls, texts = _bodies(msg)
    for part in msg.walk():
        if part.get_content_type() == "message/rfc822":
            for inner in part.get_payload() or []:
                if isinstance(inner, Message):
                    origin = _origin_from_text(inner.get("From") or "")
                    if origin:
                        return origin
    for body in texts + htmls:
        plain = re.sub(r"<[^>]+>", " ", unquote_forwarded(body))
        for m in FWD_FROM_RE.finditer(plain):
            origin = _origin_from_text(m.group(1))
            if origin:
                return origin
    for body in htmls + texts:
        if WF_JOB_RE.search(body):
            return "wellfound"
        if LI_JOB_RE.search(body):
            return "linkedin"
        if INDEED_JK_RE.search(body):
            return "indeed"
    return None


def _received_at(msg: Message, texts: list[str]) -> datetime | None:
    raw = msg.get("Date")
    if is_forwarded(msg):  # prefer the original alert's date from the forwarded header block
        for body in texts:
            m = FWD_DATE_RE.search(unquote_forwarded(body))
            if m:
                raw = m.group(1).strip()
                break
    try:
        return parsedate_to_datetime(raw) if raw else None
    except (TypeError, ValueError, IndexError):
        try:
            return parsedate_to_datetime(msg.get("Date")) if msg.get("Date") else None
        except (TypeError, ValueError, IndexError):
            return None


def parse_alert_message(msg: Message) -> list[AlertLead]:
    origin = _origin_for(msg)
    if origin is None:
        return []
    htmls, texts = _bodies(msg)
    received = _received_at(msg, texts)
    leads: dict[str, AlertLead] = {}
    for html in htmls:
        for lead in parse_alert_html(html, origin, received):
            leads.setdefault(lead.origin_job_id, lead)
    if not leads:
        for text in texts:
            for lead in parse_alert_text(unquote_forwarded(text), origin, received):
                leads.setdefault(lead.origin_job_id, lead)
    return list(leads.values())


def message_from_json(data: dict[str, Any]) -> Message:
    """{from, subject, html, text, date?, message_id?} -> email.message.EmailMessage."""
    from email.message import EmailMessage

    m = EmailMessage()
    m["From"] = str(data.get("from") or "")
    m["Subject"] = str(data.get("subject") or "")
    if data.get("date"):
        m["Date"] = str(data["date"])
    if data.get("message_id"):
        m["Message-ID"] = str(data["message_id"])
    text, html = data.get("text"), data.get("html")
    if text:
        m.set_content(str(text))
    if html:
        if text:
            m.add_alternative(str(html), subtype="html")
        else:
            m.set_content(str(html), subtype="html")
    if not text and not html:
        m.set_content("")
    return m


def message_key(msg: Message) -> str:
    """Stable de-duplication key: Message-ID, else a hash of subject + bodies (JSON input often lacks one)."""
    mid = (msg.get("Message-ID") or "").strip()
    if mid:
        return mid
    htmls, texts = _bodies(msg)
    h = hashlib.sha256(json.dumps([msg.get("Subject") or "", htmls, texts]).encode("utf-8")).hexdigest()
    return f"no-id:{h}"


# ---------------- resolution to employer ATS ----------------
def _norm(s: str | None) -> str:
    s = (s or "").lower()
    s = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|company|the)\b\.?", " ", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


@dataclass
class Resolution:
    apply_url: str
    source: str
    external_job_id: str
    score: float
    posting: JobPosting | None = None  # set when found via a public-board probe (not yet in the DB)


def resolve_against(lead: AlertLead | JobPosting, candidates: Iterable[Any], min_score: float = 0.86) -> Resolution | None:
    """candidates: objects with .company_name/.title/.apply_url/.source/.external_job_id (Job rows or JobPostings)."""
    company = _norm(getattr(lead, "company", None) or getattr(lead, "company_name", None))
    title = lead.title
    best: Resolution | None = None
    for c in candidates:
        if c.source in ALERT_SOURCES or c.source.endswith("_alert") or not c.apply_url:
            continue
        cc = _norm(c.company_name)
        if company and cc and company not in cc and cc not in company:
            continue
        s = title_similarity(title, c.title)
        if s >= min_score and (best is None or s > best.score):
            best = Resolution(c.apply_url, c.source, c.external_job_id, s)
    return best


def slug_candidates(company: str | None, extra_slugs: Iterable[str | None] = ()) -> list[str]:
    out = [s.lower() for s in extra_slugs if s]
    n = _norm(company)
    if n:
        words = n.split()
        out += ["".join(words), "-".join(words), words[0]]
    return list(dict.fromkeys(out))


async def probe_public_boards(http: Any, lead: AlertLead | JobPosting) -> Resolution | None:
    """Try the employer's PUBLIC Ashby/Greenhouse/Lever board APIs by slug (Wellfound's company slug first).
    Never touches LinkedIn, Indeed or Wellfound."""
    from jobbuddy.fetchers.ashby import API as ASHBY, parse_ashby
    from jobbuddy.fetchers.greenhouse import API as GH, parse_greenhouse
    from jobbuddy.fetchers.lever import API as LEVER, parse_lever

    company = getattr(lead, "company", None) or getattr(lead, "company_name", None)
    extra = getattr(lead, "extra", None) or {}
    for slug in slug_candidates(company, [extra.get("company_slug")]):
        for url, parser in ((ASHBY.format(name=slug), parse_ashby), (GH.format(token=slug), parse_greenhouse),
                            (LEVER.format(company=slug), parse_lever)):
            try:
                r = await http.request("GET", url)
                if r.status_code != 200:
                    continue
                postings: Sequence[JobPosting] = parser(r.json(), company)
            except Exception:
                continue
            res = resolve_against(lead, postings)
            if res:
                res.posting = next((p for p in postings if p.external_job_id == res.external_job_id), None)
                return res
    return None
