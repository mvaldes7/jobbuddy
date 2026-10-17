from typer.testing import CliRunner

from jobbuddy.cli import app
from tests.conftest import ROOT


def test_cli_report_and_check_profile(monkeypatch, db_url):
    from jobbuddy.config import reset_settings_cache

    monkeypatch.setenv("DATABASE_URL", db_url)
    reset_settings_cache()
    r = CliRunner().invoke(app, ["report"])
    assert r.exit_code == 0, r.output
    assert '"submitted_total": 0' in r.output
    import jobbuddy.profile as prof

    monkeypatch.setattr(prof, "_default_profile_path", lambda: ROOT / "profile" / "profile.example.yaml")
    r2 = CliRunner().invoke(app, ["check-profile"])
    assert "TODO: application_answers.salary_floor" in r2.output
    reset_settings_cache()


def test_submit_requires_confirm(monkeypatch, db_url):
    from jobbuddy.config import reset_settings_cache
    import jobbuddy.profile as prof

    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setattr(prof, "_default_profile_path", lambda: ROOT / "profile" / "profile.example.yaml")
    reset_settings_cache()
    r = CliRunner().invoke(app, ["submit"])
    assert r.exit_code == 2 and "--confirm" in r.output
    reset_settings_cache()


def test_init_dev_db_refuses_non_sqlite(monkeypatch):
    from jobbuddy.config import reset_settings_cache

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:1/db")
    reset_settings_cache()
    r = CliRunner().invoke(app, ["init-dev-db"])
    assert r.exit_code != 0 and "SQLite-only" in str(r.exception)
    reset_settings_cache()
