from __future__ import annotations

import asyncio
import html as html_lib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml
from pydantic import BaseModel

log = logging.getLogger(__name__)


class JobPosting(BaseModel):
    source: str
    external_job_id: str
    title: str
    company_name: str | None = None
    location: str | None = None
    remote: bool | None = None
    url: str | None = None
    apply_url: str | None = None
    description: str | None = None  # plain text
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None
    posted_at: datetime | None = None
    lead_meta: dict[str, Any] | None = None


class WorkdayTarget(BaseModel):
    host: str
    tenant: str
    site: str


class CompanyTarget(BaseModel):
    name: str
    ats: str
    board_token: str | None = None
    workday: WorkdayTarget | None = None
    careers_url: str | None = None
    active: bool = True

    @property
    def key(self) -> str:
        if self.ats == "workday" and self.workday:
            return f"{self.workday.host}/{self.workday.tenant}/{self.workday.site}"
        return self.board_token or self.name


def load_companies(path: Path) -> list[CompanyTarget]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [CompanyTarget.model_validate(c) for c in data.get("companies", [])]


_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr|section)[^>]*>", re.I)


def html_to_text(raw: str | None) -> str:
    if not raw:
        return ""
    s = html_lib.unescape(raw)  # Greenhouse double-encodes content
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = _BLOCK_RE.sub("\n", s)
    s = _TAG_RE.sub(" ", s)
    s = html_lib.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


REMOTE_RE = re.compile(r"\bremote\b|\banywhere\b|work from home|\bwfh\b", re.I)


def guess_remote(*texts: str | None) -> bool | None:
    joined = " ".join(t for t in texts if t)
    if not joined:
        return None
    return True if REMOTE_RE.search(joined) else None


def parse_iso(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):  # epoch ms (Lever)
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_relative_posted(text: str | None, now: datetime | None = None) -> datetime | None:
    """Workday 'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago'."""
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    t = text.lower()
    if "today" in t:
        return now
    if "yesterday" in t:
        return now - timedelta(days=1)
    m = re.search(r"(\d+)\+?\s*day", t)
    if m:
        return now - timedelta(days=int(m.group(1)))
    return None


_SAL_RE = re.compile(r"\$\s?(\d{2,3}(?:,\d{3})+|\d{2,3}(?:\.\d+)?\s?[kK])")


def parse_salary_text(text: str | None) -> tuple[int | None, int | None]:
    """Extract a USD range like '$150,000 - $190,000' or '$150k-$190k' from free text."""
    if not text:
        return None, None
    vals = []
    for m in _SAL_RE.finditer(text):
        raw = m.group(1).replace(",", "").replace(" ", "")
        v = float(raw[:-1]) * 1000 if raw[-1] in "kK" else float(raw)
        if 20_000 <= v <= 2_000_000:
            vals.append(int(v))
    if not vals:
        return None, None
    return min(vals), max(vals)


class PoliteHttp:
    """httpx.AsyncClient wrapper: identifying User-Agent, per-host delay, retry with backoff."""

    def __init__(self, user_agent: str, delay: float = 1.5, retries: int = 3, timeout: float = 30,
                 client: httpx.AsyncClient | None = None) -> None:
        self.delay = delay
        self.retries = retries
        self._client = client or httpx.AsyncClient(timeout=timeout, headers={"User-Agent": user_agent,
                                                                             "Accept": "application/json"},
                                                   follow_redirects=True)
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self) -> "PoliteHttp":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.aclose()

    async def _wait(self, host: str) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            elapsed = time.monotonic() - self._last.get(host, 0.0)
            if elapsed < self.delay:
                await asyncio.sleep(self.delay - elapsed)
            self._last[host] = time.monotonic()

    async def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        host = urlparse(url).netloc
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            await self._wait(host)
            try:
                r = await self._client.request(method, url, **kw)
            except httpx.TransportError as exc:
                last_exc = exc
                await asyncio.sleep(min(30, 2 ** attempt))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                retry_after = r.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt * 2
                log.warning("http %s from %s; backing off %.0fs", r.status_code, host, wait)
                await asyncio.sleep(min(wait, 60))
                continue
            return r
        if last_exc:
            raise last_exc
        return r  # type: ignore[possibly-undefined]

    async def get_json(self, url: str, **kw: Any) -> Any:
        r = await self.request("GET", url, **kw)
        r.raise_for_status()
        return r.json()

    async def post_json(self, url: str, payload: Any, **kw: Any) -> Any:
        r = await self.request("POST", url, json=payload, **kw)
        r.raise_for_status()
        return r.json()


class Fetcher:
    source: str = ""

    def __init__(self, http: PoliteHttp) -> None:
        self.http = http

    async def fetch(self, target: CompanyTarget) -> list[JobPosting]:  # pragma: no cover - interface
        raise NotImplementedError
