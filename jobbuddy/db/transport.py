"""DB transport abstraction for the on-box agent's raw-SQL work-queue operations.

* SqlAlchemyTransport (default, DATABASE_TRANSPORT=sqlalchemy): any SQLAlchemy URL (psycopg on Neon,
  SQLite for dev/tests). Used by CI and the dashboard through the ORM, and by agent commands when the
  machine can reach the database directly.
* NeonHttpTransport (DATABASE_TRANSPORT=neon_http): Neon serverless SQL-over-HTTPS (jobbuddy/db/neon_http.py).

SQL is written once with Postgres-style $1..$n placeholders; dialect differences (casts) go through
`cast()`. Values come back raw (Neon returns Postgres text), so callers normalize with the helpers below.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

Statement = tuple[str, Sequence[Any]]

_DOLLAR = re.compile(r"\$(\d+)")


@dataclass
class QueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    row_count: int = 0
    command: str | None = None

    def dicts(self) -> list[dict[str, Any]]:
        return [dict(zip(self.columns, r)) for r in self.rows]

    def first(self) -> dict[str, Any] | None:
        d = self.dicts()
        return d[0] if d else None


class SqlTransport(Protocol):
    dialect: str

    def query(self, sql: str, params: Sequence[Any] = ()) -> QueryResult: ...

    def transaction(self, statements: Sequence[Statement]) -> list[QueryResult]: ...

    def close(self) -> None: ...


def cast(transport: SqlTransport, placeholder: str, pg_type: str) -> str:
    """Explicit casts are needed on Postgres where a parameter has no inferable type (e.g. in an
    INSERT ... SELECT list). SQLite has no such types, so the bare placeholder is used there."""
    if transport.dialect == "postgresql":
        return f"CAST({placeholder} AS {pg_type})"
    return placeholder


class SqlAlchemyTransport:
    paramstyle = "dollar"

    def __init__(self, url_or_engine: Any) -> None:
        from sqlalchemy.engine import Engine

        from jobbuddy.db.session import get_engine

        self.engine: Engine = url_or_engine if isinstance(url_or_engine, Engine) else get_engine(url_or_engine)
        self.dialect = self.engine.dialect.name

    @staticmethod
    def _convert(sql: str, params: Sequence[Any]) -> tuple[str, dict[str, Any]]:
        bound: dict[str, Any] = {}
        for i, v in enumerate(params, start=1):
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            elif isinstance(v, memoryview):
                v = bytes(v)
            bound[f"p{i}"] = v
        return _DOLLAR.sub(lambda m: f":p{m.group(1)}", sql), bound

    def _run(self, conn: Any, sql: str, params: Sequence[Any]) -> QueryResult:
        from sqlalchemy import text

        q, bound = self._convert(sql, params)
        res = conn.execute(text(q), bound)
        if res.returns_rows:
            cols = list(res.keys())
            rows = [list(r) for r in res.fetchall()]
            return QueryResult(cols, rows, len(rows))
        return QueryResult([], [], res.rowcount or 0)

    def query(self, sql: str, params: Sequence[Any] = ()) -> QueryResult:
        with self.engine.begin() as conn:
            return self._run(conn, sql, params)

    def transaction(self, statements: Sequence[Statement]) -> list[QueryResult]:
        with self.engine.begin() as conn:
            return [self._run(conn, s, p) for s, p in statements]

    def close(self) -> None:
        pass


def build_transport(settings: Any = None) -> SqlTransport:
    from jobbuddy.config import get_settings

    s = settings or get_settings()
    kind = (s.database_transport or "sqlalchemy").strip().lower()
    if kind == "neon_http":
        from jobbuddy.db.neon_http import NeonHttpTransport

        return NeonHttpTransport(s.database_url, endpoint=s.neon_http_endpoint)
    if kind in ("sqlalchemy", "psycopg", "default", ""):
        return SqlAlchemyTransport(s.database_url)
    raise ValueError(f"unknown DATABASE_TRANSPORT {kind!r} (use sqlalchemy or neon_http)")


# ---------------- value normalization (Neon raw text vs. native driver values) ----------------
def as_json(v: Any) -> Any:
    if v is None or isinstance(v, (dict, list, bool, int, float)):
        return v
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return v


def as_bool(v: Any) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).strip().lower() in ("t", "true", "1", "yes")


def as_int(v: Any) -> int | None:
    return None if v is None or v == "" else int(float(v))


def as_float(v: Any) -> float | None:
    return None if v is None or v == "" else float(v)
