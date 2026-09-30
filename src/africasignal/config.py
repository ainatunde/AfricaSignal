"""Application settings, read from environment variables.

In staging and production every required secret must be present; a missing one raises
``RuntimeError`` at startup rather than failing later at first use.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Settings that must be set outside development. ``S3_*`` and ``EMAIL_*`` are expanded to the
# concrete field names below.
REQUIRED_OUTSIDE_DEVELOPMENT: tuple[str, ...] = (
    "database_url",
    "s3_endpoint_url",
    "s3_bucket",
    "s3_access_key_id",
    "s3_secret_access_key",
    "anthropic_api_key",
    "email_provider",
    "email_api_key",
    "email_from",
    "secret_key",
)

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
