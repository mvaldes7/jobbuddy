import json
import logging

from jobbuddy.logging_utils import PIIRedactingFilter
from jobbuddy.profile import is_todo, load_profile, profile_to_json
from tests.conftest import ROOT


def test_example_profile_loads_and_lists_todos(profile):
    assert profile.personal.first_name == "Alex"
    todos = profile.todo_fields()
    assert "application_answers.salary_floor" in todos
    assert "application_answers.work_authorization" in todos
    assert any(t.startswith("targets.titles") for t in todos)
    assert any(t.startswith("application_answers.eeo") for t in todos)


def test_real_profile_has_required_todos_and_facts():
    path = ROOT / "profile" / "profile.yaml"
    if not path.exists():  # PII file is excluded from the distributed zip
        import pytest
        pytest.skip("profile.yaml not present")
    p = load_profile(path)
    assert p.personal.email.endswith("@gmail.com")
    corpus = p.fact_corpus()
    for fact in ("66%", "85%", "30%", "3 million", "21 CFR Part 11", "KNIME", "Towson University"):
        assert fact in corpus
    todos = p.todo_fields()
    for key in ("application_answers.work_authorization", "application_answers.requires_sponsorship",
                "application_answers.salary_floor", "application_answers.start_date",
                "application_answers.willing_to_relocate"):
        assert key in todos
    assert p.targets.locations == ["San Francisco Bay Area", "Remote (US)"]


def test_candidate_profile_json_env_overrides_file(profile):
    blob = profile_to_json(profile)
    p2 = load_profile(env={"CANDIDATE_PROFILE_JSON": blob})
    assert p2.personal.email == profile.personal.email
    assert json.loads(blob)["personal"]["first_name"] == "Alex"


def test_is_todo():
    assert is_todo("TODO: fill me")
    assert not is_todo("Decline to answer")
    assert not is_todo(None)


def test_pii_redaction_filter(profile):
    f = PIIRedactingFilter(profile.pii_literals())
    rec = logging.LogRecord("x", logging.INFO, __file__, 1,
                            "applied as Alex Q. Example alex.example@example.com 555-010-0199 "
                            "postgresql+psycopg://user:secret@host/db token sk-abcdefghijklmnopqrstu", None, None)
    f.filter(rec)
    msg = rec.getMessage()
    assert "alex.example@example.com" not in msg
    assert "555-010-0199" not in msg
    assert "Alex Q. Example" not in msg
    assert "secret@" not in msg
    assert "sk-abcdefghijklmnop" not in msg
