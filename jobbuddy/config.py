"""Configuration: secrets/switches from environment variables, tunables from config/config.yaml."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class FetchConfig(BaseModel):
    user_agent: str = "jobbuddy/0.1 (personal job search)"
    request_delay_seconds: float = 1.5
    max_retries: int = 3
    timeout_seconds: float = 30
    workday_page_size: int = 20
    workday_max_pages: int = 5
    probe_ats_for_email_leads: bool = True  # ingest-alert-email: try public Greenhouse/Lever/Ashby APIs


class MatchingConfig(BaseModel):
    title_keywords_any: list[str] = Field(default_factory=list)
    title_exclude: list[str] = Field(default_factory=list)
    locations_any: list[str] = Field(default_factory=list)
    allow_remote: bool = True
    remote_country_allow: list[str] = Field(default_factory=lambda: ["us", "united states", "usa"])
    min_salary: int | None = None
    max_posting_age_days: int = 30
    required_keywords_any: list[str] = Field(default_factory=list)
    excluded_keywords: list[str] = Field(default_factory=list)
    similarity_threshold: float = 0.30
    agent_score_threshold: int = 70   # on-box agent score (0-100) needed to create an application


class TailoringConfig(BaseModel):
    max_bullets_per_role: int = 6
    min_bullet_overlap: float = 0.35
    output_formats: list[str] = Field(default_factory=lambda: ["docx", "pdf"])


class EngineConfig(BaseModel):
    headless: bool = True
    max_steps: int = 12
    min_confidence_autofill: float = 0.75
    llm_confidence_cap: float = 0.80        # cap for any model-sourced mapping (library path)
    agent_answer_confidence: float = 0.85   # confidence given to answers the on-box agent wrote
    typing_delay_ms: tuple[int, int] = (45, 140)
    action_delay_ms: tuple[int, int] = (350, 1200)
    navigation_timeout_ms: int = 45000
    how_did_you_hear_default: str = "Company careers page"


class PipelineConfig(BaseModel):
    daily_application_cap: int = 5
    max_fills_per_run: int = 3
    run_time_budget_minutes: int = 20
    # run-ci fills (and notifies) ready applications BEFORE fetching, within this slice of the run budget,
    # so a slow fetch can never starve fills; fetch/filter then use whatever budget remains.
    fill_time_budget_minutes: int = 12
    auto_submit: bool = False
    dry_run: bool = False


class AgentConfig(BaseModel):
    """Work-queue settings for the on-box AI agent (agent-pull / agent-push-*)."""

    default_pull_limit: int = 10
    max_description_chars: int = 8000
    max_cover_letter_words: int = 450


class AppConfig(BaseModel):
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    tailoring: TailoringConfig = Field(default_factory=TailoringConfig)
    engine: EngineConfig = Field(default_factory=EngineConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)


class Settings(BaseSettings):
    """Environment variables. Names are the contract with .env.example and the workflow."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    database_url: str = "sqlite:///./jobbuddy.db"
    database_url_direct: str | None = None
    # "sqlalchemy" (default: CI + dashboard, psycopg on port 5432) or "neon_http" (on-box agent:
    # Neon serverless SQL-over-HTTPS, for machines that cannot reach port 5432).
    database_transport: str = "sqlalchemy"
    neon_http_endpoint: str | None = None  # optional override of https://<host>/sql

    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"

    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_allowed_user_id: str | None = None  # defaults to telegram_chat_id (private chat)
    slack_bot_token: str | None = None
    slack_channel_id: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    notify_email_to: str | None = None
    notifier: str | None = None

    candidate_profile_json: str | None = None

    auto_submit: bool = False
    dry_run: bool = False
    daily_application_cap: int | None = None
    headless: bool = True

    dashboard_password: str | None = None

    jobbuddy_config: str | None = None
    jobbuddy_companies: str | None = None

    @property
    def config_path(self) -> Path:
        return Path(self.jobbuddy_config) if self.jobbuddy_config else PROJECT_ROOT / "config" / "config.yaml"

    @property
    def companies_path(self) -> Path:
        return Path(self.jobbuddy_companies) if self.jobbuddy_companies else PROJECT_ROOT / "config" / "companies.yaml"


def load_app_config(path: Path | None = None, settings: Settings | None = None) -> AppConfig:
    settings = settings or get_settings()
    path = path or settings.config_path
    data: dict[str, Any] = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cfg = AppConfig.model_validate(data)
    # Environment switches win over the file, and AUTO_SUBMIT defaults to false.
    cfg.pipeline.auto_submit = bool(settings.auto_submit)
    cfg.pipeline.dry_run = bool(settings.dry_run or cfg.pipeline.dry_run)
    if settings.daily_application_cap is not None:
        cfg.pipeline.daily_application_cap = settings.daily_application_cap
    cfg.engine.headless = bool(settings.headless)
    return cfg


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
