"""Engine/session factory. One session per pipeline run (batched commits)."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_ENGINES: dict[str, Engine] = {}


def normalize_url(url: str) -> str:
    """Accept Neon/Heroku style URLs and force the psycopg (v3) driver."""
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def get_engine(url: str | None = None) -> Engine:
    if url is None:
        from jobbuddy.config import get_settings

        url = get_settings().database_url
    url = normalize_url(url)
    if url in _ENGINES:
        return _ENGINES[url]
    kwargs: dict = {"pool_pre_ping": True, "future": True}
    if url.startswith("sqlite"):
        kwargs = {"future": True, "connect_args": {"check_same_thread": False}}
    elif url.startswith("mssql"):
        # Azure SQL: ODBC Driver 18 (encrypt by default); fast_executemany for batch upserts.
        kwargs["fast_executemany"] = True
        kwargs["pool_recycle"] = 1800
    else:
        # Neon pooled endpoint (PgBouncer, transaction mode): no server-side prepared statements.
        # sslmode=require&channel_binding=require in the URL is passed straight to libpq (>= 13).
        kwargs["pool_size"] = 3
        kwargs["max_overflow"] = 2
        kwargs["pool_recycle"] = 300  # Neon pooler closes idle connections
        kwargs["connect_args"] = {"prepare_threshold": None}
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    _ENGINES[url] = engine
    return engine


def session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=engine or get_engine(), expire_on_commit=False, future=True)


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    session = session_factory(engine)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def create_all_for_dev(engine: Engine) -> None:
    """SQLite-only dev convenience. Production schema comes ONLY from Alembic via DATABASE_URL_DIRECT;
    the runtime role (jobbuddy_app) is DML-only, so app code never runs DDL against DATABASE_URL."""
    if engine.dialect.name != "sqlite":
        raise RuntimeError("create_all_for_dev is SQLite-only; run `alembic upgrade head` with DATABASE_URL_DIRECT")
    from jobbuddy.db.models import Base

    Base.metadata.create_all(engine)
