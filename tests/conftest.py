from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

import pytest
import pytest_asyncio

if TYPE_CHECKING:
    import httpx

from jobbuddy.config import AppConfig, EngineConfig, Settings
from jobbuddy.embeddings import HashingEmbedder
from jobbuddy.llm.client import LLMResponse, TokenBudget
from jobbuddy.profile import load_profile

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def profile():
    return load_profile(ROOT / "profile" / "profile.example.yaml")


class FakeLLM:
    """Mocked LLM: `handlers` maps purpose -> callable(messages) -> str (or a static str)."""

    def __init__(self, handlers: dict[str, Any] | None = None, budget: int = 100_000) -> None:
        self.handlers = handlers or {}
        self.budget = TokenBudget(budget)
        self.model = "fake-model"
        self.calls: list[dict[str, Any]] = []

    def chat(self, messages, *, purpose, json_mode=False, max_tokens=None, temperature=None):
        self.calls.append({"purpose": purpose, "messages": messages, "json_mode": json_mode})
        self.budget.check(self.budget.estimate(messages, max_tokens or 100))
        h = self.handlers.get(purpose)
        if h is None:
            raise AssertionError(f"unexpected LLM call: {purpose}")
        text = h(messages) if callable(h) else h
        if not isinstance(text, str):
            text = json.dumps(text)
        self.budget.consume(50)
        return LLMResponse(text=text, prompt_tokens=40, completion_tokens=10, model=self.model)


@pytest.fixture
def fake_llm_factory() -> Callable[..., FakeLLM]:
    return FakeLLM


@pytest.fixture
def embedder():
    return HashingEmbedder()


@pytest.fixture
def db_url(tmp_path):
    """Fresh SQLite DB built by running the real Alembic migration 0001."""
    from alembic import command
    from alembic.config import Config

    url = f"sqlite:///{tmp_path / 'test.db'}"
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.attributes["url"] = url
    command.upgrade(cfg, "head")
    return url


@pytest.fixture
def session(db_url):
    from jobbuddy.db.session import get_engine, session_factory

    s = session_factory(get_engine(db_url))()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def settings(tmp_path, db_url):
    companies = tmp_path / "companies.yaml"
    companies.write_text("companies: []\n")
    return Settings(database_url=db_url, jobbuddy_companies=str(companies), _env_file=None)


@pytest.fixture
def app_config():
    cfg = AppConfig()
    cfg.engine = EngineConfig(typing_delay_ms=(0, 0), action_delay_ms=(0, 0), max_steps=8)
    return cfg


@pytest_asyncio.fixture
async def browser_context():
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright not installed")
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
    except Exception as exc:  # browsers not installed
        await pw.stop()
        pytest.skip(f"chromium unavailable: {exc}")
    ctx = await browser.new_context(viewport={"width": 1200, "height": 900})
    yield ctx
    await ctx.close()
    await browser.close()
    await pw.stop()


def fixture_url(name: str) -> str:
    return (FIXTURES / name).resolve().as_uri()


# ---------------- mocked Neon SQL-over-HTTP endpoint (backed by the SQLite test DB) ----------------
NEON_TEST_URL = "postgresql+psycopg://agent:not-a-real-password@ep-test-123456.us-east-2.aws.neon.tech/jobbuddy" \
                "?sslmode=require&channel_binding=require"


class NeonMock:
    """Speaks Neon's /sql protocol (raw text output, array mode, batch transactions) and executes the
    Postgres-flavoured statements on SQLite: CAST($n AS T) -> ?n, '\\x..' -> bytes, 'true'/'false' -> 1/0,
    ISO timestamps -> SQLite text format. Records every request for assertions."""

    def __init__(self, db_path: str) -> None:
        import re

        self.db_path = db_path
        self.requests: list[dict[str, Any]] = []
        self.fail_next: int = 0
        self._cast = re.compile(r"CAST\((\$\d+) AS [A-Z]+\)")
        self._param = re.compile(r"\$(\d+)")
        self._iso = re.compile(r"^(\d{4}-\d\d-\d\d)T(\d\d:\d\d:\d\d(?:\.\d+)?)(?:\+00:00|Z)$")

    def _sql(self, q: str) -> str:
        return self._param.sub(lambda m: f"?{m.group(1)}", self._cast.sub(r"\1", q))

    def _param_value(self, v: Any) -> Any:
        if isinstance(v, str):
            if v.startswith("\\x"):
                return bytes.fromhex(v[2:])
            if v in ("true", "false"):
                return 1 if v == "true" else 0
            m = self._iso.match(v)
            if m:
                return f"{m.group(1)} {m.group(2)}"
        return v

    @staticmethod
    def _text(v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, bytes):
            return "\\x" + v.hex()
        return str(v)

    def _run(self, cur: Any, q: dict[str, Any]) -> dict[str, Any]:
        cur.execute(self._sql(q["query"]), [self._param_value(p) for p in q.get("params") or []])
        if cur.description:
            rows = [[self._text(v) for v in r] for r in cur.fetchall()]
            return {"command": "SELECT", "rowCount": len(rows), "fields": [{"name": d[0]} for d in cur.description],
                    "rows": rows, "rowAsArray": True}
        return {"command": q["query"].split()[0].upper(), "rowCount": cur.rowcount, "fields": [], "rows": [],
                "rowAsArray": True}

    def handler(self, request: "httpx.Request") -> "httpx.Response":
        import sqlite3

        import httpx

        body = json.loads(request.content)
        self.requests.append({"url": str(request.url), "headers": dict(request.headers), "body": body})
        if self.fail_next:
            self.fail_next -= 1
            return httpx.Response(503, text="unavailable")
        h = request.headers
        assert h["neon-connection-string"].startswith("postgresql://"), "driver suffix must be stripped"
        assert h["neon-raw-text-output"] == "true" and h["neon-array-mode"] == "true"
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.cursor()
            if "queries" in body:
                results = [self._run(cur, q) for q in body["queries"]]
                conn.commit()
                return httpx.Response(200, json={"results": results})
            out = self._run(cur, body)
            conn.commit()
            return httpx.Response(200, json=out)
        except sqlite3.Error as exc:
            conn.rollback()
            return httpx.Response(400, json={"message": f"{exc} (conn {h['neon-connection-string']})",
                                             "code": "XX000"})
        finally:
            conn.close()


@pytest.fixture
def neon(db_url):
    import httpx

    from jobbuddy.db.neon_http import NeonHttpTransport

    mock = NeonMock(db_url.removeprefix("sqlite:///"))
    transport = NeonHttpTransport(NEON_TEST_URL, client=httpx.Client(transport=httpx.MockTransport(mock.handler)))
    transport.mock = mock  # type: ignore[attr-defined]
    return transport


@pytest.fixture
def agent(neon, profile, app_config, tmp_path):
    from jobbuddy.agent_io import AgentIO

    app_config.tailoring.output_formats = ["docx"]
    return AgentIO(neon, profile, app_config, workdir=tmp_path / "agentwork",
                   style_sample="I am writing to express my strong interest. Thank you for your consideration.")
