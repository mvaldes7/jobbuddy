"""Minimal client for Neon's serverless SQL-over-HTTP endpoint.

Used by the on-box AI agent, whose machine cannot reach Postgres on port 5432. Protocol (same as
@neondatabase/serverless):

    POST https://<endpoint host>/sql
    Neon-Connection-String: postgresql://user:pass@host/db?sslmode=require
    Neon-Raw-Text-Output: true      -> every value comes back as Postgres text (or null)
    Neon-Array-Mode: true           -> rows are arrays, column names in "fields"
    body: {"query": "SELECT ... $1", "params": ["..."]}
    batch (one transaction): {"queries": [{"query": ..., "params": [...]}, ...]} -> {"results": [...]}

The connection string travels in a header over TLS; it is never logged. Statements are plain DML:
the runtime role cannot run DDL, and nothing here creates objects.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from typing import Any, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx

from jobbuddy.db.transport import QueryResult, Statement

log = logging.getLogger(__name__)


class NeonHttpError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def to_connection_string(database_url: str) -> str:
    """postgresql+psycopg://... -> postgresql://...  (drops the SQLAlchemy driver suffix)."""
    url = database_url.strip()
    url = re.sub(r"^postgres(?:ql)?\+[a-z0-9_]+://", "postgresql://", url)
    url = re.sub(r"^postgres://", "postgresql://", url)
    if not url.startswith("postgresql://"):
        raise ValueError("neon_http needs a postgresql:// DATABASE_URL")
    return url


def endpoint_for(connection_string: str, override: str | None = None) -> str:
    """Default: https://<host>/sql. NEON_HTTP_ENDPOINT may override it (the official JS driver uses
    https://api.<region>.<cloud>.neon.tech/sql, i.e. the first host label replaced by 'api')."""
    if override:
        return override
    host = urlsplit(connection_string).hostname
    if not host:
        raise ValueError("DATABASE_URL has no host")
    return f"https://{host}/sql"


def encode_param(v: Any) -> Any:
    """Python value -> wire value, mirroring the JS driver's prepareValue + bytea encoding."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(v).hex()
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (_dt.datetime, _dt.date)):
        return v.isoformat()
    if isinstance(v, (int, float)):
        return str(v)
    return str(v)


def decode_bytea(v: Any) -> bytes | None:
    if v is None:
        return None
    if isinstance(v, (bytes, bytearray, memoryview)):
        return bytes(v)
    s = str(v)
    if s.startswith("\\x"):
        return bytes.fromhex(s[2:])
    return s.encode("utf-8")


def _redact(msg: str) -> str:
    return re.sub(r"postgres(?:ql)?://\S+", "postgresql://***", msg or "")


class NeonHttpTransport:
    """SqlTransport over HTTPS. Always speaks Postgres SQL with $1..$n placeholders."""

    dialect = "postgresql"
    paramstyle = "dollar"

    def __init__(self, database_url: str, *, endpoint: str | None = None, timeout: float = 30.0,
                 client: httpx.Client | None = None, max_retries: int = 2) -> None:
        self._conn = to_connection_string(database_url)
        self.endpoint = endpoint_for(self._conn, endpoint)
        self._client = client or httpx.Client(timeout=timeout)
        self._max_retries = max_retries

    # ---------------- wire ----------------
    def _headers(self, batch: bool) -> dict[str, str]:
        h = {
            "Neon-Connection-String": self._conn,
            "Neon-Raw-Text-Output": "true",
            "Neon-Array-Mode": "true",
            "Content-Type": "application/json",
        }
        if batch:
            h["Neon-Batch-Isolation-Level"] = "ReadCommitted"
            h["Neon-Batch-Read-Only"] = "false"
        return h

    def _post(self, body: dict[str, Any], batch: bool) -> dict[str, Any]:
        last: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                r = self._client.post(self.endpoint, json=body, headers=self._headers(batch))
            except httpx.TransportError as exc:  # connection-level: safe to retry (nothing executed)
                last = exc
                continue
            if r.status_code in (502, 503, 504) and attempt < self._max_retries:
                last = NeonHttpError(f"HTTP {r.status_code}", status=r.status_code)
                continue
            if r.status_code >= 400:
                try:
                    data = r.json()
                    msg, code = data.get("message") or r.text, data.get("code")
                except ValueError:
                    msg, code = r.text, None
                raise NeonHttpError(_redact(str(msg))[:500], status=r.status_code, code=code)
            return r.json()
        raise NeonHttpError(f"Neon HTTP endpoint unreachable: {type(last).__name__}")

    @staticmethod
    def _result(data: dict[str, Any]) -> QueryResult:
        cols = [f.get("name") for f in data.get("fields") or []]
        rows = [list(r) for r in data.get("rows") or []]
        rc = data.get("rowCount")
        return QueryResult(columns=cols, rows=rows, row_count=int(rc) if rc is not None else len(rows),
                           command=data.get("command"))

    # ---------------- SqlTransport ----------------
    def query(self, sql: str, params: Sequence[Any] = ()) -> QueryResult:
        return self._result(self._post({"query": sql, "params": [encode_param(p) for p in params]}, batch=False))

    def transaction(self, statements: Sequence[Statement]) -> list[QueryResult]:
        if not statements:
            return []
        body = {"queries": [{"query": s, "params": [encode_param(p) for p in ps]} for s, ps in statements]}
        data = self._post(body, batch=True)
        results = data.get("results")
        if not isinstance(results, list):
            raise NeonHttpError("unexpected batch response format")
        return [self._result(r) for r in results]

    def close(self) -> None:
        self._client.close()

    def __repr__(self) -> str:  # never print the connection string
        return f"NeonHttpTransport(endpoint={urlunsplit(urlsplit(self.endpoint)._replace(query=''))!r})"
