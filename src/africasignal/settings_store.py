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
import os
import re
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Any, Literal
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from africasignal import audit
from africasignal.config import get_settings
from africasignal.models import Operator, Setting
from africasignal.operators import derive_key

log = logging.getLogger("africasignal.settings_store")

PREFIX = "config."
Kind = Literal["text", "secret", "url", "https_url", "email", "int", "float", "choice"]
Source = Literal["console", "environment", "default", "unset", "unreadable"]

EMAIL_PROVIDERS = ("postmark", "resend")


class SettingError(ValueError):
    """A value the operator should fix; the message is safe to show."""


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
    ("site", "Website"),
    ("email", "Email"),
    ("storage", "Evidence storage"),
    ("backup", "Backup storage"),
)

_DEFS = (
    SettingDef("anthropic_api_key", "Anthropic API key", "llm", "secret", expected=True),
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
)

REGISTRY: dict[str, SettingDef] = {d.key: d for d in _DEFS}


def definition(key: str) -> SettingDef:
    try:
        return REGISTRY[key]
    except KeyError:
        raise KeyError(f"unknown setting {key!r}") from None


def group_keys(group: str) -> list[str]:
    return [d.key for d in _DEFS if d.group == group]


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
    if defn.kind in ("int", "float"):
        try:
            number = float(raw) if defn.kind == "float" else int(raw)
        except ValueError:
            raise SettingError(f"{defn.label}: enter a number") from None
        if defn.minimum is not None and number < defn.minimum:
            raise SettingError(f"{defn.label}: must be at least {defn.minimum:g}")
        if defn.maximum is not None and number > defn.maximum:
            raise SettingError(f"{defn.label}: must be at most {defn.maximum:g}")
        return raw
    # text
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


def apply_changes(session: Session, operator: Operator, changes: dict[str, str | None]) -> int:
    """Apply ``key -> new value`` (``None`` clears). All values are validated before any is
    written, so one bad field changes nothing. Returns how many settings actually changed."""
    prepared: list[tuple[SettingDef, str | None]] = []
    for key, raw in changes.items():
        defn = definition(key)
        prepared.append((defn, None if raw is None else normalise(defn, raw.strip())))
    changed = 0
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
    session.flush()
    return changed


def missing_expected(session: Session) -> list[SettingDef]:
    """Settings the app needs outside development that nothing supplies yet."""
    if get_settings().env == "development":
        return []
    return [d for d in _DEFS if d.expected and get(session, d.key) is None]
