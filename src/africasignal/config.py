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
    llm_daily_budget_usd: float = Field(default=10.0, alias="LLM_DAILY_BUDGET_USD")
    llm_per_job_max_tokens: int = Field(default=20000, alias="LLM_PER_JOB_MAX_TOKENS")

    email_provider: str = Field(default="", alias="EMAIL_PROVIDER")
    email_api_key: str = Field(default="", alias="EMAIL_API_KEY")
    email_from: str = Field(default="", alias="EMAIL_FROM")

    secret_key: str = Field(default="", alias="SECRET_KEY")

    def validate_required(self) -> None:
        """Raise ``RuntimeError`` naming every missing required setting (non-development only)."""
        if self.env == "development":
            return
        missing = [name.upper() for name in REQUIRED_OUTSIDE_DEVELOPMENT if not getattr(self, name)]
        if missing:
            raise RuntimeError(
                f"Missing required settings for ENV={self.env}: {', '.join(missing)}"
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
