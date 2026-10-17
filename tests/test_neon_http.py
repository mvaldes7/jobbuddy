"""Neon SQL-over-HTTP client against a mocked endpoint (no network)."""
import json

import httpx
import pytest

from jobbuddy.db.neon_http import (NeonHttpError, NeonHttpTransport, decode_bytea, encode_param, endpoint_for,
                                   to_connection_string)
from jobbuddy.db.transport import SqlAlchemyTransport, as_bool, as_json, build_transport
from tests.conftest import NEON_TEST_URL


def test_connection_string_and_endpoint():
    cs = to_connection_string(NEON_TEST_URL)
    assert cs.startswith("postgresql://agent:") and "+psycopg" not in cs
    assert cs.endswith("sslmode=require&channel_binding=require")
    assert endpoint_for(cs) == "https://ep-test-123456.us-east-2.aws.neon.tech/sql"
    assert endpoint_for(cs, "https://api.us-east-2.aws.neon.tech/sql").startswith("https://api.")
    with pytest.raises(ValueError):
        to_connection_string("sqlite:///x.db")


def test_param_encoding_matches_js_driver():
    assert encode_param(None) is None
    assert encode_param(True) == "true" and encode_param(False) == "false"
    assert encode_param(b"\x00\xff") == "\\x00ff" and decode_bytea("\\x00ff") == b"\x00\xff"
    assert encode_param({"a": [1]}) == '{"a": [1]}' and encode_param(7) == "7"
    assert as_bool("t") is True and as_bool("f") is False and as_json('{"x": 1}') == {"x": 1}


def test_single_query_and_batch_protocol():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append((dict(req.headers), body))
        if "queries" in body:
            return httpx.Response(200, json={"results": [{"command": "UPDATE", "rowCount": 1, "fields": [], "rows": []},
                                                         {"command": "SELECT", "rowCount": 1,
                                                          "fields": [{"name": "n"}], "rows": [["2"]]}]})
        return httpx.Response(200, json={"command": "SELECT", "rowCount": 1, "fields": [{"name": "id"}, {"name": "ok"}],
                                         "rows": [["1", "t"]]})

    t = NeonHttpTransport(NEON_TEST_URL, client=httpx.Client(transport=httpx.MockTransport(handler)))
    r = t.query("SELECT id, ok FROM jobs WHERE id = $1", [1])
    assert r.dicts() == [{"id": "1", "ok": "t"}]
    h, body = seen[0]
    assert h["neon-raw-text-output"] == "true" and h["neon-array-mode"] == "true"
    assert body == {"query": "SELECT id, ok FROM jobs WHERE id = $1", "params": ["1"]}
    res = t.transaction([("UPDATE jobs SET status = $2 WHERE id = $1", [1, "scored"]), ("SELECT 2 AS n", [])])
    assert [x.row_count for x in res] == [1, 1] and res[1].rows == [["2"]]
    h2, body2 = seen[1]
    assert body2["queries"][0]["params"] == ["1", "scored"]
    assert h2["neon-batch-isolation-level"] == "ReadCommitted" and h2["neon-batch-read-only"] == "false"
    assert "not-a-real-password" not in repr(t)


def test_errors_are_redacted_and_5xx_retried():
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(400, json={"message": "permission denied for table x, conn "
                                                    "postgresql://agent:secret@host/db", "code": "42501"})

    t = NeonHttpTransport(NEON_TEST_URL, client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(NeonHttpError) as ei:
        t.query("CREATE TABLE nope (id int)")
    assert calls["n"] == 2 and ei.value.code == "42501" and "secret" not in str(ei.value)


def test_mocked_endpoint_runs_against_test_db(neon):
    r = neon.query("SELECT COUNT(*) AS n FROM jobs WHERE status = $1", ["found"])
    assert r.rows == [["0"]]
    assert neon.mock.requests[-1]["url"].endswith("/sql")


def test_build_transport_selection(settings):
    assert isinstance(build_transport(settings), SqlAlchemyTransport)
    s2 = settings.model_copy(update={"database_transport": "neon_http", "database_url": NEON_TEST_URL})
    assert isinstance(build_transport(s2), NeonHttpTransport)
    with pytest.raises(ValueError):
        build_transport(settings.model_copy(update={"database_transport": "carrier_pigeon"}))


def test_sqlalchemy_transport_uses_same_dollar_sql(db_url):
    t = SqlAlchemyTransport(db_url)
    assert t.query("SELECT $1 AS a, $2 AS b", [3, {"k": 1}]).dicts() == [{"a": 3, "b": '{"k": 1}'}]
