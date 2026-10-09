"""Settings an operator manages in the console, with environment variables as the fallback.

Resolution order for every key: a value saved in the console, then the environment variable named
after the key in capitals (``anthropic_api_key`` -> ``ANTHROPIC_API_KEY``, read through the
application settings first so a ``.env`` file counts), then the default, then nothing.

Values live in the existing ``setting`` table under ``config.<key>`` (no migration). A secret is
stored as a Fernet token under a key derived from ``SECRET_KEY`` and is never returned by the
console: the page only says whether one is configured. Every change is audited in the same
transaction, and the audit row records a secret's state, never its value.

Other code reads settings with ``get(session, "anthropic_api_key")`` (or ``get_int`` /
``get_float``) on every use rather than caching, so a change in the console applies on the next
call. Not managed here, because they are needed before the database can be read or they protect
it: ``DATABASE_URL``, ``SECRET_KEY``, ``ENV`` and ``ADMIN_TRUSTED_ORIGINS``.
"""

from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Any, Literal
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from africasignal import audit
from africasignal.config import get_settings
from africasignal.models import Operator, Setting
from africasignal.operators import derive_key

log = logging.getLogger("africasignal.settings_store")

PREFIX = "config."
Kind = Literal[
    "text", "secret", "url", "https_url", "email", "int", "float", "choice", "model_route"
]
Source = Literal["console", "environment", "default", "unset", "unreadable"]

EMAIL_PROVIDERS = ("postmark", "resend")


class SettingError(ValueError):
    """A value the operator should fix; the message is safe to show."""

    def __init__(self, message: str, *, key: str | None = None) -> None:
        super().__init__(message)
        self.key = key


class SettingsRevisionConflict(ValueError):
    """The settings group changed after the operator loaded it."""

    def __init__(self, expected: int, actual: int) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            "Settings changed since this page loaded. Review the current values and save again."
        )


@dataclass(frozen=True)
class SettingDef:
    key: str
    label: str
    group: str
    kind: Kind
    help: str = ""
    default: str | None = None
    choices: tuple[str, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    # Shown as "not configured" on the Settings page when nothing supplies it outside development.
    expected: bool = False

    @property
    def secret(self) -> bool:
        return self.kind == "secret"

    @property
    def env_name(self) -> str:
        return self.key.upper()


GROUPS: tuple[tuple[str, str], ...] = (
    ("llm", "Language model"),
    ("agent", "Agent Reach runner"),
    ("sources", "Sources and discovery"),
    ("site", "Website"),
    ("email", "Email"),
    ("social_listening", "Social listening"),
    ("social_publishing", "Social publishing"),
    ("storage", "Evidence storage"),
    ("backup", "Backup storage"),
    ("privacy", "Privacy and retention"),
)

_DEFS = (
    SettingDef("anthropic_api_key", "Anthropic API key", "llm", "secret", expected=True),
    SettingDef(
        "llm_evidence_context_enabled",
        "Evidence-grounded explanation context",
        "llm",
        "choice",
        "Default off. Give the explanation model only current, permission-checked claim passages "
        "already linked to that assessment. No new sources are searched or retained.",
        default="no",
        choices=("no", "yes"),
    ),
    SettingDef("openai_api_key", "OpenAI API key", "llm", "secret"),
    SettingDef(
        "agent_reach_endpoint",
        "Agent Reach runner URL",
        "agent",
        "https_url",
        "HTTPS endpoint for the isolated AfricaSignal runner bridge. The upstream Agent Reach "
        "package itself does not expose this API.",
    ),
    SettingDef(
        "agent_reach_api_key",
        "Agent Reach runner token",
        "agent",
        "secret",
        "Scoped bearer token for this AfricaSignal deployment. Stored encrypted and never shown.",
    ),
    SettingDef(
        "agent_reach_max_tasks_per_day",
        "Agent Reach task starts per 24 hours",
        "agent",
        "int",
        "External request count in rolling 24h; failed and cancelled starts also count.",
        default="10",
        minimum=1,
        maximum=100,
    ),
    SettingDef(
        "gdelt_poll_minutes",
        "GDELT poll interval (minutes)",
        "sources",
        "int",
        "GDELT publishes 15-minute windows. Slower polling reduces checks but delays discovery.",
        default="15",
        minimum=15,
        maximum=60,
    ),
    SettingDef(
        "gdelt_windows_per_poll",
        "GDELT windows per job",
        "sources",
        "int",
        "Catch-up is bounded to 1–6 windows per poll to keep work inside the job lease.",
        default="6",
        minimum=1,
        maximum=6,
    ),
    SettingDef(
        "gdelt_max_lookback_hours",
        "GDELT maximum replay lookback (hours)",
        "sources",
        "int",
        "Skips windows outside this bound after outages; lower values reduce recovery coverage.",
        default="24",
        minimum=1,
        maximum=24,
    ),
    SettingDef(
        "llm_route_claim_extract",
        "Claim extraction model route",
        "llm",
        "model_route",
        "Choose a reviewed provider/model and price from config/llm.yaml.",
    ),
    SettingDef(
        "llm_route_explain",
        "Explanation model route",
        "llm",
        "model_route",
        "Choose a reviewed provider/model and price from config/llm.yaml.",
    ),
    SettingDef(
        "llm_route_editorial_draft",
        "Editorial draft model route",
        "llm",
        "model_route",
        "Separate route for private editorial drafts; calls use the same budget and token limits.",
    ),
    SettingDef(
        "editorial_drafting_enabled",
        "Draft-only editorial synthesis",
        "llm",
        "choice",
        "Default off. New qualified assessments may queue private synthesis drafts for operator "
        "review. Approval never publishes, emails, or posts.",
        default="no",
        choices=("no", "yes"),
    ),
    SettingDef(
        "llm_daily_budget_usd",
        "Daily budget (USD)",
        "llm",
        "float",
        "When it is spent, extraction jobs wait for the next day.",
        default="10",
        minimum=0,
        maximum=10_000,
    ),
    SettingDef(
        "llm_per_job_max_tokens",
        "Token limit per job",
        "llm",
        "int",
        default="20000",
        minimum=1_000,
        maximum=1_000_000,
    ),
    SettingDef(
        "public_base_url",
        "Public address",
        "site",
        "https_url",
        "The site's address, for example https://africasignal.example. Used in links in emails and "
        "channel posts. No path.",
        expected=True,
    ),
    SettingDef(
        "operator_name",
        "Operator name",
        "site",
        "text",
        "Who runs the site, as the privacy notice and terms should name them: a person or a "
        "registered company.",
        expected=True,
    ),
    SettingDef(
        "contact_email",
        "Contact address",
        "site",
        "email",
        "Where readers send privacy requests, corrections and takedown requests. Shown on the "
        "privacy notice, the terms and the correction policy, and on the crawler page unless "
        "BOT_CONTACT_EMAIL is set.",
        expected=True,
    ),
    SettingDef(
        "legal_review_confirmed",
        "Legal pages reviewed",
        "site",
        "choice",
        "Choose yes only after a lawyer has reviewed the privacy notice, the terms and the "
        "correction policy. Until then each page carries a banner saying it is a draft.",
        default="no",
        choices=("no", "yes"),
    ),
    SettingDef(
        "trusted_proxy_hops",
        "Proxies in front of the site",
        "site",
        "int",
        "How many reverse proxies or CDNs sit between readers and this app, each adding the "
        "address it saw to X-Forwarded-For. 0 (the default) uses the connecting address, which is "
        "right with no proxy. Set it so the rate limits see readers, not the proxy. Leave 0 when "
        "the proxy runs on the same machine and uvicorn already handles it.",
        default="0",
        minimum=0,
        maximum=5,
    ),
    SettingDef(
        "email_provider",
        "Email provider",
        "email",
        "choice",
        choices=EMAIL_PROVIDERS,
        expected=True,
    ),
    SettingDef("email_api_key", "Email provider API key", "email", "secret", expected=True),
    SettingDef(
        "email_from",
        "Sender address",
        "email",
        "email",
        "Must belong to a domain the provider has verified (SPF and DKIM).",
        expected=True,
    ),
    SettingDef(
        "x_user_access_token",
        "X user access token",
        "social_publishing",
        "secret",
        "OAuth 2.0 user-context token with tweet.write permission for the AfricaSignal X account. "
        "Stored encrypted and never shown.",
        expected=True,
    ),
    SettingDef(
        "x_publishing_enabled",
        "X publishing",
        "social_publishing",
        "choice",
        "The per-post operator approval remains required.",
        default="yes",
        choices=("no", "yes"),
    ),
    SettingDef(
        "facebook_publishing_enabled",
        "Facebook publishing",
        "social_publishing",
        "choice",
        "Off by default; enable after the Page credentials and permissions are verified.",
        default="no",
        choices=("no", "yes"),
    ),
    SettingDef(
        "instagram_publishing_enabled",
        "Instagram publishing",
        "social_publishing",
        "Off by default; enable after professional-account media publishing is verified.",
        default="no",
        choices=("no", "yes"),
    ),
    SettingDef(
        "telegram_publishing_enabled",
        "Telegram publishing",
        "social_publishing",
        "choice",
        "Off by default; enable after the bot's channel permissions are verified.",
        default="no",
        choices=("no", "yes"),
    ),
    SettingDef(
        "youtube_publishing_enabled",
        "YouTube publishing",
        "social_publishing",
        "choice",
        "Off by default; enable after OAuth channel identity and API project review.",
        default="no",
        choices=("no", "yes"),
    ),
    SettingDef(
        "social_media_retention_days",
        "Unresolved social media retention (days)",
        "social_publishing",
        "int",
        "Successful uploads are deleted immediately; unresolved media expires after the configured period.",
        default="30",
        minimum=1,
        maximum=90,
    ),
    SettingDef(
        "x_daily_post_limit",
        "X posts per Lagos day",
        "social_publishing",
        "int",
        "Hard cap across operator-approved posts. No scheduled or automatic posts are sent.",
        default="5",
        minimum=1,
        maximum=50,
    ),
    SettingDef("facebook_page_id", "Facebook Page ID", "social_publishing", "text"),
    SettingDef(
        "facebook_page_access_token",
        "Facebook Page access token",
        "social_publishing",
        "secret",
        "Encrypted Page token with the approved Page publishing permission.",
    ),
    SettingDef(
        "instagram_professional_account_id",
        "Instagram professional account ID",
        "social_publishing",
        "text",
    ),
    SettingDef(
        "instagram_access_token",
        "Instagram access token",
        "social_publishing",
        "secret",
        "Encrypted token for an eligible Instagram professional account with content "
        "publishing access.",
    ),
    SettingDef(
        "meta_graph_api_version",
        "Meta Graph API version",
        "social_publishing",
        "text",
        "Version segment used by Facebook and Instagram Graph calls, for example v26.0.",
    ),
    SettingDef(
        "telegram_channel_id", "Telegram channel username or ID", "social_publishing", "text"
    ),
    SettingDef(
        "telegram_bot_token",
        "Telegram bot token",
        "social_publishing",
        "secret",
        "Encrypted bot token. Add the bot as an administrator of the destination channel.",
    ),
    SettingDef("youtube_channel_id", "Expected YouTube channel ID", "social_publishing", "text"),
    SettingDef("youtube_oauth_client_id", "YouTube OAuth client ID", "social_publishing", "text"),
    SettingDef(
        "youtube_oauth_client_secret", "YouTube OAuth client secret", "social_publishing", "secret"
    ),
    SettingDef(
        "youtube_refresh_token", "YouTube OAuth refresh token", "social_publishing", "secret"
    ),
    SettingDef(
        "youtube_video_privacy",
        "YouTube upload visibility",
        "social_publishing",
        "choice",
        "Initial visibility for operator-approved video uploads. Unlisted is the safe default.",
        default="unlisted",
        choices=("private", "unlisted", "public"),
    ),
    SettingDef(
        "youtube_category_id",
        "YouTube video category ID",
        "social_publishing",
        "text",
        "YouTube Data API category identifier used for video metadata.",
        default="25",
    ),
    SettingDef(
        "facebook_daily_post_limit",
        "Facebook posts per Lagos day",
        "social_publishing",
        "int",
        default="5",
        minimum=1,
        maximum=50,
    ),
    SettingDef(
        "instagram_daily_post_limit",
        "Instagram posts per Lagos day",
        "social_publishing",
        "int",
        default="3",
        minimum=1,
        maximum=25,
    ),
    SettingDef(
        "telegram_daily_post_limit",
        "Telegram posts per Lagos day",
        "social_publishing",
        "int",
        default="10",
        minimum=1,
        maximum=100,
    ),
    SettingDef(
        "youtube_daily_post_limit",
        "YouTube uploads per Lagos day",
        "social_publishing",
        "int",
        default="2",
        minimum=1,
        maximum=10,
    ),
    SettingDef(
        "x_app_bearer_token",
        "X app bearer token",
        "social_listening",
        "secret",
        "Read-only app credential for X Recent Search. Stored encrypted and never shown.",
    ),
    SettingDef(
        "x_listening_enabled",
        "X listening",
        "social_listening",
        "choice",
        "Off by default. Enabling performs billable recent-search reads for active watch queries.",
        default="no",
        choices=("no", "yes"),
    ),
    SettingDef(
        "x_listening_poll_minutes",
        "X listening interval (minutes)",
        "social_listening",
        "int",
        "Minimum interval between scheduled searches per active watch query.",
        default="360",
        minimum=60,
        maximum=1440,
    ),
    SettingDef(
        "x_listening_daily_read_cap",
        "X posts reserved per Lagos day",
        "social_listening",
        "int",
        "Maximum result resources reserved across searches per Lagos day. "
        "Unknown outcomes keep the full reservation.",
        default="100",
        minimum=10,
        maximum=5000,
    ),
    SettingDef(
        "weekly_digest_weekday",
        "Weekly digest weekday (0 Monday-6 Sunday)",
        "email",
        "int",
        "Weekly digest schedule in Africa/Lagos time.",
        default="0",
        minimum=0,
        maximum=6,
    ),
    SettingDef(
        "weekly_digest_hour",
        "Weekly digest local hour",
        "email",
        "int",
        "Hour from 0 to 23 in Africa/Lagos. Queueing begins at this hour on the selected weekday.",
        default="7",
        minimum=0,
        maximum=23,
    ),
    SettingDef(
        "s3_endpoint_url",
        "Endpoint URL",
        "storage",
        "url",
        "Leave empty for AWS S3; set it for R2, B2 or MinIO.",
    ),
    SettingDef("s3_bucket", "Bucket", "storage", "text", expected=True),
    SettingDef("s3_access_key_id", "Access key ID", "storage", "secret", expected=True),
    SettingDef("s3_secret_access_key", "Secret access key", "storage", "secret", expected=True),
    SettingDef(
        "backup_s3_endpoint_url",
        "Endpoint URL",
        "backup",
        "url",
        "Where nightly database dumps go. Use a different account from evidence storage.",
    ),
    SettingDef("backup_s3_bucket", "Bucket", "backup", "text"),
    SettingDef("backup_s3_access_key_id", "Access key ID", "backup", "secret"),
    SettingDef("backup_s3_secret_access_key", "Secret access key", "backup", "secret"),
    SettingDef("backup_s3_prefix", "Key prefix", "backup", "text", "Default: africasignal/<ENV>."),
    SettingDef(
        "backup_retain_days",
        "Days to keep dumps",
        "backup",
        "int",
        default="30",
        minimum=1,
        maximum=3650,
    ),
    SettingDef(
        "feedback_retention_months",
        "Months to keep feedback text",
        "privacy",
        "int",
        "After this many months a daily job removes the text, typed contact email, visitor code "
        "and account link from feedback and error reports. The row stays, so vote counts and "
        "what was corrected are kept. The privacy notice shows this number.",
        default="24",
        minimum=1,
        maximum=120,
    ),
    SettingDef(
        "backup_max_age_hours",
        "Alert when the last backup is older than (hours)",
        "backup",
        "int",
        "A nightly backup plus a margin. Older than this raises an alert in the audit log.",
        default="36",
        minimum=1,
        maximum=720,
    ),
)

REGISTRY: dict[str, SettingDef] = {d.key: d for d in _DEFS}


def definition(key: str) -> SettingDef:
    try:
        return REGISTRY[key]
    except KeyError:
        raise KeyError(f"unknown setting {key!r}") from None


def options(key: str) -> list[tuple[str, str]]:
    """Selectable values for choice fields and the reviewed model catalogue."""
    defn = definition(key)
    if defn.kind == "choice":
        return [(value, value.replace("_", " ").title()) for value in defn.choices]
    if defn.kind == "model_route":
        from africasignal.llm.config import load_llm_config

        return load_llm_config().route_options()
    return []


def group_keys(group: str) -> list[str]:
    return [d.key for d in _DEFS if d.group == group]


def group_revision(session: Session, group: str) -> int:
    """Return the revision for a console settings group (1 before its first edit)."""
    if group not in dict(GROUPS):
        raise SettingError("unknown settings group")
    value = session.scalar(
        select(Setting.value).where(Setting.key == f"control_revision.settings.{group}")
    )
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 1


def _lock_group_revision(session: Session, group: str) -> Setting:
    """Serialize edits to one group, including its first update before a row exists."""
    revision_key = f"control_revision.settings.{group}"
    session.execute(
        select(func.pg_advisory_xact_lock(func.hashtext(f"africasignal.settings.{group}")))
    )
    session.execute(
        insert(Setting)
        .values(key=revision_key, value=1)
        .on_conflict_do_nothing(index_elements=[Setting.key])
    )
    row = session.scalar(select(Setting).where(Setting.key == revision_key).with_for_update())
    if row is None:
        raise RuntimeError("settings revision row could not be locked")
    return row


def apply_group_changes(
    session: Session,
    operator: Operator,
    group: str,
    expected_revision: int,
    changes: dict[str, str | None],
) -> tuple[int, int]:
    """Apply one validated settings group using optimistic concurrency control.

    Returns (number of changed settings, resulting group revision). Revision and setting rows
    commit or roll back together with their audit records in the request transaction.
    """
    allowed = set(group_keys(group))
    if not allowed:
        raise SettingError("unknown settings group")
    if any(key not in allowed for key in changes):
        raise SettingError("a setting does not belong to this group")
    if expected_revision < 1:
        raise SettingError("reload the settings page before saving")

    revision_row = _lock_group_revision(session, group)
    actual_revision = revision_row.value if isinstance(revision_row.value, int) else 1
    if actual_revision != expected_revision:
        raise SettingsRevisionConflict(expected_revision, actual_revision)
    changed = apply_changes(session, operator, changes, bump_group_revisions=False)
    if changed:
        revision_row.value = actual_revision + 1
        session.flush()
    return changed, revision_row.value


# --- storage ------------------------------------------------------------------------------------


def _fernet() -> Fernet:
    import base64

    return Fernet(base64.urlsafe_b64encode(derive_key("console-settings")))


def _row_key(key: str) -> str:
    return PREFIX + key


def _read_row(session: Session, key: str) -> tuple[str | None, bool, bool]:
    """(plain value, has a stored row, stored row is unreadable)."""
    # A column query, not session.get(), so a value written earlier in this transaction is seen.
    found = session.execute(select(Setting.value).where(Setting.key == _row_key(key))).first()
    if found is None:
        return None, False, False
    payload: Any = found[0]
    if isinstance(payload, dict) and "enc" in payload:
        try:
            return _fernet().decrypt(str(payload["enc"]).encode()).decode(), True, False
        except InvalidToken:
            log.warning("setting %s is stored but cannot be decrypted (SECRET_KEY changed?)", key)
            return None, True, True
    if isinstance(payload, dict) and "v" in payload:
        return str(payload["v"]), True, False
    return None, True, True


def _write_row(session: Session, key: str, value: str, secret: bool) -> None:
    payload = {"enc": _fernet().encrypt(value.encode()).decode()} if secret else {"v": value}
    stmt = insert(Setting).values(key=_row_key(key), value=payload)
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[Setting.key],
            set_={"value": payload, "updated_at": stmt.excluded.updated_at},
        )
    )


def _environment(defn: SettingDef) -> str | None:
    settings = get_settings()
    # Only a field that the environment or ``.env`` actually set counts, not a field's own default.
    value = getattr(settings, defn.key) if defn.key in settings.model_fields_set else None
    if value is None:
        value = os.environ.get(defn.env_name)
    return str(value) if value not in (None, "") else None


@dataclass(frozen=True)
class Resolved:
    value: str | None
    source: Source


def resolve(session: Session, key: str) -> Resolved:
    """The effective value of a setting and where it came from."""
    defn = definition(key)
    stored, has_row, unreadable = _read_row(session, key)
    if stored not in (None, ""):
        return Resolved(stored, "console")
    env = _environment(defn)
    if env is not None:
        # Development-only email doubles remain available to local tests.
        if not (
            key == "email_provider"
            and get_settings().env == "development"
            and env.strip().lower() in ("fake", "console")
        ):
            env = normalise(defn, env.strip())
        return Resolved(env, "environment")
    if defn.default is not None:
        return Resolved(defn.default, "default")
    return Resolved(None, "unreadable" if unreadable else "unset")


def get(session: Session, key: str) -> str | None:
    """The effective value: console, else environment, else default, else None."""
    return resolve(session, key).value


def get_int(session: Session, key: str) -> int | None:
    value = get(session, key)
    return int(float(value)) if value is not None else None


def get_float(session: Session, key: str) -> float | None:
    value = get(session, key)
    return float(value) if value is not None else None


@dataclass(frozen=True)
class StorageConfig:
    endpoint_url: str
    bucket: str
    access_key_id: str
    secret_access_key: str


def storage_config(session: Session) -> StorageConfig:
    """Evidence object storage, as read by ``storage.get_store()``."""
    return StorageConfig(
        endpoint_url=get(session, "s3_endpoint_url") or "",
        bucket=get(session, "s3_bucket") or "",
        access_key_id=get(session, "s3_access_key_id") or "",
        secret_access_key=get(session, "s3_secret_access_key") or "",
    )


# --- validation ---------------------------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_BUCKET = re.compile(r"[a-z0-9][a-z0-9._-]{1,62}")


def _url(raw: str, *, https_only: bool, bare: bool) -> str:
    parts = urlsplit(raw)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise SettingError("enter a web address that starts with http:// or https://")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise SettingError("the address must not contain a login, query or #fragment")
    if bare and parts.path not in ("", "/"):
        raise SettingError("give the site address only, with no path")
    if https_only and parts.scheme.lower() != "https" and get_settings().env != "development":
        raise SettingError("the public address must use https://")
    if len(raw) > 500:
        raise SettingError("that address is too long")
    return raw.rstrip("/")


def normalise(defn: SettingDef, raw: str) -> str:
    """The value to store, or ``SettingError``. ``raw`` is non-empty and stripped."""
    if _CONTROL.search(raw):
        raise SettingError(f"{defn.label}: control characters are not allowed")
    if defn.kind == "secret":
        if len(raw) < 8 or len(raw) > 500 or re.search(r"\s", raw):
            raise SettingError(f"{defn.label}: a key is 8 to 500 characters with no spaces")
        return raw
    if defn.kind in ("url", "https_url"):
        try:
            return _url(raw, https_only=defn.kind == "https_url", bare=defn.kind == "https_url")
        except SettingError as exc:
            raise SettingError(f"{defn.label}: {exc}") from None
    if defn.kind == "email":
        address = parseaddr(raw)[1]
        if not re.fullmatch(r"[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+", address) or len(raw) > 254:
            raise SettingError(f"{defn.label}: enter an email address")
        return raw
    if defn.kind == "choice":
        if raw not in defn.choices:
            raise SettingError(f"{defn.label}: choose one of {', '.join(defn.choices)}")
        return raw
    if defn.kind == "model_route":
        from africasignal.llm.config import load_llm_config

        allowed = dict(load_llm_config().route_options())
        if raw not in allowed:
            raise SettingError(f"{defn.label}: choose a model route with a reviewed price")
        return raw
    if defn.kind in ("int", "float"):
        try:
            number = float(raw) if defn.kind == "float" else int(raw)
        except ValueError:
            raise SettingError(f"{defn.label}: enter a number") from None
        if not math.isfinite(number):
            raise SettingError(f"{defn.label}: enter a finite number")
        if defn.minimum is not None and number < defn.minimum:
            raise SettingError(f"{defn.label}: must be at least {defn.minimum:g}")
        if defn.maximum is not None and number > defn.maximum:
            raise SettingError(f"{defn.label}: must be at most {defn.maximum:g}")
        return raw
    # text
    if defn.key == "meta_graph_api_version" and not re.fullmatch(r"v[0-9]{1,2}\.[0-9]", raw):
        raise SettingError("Meta Graph API version must look like v26.0")
    if (
        defn.key in {"facebook_page_id", "instagram_professional_account_id"}
        and not re.fullmatch(r"[0-9]{5,30}", raw)
    ):
        raise SettingError(f"{defn.label}: enter the numeric account ID")
    if defn.key == "youtube_channel_id" and not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", raw):
        raise SettingError("YouTube channel ID must be the standard UC-prefixed channel ID")
    if defn.key == "youtube_category_id" and not re.fullmatch(r"[0-9]{1,4}", raw):
        raise SettingError("YouTube category ID must contain 1 to 4 digits")
    if (
        defn.key == "telegram_channel_id"
        and not re.fullmatch(r"@[A-Za-z0-9_]{5,32}|-?[0-9]{5,20}", raw)
    ):
        raise SettingError("Telegram destination must be a channel @username or numeric chat ID")
    if len(raw) > 500:
        raise SettingError(f"{defn.label}: at most 500 characters")
    if defn.key.endswith("_bucket") and not _BUCKET.fullmatch(raw):
        raise SettingError(f"{defn.label}: lower-case letters, digits, dots, dashes; 2 to 63 long")
    if defn.key.endswith("_prefix") and (raw.startswith("/") or ".." in raw.split("/")):
        raise SettingError(f"{defn.label}: a relative path without ..")
    return raw


# --- changes ------------------------------------------------------------------------------------


def _audit_state(defn: SettingDef, value: str | None, present: bool) -> dict[str, Any]:
    if defn.secret:
        return {"key": defn.key, "configured": present}
    return {"key": defn.key, "value": value}


def set_value(session: Session, operator: Operator, key: str, raw: str) -> None:
    """Save one value in the console and audit it. ``SettingError`` when it is invalid."""
    apply_changes(session, operator, {key: raw})


def clear_value(session: Session, operator: Operator, key: str) -> None:
    """Remove the console value so the environment variable or default applies again."""
    apply_changes(session, operator, {key: None})


def apply_changes(
    session: Session,
    operator: Operator,
    changes: dict[str, str | None],
    *,
    bump_group_revisions: bool = True,
) -> int:
    """Apply ``key -> new value`` (``None`` clears). All values are validated before any is
    written, so one bad field changes nothing. Returns how many settings actually changed."""
    prepared: list[tuple[SettingDef, str | None]] = []
    for key, raw in changes.items():
        defn = definition(key)
        try:
            value = None if raw is None else normalise(defn, raw.strip())
        except SettingError as exc:
            raise SettingError(str(exc), key=key) from exc
        prepared.append((defn, value))
    revision_rows = {}
    if bump_group_revisions:
        groups = sorted({defn.group for defn, _value in prepared})
        revision_rows = {group: _lock_group_revision(session, group) for group in groups}
    changed = 0
    changed_groups: set[str] = set()
    for defn, value in prepared:
        before_value, had_row, _ = _read_row(session, defn.key)
        if value is None:
            if not had_row:
                continue
            session.execute(delete(Setting).where(Setting.key == _row_key(defn.key)))
            audit.record(
                session,
                operator,
                "setting.clear",
                f"setting:{defn.key}",
                None,
                before=_audit_state(defn, before_value, True),
                after=_audit_state(defn, None, False),
            )
            changed += 1
            changed_groups.add(defn.group)
            continue
        if had_row and before_value == value:
            continue
        _write_row(session, defn.key, value, defn.secret)
        audit.record(
            session,
            operator,
            "setting.set",
            f"setting:{defn.key}",
            None,
            before=_audit_state(defn, before_value, had_row),
            after=_audit_state(defn, value, True),
        )
        changed += 1
        changed_groups.add(defn.group)
    for group in changed_groups:
        if bump_group_revisions:
            row = revision_rows[group]
            row.value = (row.value if isinstance(row.value, int) else 1) + 1
    session.flush()
    return changed


def missing_expected(session: Session) -> list[SettingDef]:
    """Settings the app needs outside development that nothing supplies yet."""
    if get_settings().env == "development":
        return []
    return [d for d in _DEFS if d.expected and get(session, d.key) is None]
