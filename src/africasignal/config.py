"""Application settings, read from environment variables.

In staging and production the settings the app needs to start (the database and the secret key)
must be present; a missing one raises ``RuntimeError`` at startup rather than failing later at
first use. Everything else (Anthropic, email, storage, backups, the public address) can instead be
saved in the operator console, so it is not required at startup: the console's Settings page lists
what is still missing, and ``settings_store`` resolves each value (console, then environment).
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Settings that must be set in the environment outside development: the database (needed to read
# anything else) and the secret key (which encrypts the secrets saved in the console).
REQUIRED_OUTSIDE_DEVELOPMENT: tuple[str, ...] = ("database_url", "secret_key")

DEV_DATABASE_URL = "postgresql+psycopg://africasignal:africasignal@localhost:5432/africasignal"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    env: Literal["development", "staging", "production"] = Field(default="development", alias="ENV")

    database_url: str = Field(default="", alias="DATABASE_URL")

    s3_endpoint_url: str = Field(default="", alias="S3_ENDPOINT_URL")
    s3_bucket: str = Field(default="", alias="S3_BUCKET")
    s3_access_key_id: str = Field(default="", alias="S3_ACCESS_KEY_ID")
    s3_secret_access_key: str = Field(default="", alias="S3_SECRET_ACCESS_KEY")

    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    agent_reach_deny: bool = Field(default=False, alias="AGENT_REACH_DENY")
    external_agents_deny: bool = Field(default=False, alias="EXTERNAL_AGENTS_DENY")
    commercial_deny: bool = Field(default=True, alias="COMMERCIAL_DENY")
    agent_reach_endpoint: str = Field(default="", alias="AGENT_REACH_ENDPOINT")
    agent_reach_api_key: str = Field(default="", alias="AGENT_REACH_API_KEY")
    agent_reach_max_tasks_per_day: int = Field(
        default=10, ge=1, le=100, alias="AGENT_REACH_MAX_TASKS_PER_DAY"
    )
    gdelt_poll_minutes: int = Field(default=15, ge=15, le=60, alias="GDELT_POLL_MINUTES")
    gdelt_windows_per_poll: int = Field(default=6, ge=1, le=6, alias="GDELT_WINDOWS_PER_POLL")
    gdelt_max_lookback_hours: int = Field(default=24, ge=1, le=24, alias="GDELT_MAX_LOOKBACK_HOURS")
    llm_daily_budget_usd: float = Field(default=10.0, alias="LLM_DAILY_BUDGET_USD")
    llm_per_job_max_tokens: int = Field(default=20000, alias="LLM_PER_JOB_MAX_TOKENS")

    email_provider: str = Field(default="", alias="EMAIL_PROVIDER")
    email_api_key: str = Field(default="", alias="EMAIL_API_KEY")
    email_from: str = Field(default="", alias="EMAIL_FROM")
    weekly_digest_weekday: int = Field(default=0, ge=0, le=6, alias="WEEKLY_DIGEST_WEEKDAY")
    weekly_digest_hour: int = Field(default=7, ge=0, le=23, alias="WEEKLY_DIGEST_HOUR")
    # In staging every outbound message is refused unless the normalized address appears here.
    staging_email_allowed_recipients: str = Field(
        default="", alias="STAGING_EMAIL_ALLOWED_RECIPIENTS"
    )

    secret_key: str = Field(default="", alias="SECRET_KEY")

    # Origin used in links inside emails (sign-in, unsubscribe, situation pages).
    public_base_url: str = Field(default="http://localhost:8000", alias="PUBLIC_BASE_URL")

    def validate_required(self) -> None:
        """Raise ``RuntimeError`` naming every missing required setting (non-development only)."""
        if self.env == "development":
            return
        missing = [name.upper() for name in REQUIRED_OUTSIDE_DEVELOPMENT if not getattr(self, name)]
        if missing:
            raise RuntimeError(
                f"Missing required settings for ENV={self.env}: {', '.join(missing)}"
            )

        key = self.secret_key.strip()
        if (
            len(key) < 32
            or len(set(key)) < 8
            or key.lower()
            in {
                "africasignal-development-only-secret-key",
                "change-me-change-me-change-me-change-me",
            }
        ):
            raise RuntimeError(
                "SECRET_KEY must be a randomly generated secret of at least 32 characters"
            )

    @property
    def staging_email_recipients(self) -> frozenset[str]:
        return frozenset(
            address.strip().casefold()
            for address in self.staging_email_allowed_recipients.split(",")
            if address.strip()
        )

    @property
    def effective_database_url(self) -> str:
        return self.database_url or DEV_DATABASE_URL


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_required()
    return settings


def config_dir() -> Path:
    """Directory holding the YAML seed files (``sources.yaml``, ``place_aliases.yaml``, ...).

    ``CONFIG_DIR`` overrides the default of ``config`` in the working directory (``/app`` in the
    container, the repository root in development).
    """
    return Path(os.environ.get("CONFIG_DIR", "config"))
