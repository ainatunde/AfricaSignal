"""What an operator can change about a source from the console (AS-022): approve a permission
version, publish a new one, pause or resume. Each function writes its audit row in the same
transaction. Rules live here, not in the route, so the CLI and tests can use them too."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from africasignal import audit
from africasignal.models import Operator, Source, SourcePermission
from africasignal.sources.permissions import current_permission

# How long an approval stands before the console asks for a fresh review of the terms.
REVIEW_INTERVAL = timedelta(days=365)

PERMISSION_FIELDS = (
    "may_collect",
    "may_store_full_text",
    "max_quote_chars",
    "may_republish_numbers",
    "link_required",
    "retention_days",
    "terms_url",
    "rights_basis",
)


class ConsoleError(ValueError):
    """A refusal the operator should see, for example an unticked confirmation."""


@dataclass(frozen=True)
class PermissionInput:
    may_collect: bool
    may_store_full_text: bool
    max_quote_chars: int | None
    may_republish_numbers: bool
    link_required: bool
    retention_days: int | None
    terms_url: str | None
    rights_basis: str | None


def permission_snapshot(permission: SourcePermission | None) -> dict[str, Any] | None:
    """The fields an audit row records about a permission version."""
    if permission is None:
        return None
    snap: dict[str, Any] = {f: getattr(permission, f) for f in PERMISSION_FIELDS}
    snap["version"] = permission.version
    snap["approved_at"] = permission.approved_at.isoformat() if permission.approved_at else None
    snap["approved_by_operator_id"] = permission.approved_by_operator_id
    return snap


def validate_permission(data: PermissionInput) -> None:
    """Raise ``ConsoleError`` when a value is out of range or unsafe."""
    for name in ("max_quote_chars", "retention_days"):
        value = getattr(data, name)
        if value is not None and not 0 <= value <= 100_000:
            raise ConsoleError(f"{name.replace('_', ' ')} must be between 0 and 100000")
    if data.terms_url is not None:
        if len(data.terms_url) > 2000 or not data.terms_url.lower().startswith(
            ("http://", "https://")
        ):
            raise ConsoleError("the terms URL must start with http:// or https://")
    if data.rights_basis is not None and len(data.rights_basis) > 500:
        raise ConsoleError("the rights basis must be 500 characters or fewer")
    if data.may_collect and not (data.rights_basis and data.rights_basis.strip()):
        raise ConsoleError("state the rights basis before allowing collection")


def require_owner_for_outlet(source: Source, may_collect: bool) -> None:
    """Rule from security finding S-08: corroboration treats outlets with the same owner as one
    voice, so a news outlet whose owner is unknown could be counted as independent of its owner's
    other outlets. Approving collection from a news outlet therefore needs the owner on record."""
    if may_collect and source.kind == "news_outlet" and not (source.owner or "").strip():
        raise ConsoleError(
            "set the owner of this news outlet before approving it: the company or group "
            "behind it, so that outlets with the same owner count as one voice"
        )


def _lock_source(session: Session, source_id: int) -> Source:
    source = session.scalars(select(Source).where(Source.id == source_id).with_for_update()).first()
    if source is None:
        raise ConsoleError("no such source")
    return source


def approve_permission(
    session: Session,
    operator: Operator,
    source_id: int,
    version: int,
    *,
    terms_reviewed: bool,
    now: datetime | None = None,
) -> SourcePermission:
    """Approve the newest, still unapproved permission version of a source."""
    now = now or datetime.now(UTC)
    source = _lock_source(session, source_id)
    permission = session.scalars(
        select(SourcePermission).where(
            SourcePermission.source_id == source.id, SourcePermission.version == version
        )
    ).first()
    if permission is None:
        raise ConsoleError("no such permission version")
    if permission.approved_at is not None:
        raise ConsoleError("that version is already approved")
    newest = session.scalar(
        select(func.max(SourcePermission.version)).where(SourcePermission.source_id == source.id)
    )
    if version != newest:
        raise ConsoleError("only the newest version can be approved")
    if not terms_reviewed:
        raise ConsoleError("confirm that you have reviewed the source's terms")
    require_owner_for_outlet(source, permission.may_collect)
    validate_permission(
        PermissionInput(
            **{f: getattr(permission, f) for f in PERMISSION_FIELDS},
        )
    )
    before = permission_snapshot(current_permission(session, source.id))
    permission.approved_by_operator_id = operator.id
    permission.approved_at = now
    permission.terms_checked_at = now
    permission.review_due_at = now + REVIEW_INTERVAL
    session.flush()
    audit.record(
        session,
        operator,
        "source_permission.approve",
        "source_permission",
        permission.id,
        before={"source": source.slug, "in_force": before},
        after={"source": source.slug, "in_force": permission_snapshot(permission)},
    )
    return permission


def publish_permission_version(
    session: Session,
    operator: Operator,
    source_id: int,
    data: PermissionInput,
    *,
    terms_reviewed: bool,
    now: datetime | None = None,
) -> SourcePermission:
    """Create the next permission version and approve it in the same step. Approved rows are never
    edited: a change, including withdrawing permission (``may_collect`` off), is a new version."""
    now = now or datetime.now(UTC)
    source = _lock_source(session, source_id)
    validate_permission(data)
    if data.may_collect and not terms_reviewed:
        raise ConsoleError("confirm that you have reviewed the source's terms")
    require_owner_for_outlet(source, data.may_collect)
    before = permission_snapshot(current_permission(session, source.id))
    newest = session.scalar(
        select(func.max(SourcePermission.version)).where(SourcePermission.source_id == source.id)
    )
    permission = SourcePermission(
        source_id=source.id,
        version=(newest or 0) + 1,
        may_collect=data.may_collect,
        may_store_full_text=data.may_store_full_text,
        max_quote_chars=data.max_quote_chars,
        may_republish_numbers=data.may_republish_numbers,
        link_required=data.link_required,
        retention_days=data.retention_days,
        terms_url=data.terms_url,
        rights_basis=data.rights_basis,
        terms_checked_at=now if terms_reviewed else None,
        approved_by_operator_id=operator.id,
        approved_at=now,
        review_due_at=now + REVIEW_INTERVAL,
    )
    session.add(permission)
    session.flush()
    audit.record(
        session,
        operator,
        "source_permission.new_version",
        "source_permission",
        permission.id,
        before={"source": source.slug, "in_force": before},
        after={"source": source.slug, "in_force": permission_snapshot(permission)},
    )
    return permission


def set_source_active(session: Session, operator: Operator, source_id: int, active: bool) -> Source:
    """Pause (``active`` false) or resume a source. A no-op change writes no audit row."""
    source = _lock_source(session, source_id)
    if source.active == active:
        raise ConsoleError("the source is already " + ("active" if active else "paused"))
    source.active = active
    session.flush()
    audit.record(
        session,
        operator,
        "source.resume" if active else "source.pause",
        "source",
        source.id,
        before={"slug": source.slug, "active": not active},
        after={"slug": source.slug, "active": active},
    )
    return source


def set_source_owner(session: Session, operator: Operator, source_id: int, owner: str) -> Source:
    """Record who owns a source (the company or group behind it). Needed before a news outlet can
    be approved (S-08); it cannot be blanked while the outlet may be collected from."""
    source = _lock_source(session, source_id)
    owner = " ".join(owner.split())
    if len(owner) > 200:
        raise ConsoleError("the owner must be 200 characters or fewer")
    if not owner:
        in_force = current_permission(session, source.id)
        if source.kind == "news_outlet" and in_force is not None and in_force.may_collect:
            raise ConsoleError(
                "this outlet is approved for collection, so its owner must stay on record; "
                "withdraw the permission first"
            )
    if (source.owner or "") == owner:
        raise ConsoleError("the owner is already set to that")
    before = source.owner
    source.owner = owner or None
    session.flush()
    audit.record(
        session,
        operator,
        "source.set_owner",
        "source",
        source.id,
        before={"slug": source.slug, "owner": before},
        after={"slug": source.slug, "owner": source.owner},
    )
    return source
