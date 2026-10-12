"""Retention: the daily clean-up that makes the privacy notice true (AS-043 gaps G1, G2, G4).

What it deletes, and when:

* Login links and sessions, 30 days after they expired (or were used or revoked). Nothing reads
  them after that; the grace period only helps when someone investigates a sign-in problem.
* Accounts whose address was never verified, 30 days after they were created. An account row is
  made the moment an address is typed at /signin, and nobody who did not click the link ever
  proved the address is theirs.
* Personal content in feedback, after the configured number of months (``feedback_retention_months``
  in the console Settings, default 24): the text, typed contact email, visitor code and account
  link are removed. The row stays (its kind, status and the operator's resolution note), so vote
  counts and what was corrected are kept.
* Ledger entries for deleted accounts, once no backup that could hold the account remains.

Events are pruned by their own job (``prune_events``, 13 months).

``reapply_deletions`` is the post-restore step: it deletes again every account that its holder
deleted after the restored backup was taken.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from africasignal import settings_store
from africasignal.jobs import queue
from africasignal.models import (
    AccountDeletion,
    AppUser,
    Claim,
    EvidenceDocument,
    Feedback,
    LlmResponseCache,
    LoginToken,
    UserSession,
)
from africasignal.operations.commercial_reporting import prune_delivery_data
from africasignal.publish import accounts, deletions
from africasignal.storage import S3Store, evidence_key

log = logging.getLogger("africasignal.retention")

TOKEN_GRACE = timedelta(days=30)
UNVERIFIED_ACCOUNT_TTL = timedelta(days=30)
FEEDBACK_RETENTION_KEY = "feedback_retention_months"
DAYS_PER_MONTH = 30.4375


@dataclass
class RetentionResult:
    login_tokens: int = 0
    sessions: int = 0
    unverified_accounts: int = 0
    feedback_scrubbed: int = 0
    ledger_pruned: int = 0
    ledger_mirrored: int = 0
    evidence_expired: int = 0
    evidence_redacted: int = 0
    delivery_events: int = 0
    delivery_aggregates: int = 0


def purge_login_records(session: Session, now: datetime) -> tuple[int, int]:
    """Delete login tokens and sessions that ended more than ``TOKEN_GRACE`` ago."""
    cutoff = now - TOKEN_GRACE
    tokens = session.execute(
        delete(LoginToken).where(or_(LoginToken.expires_at < cutoff, LoginToken.used_at < cutoff))
    ).rowcount  # type: ignore[attr-defined]
    sessions = session.execute(
        delete(UserSession).where(
            or_(UserSession.expires_at < cutoff, UserSession.revoked_at < cutoff)
        )
    ).rowcount  # type: ignore[attr-defined]
    return int(tokens), int(sessions)


def purge_unverified_accounts(session: Session, now: datetime) -> int:
    cutoff = now - UNVERIFIED_ACCOUNT_TTL
    users = session.scalars(
        select(AppUser).where(AppUser.email_verified_at.is_(None), AppUser.created_at < cutoff)
    ).all()
    for user in users:
        accounts.erase_user(session, user)  # no ledger entry: a restore brings it back only to
        # be deleted again by this same job on its next run
    return len(users)


def feedback_retention_months(session: Session) -> int:
    return settings_store.get_int(session, FEEDBACK_RETENTION_KEY) or 24


def scrub_old_feedback(session: Session, now: datetime, months: int) -> int:
    cutoff = now - timedelta(days=round(months * DAYS_PER_MONTH))
    result = session.execute(
        update(Feedback)
        .where(
            Feedback.created_at < cutoff,
            or_(
                Feedback.text.is_not(None),
                Feedback.contact_email.is_not(None),
                Feedback.anon_id.is_not(None),
                Feedback.user_id.is_not(None),
            ),
        )
        .values(text=None, contact_email=None, anon_id=None, user_id=None)
    )
    return int(result.rowcount)  # type: ignore[attr-defined]


def ledger_keep(session: Session) -> timedelta:
    days = settings_store.get_int(session, "backup_retain_days") or 30
    return timedelta(days=days) + deletions.LEDGER_MARGIN


def redact_forbidden_copies(session: Session, store: S3Store | None, now: datetime) -> int:
    """Convert legacy raw copies to permitted quotations, preserving legally shared objects."""
    from africasignal.publish.invalidation import invalidate
    from africasignal.sources.permissions import current_permission

    changed = 0
    for document in session.scalars(
        select(EvidenceDocument)
        .where(
            EvidenceDocument.status == "active", ~EvidenceDocument.storage_key.endswith(".quote")
        )
        .with_for_update(skip_locked=True)
    ):
        permission = current_permission(session, document.source_id)
        if permission is None or permission.may_store_full_text:
            continue
        if store is None:
            raise RuntimeError("Legacy evidence redaction requires object storage")
        quote = document.excerpt or ""
        if permission.max_quote_chars is not None:
            quote = quote[: permission.max_quote_chars]
        retained = quote.encode()
        key = evidence_key(hashlib.sha256(retained).hexdigest(), "quote")
        store.put(key, retained, "text/plain")
        old_key = document.storage_key
        document.storage_key = key
        document.text_content = None
        document.excerpt = quote or None
        for claim in session.scalars(
            select(Claim).where(Claim.evidence_document_id == document.id)
        ):
            if claim.passage and claim.passage not in quote:
                claim.passage = ""
                claim.text = ""
                claim.valid = False
                claim.invalid_reason = "outside permitted retained quotation"
                invalidate(session, "claim", [claim.id], now)
        queue.enqueue(
            session,
            "purge_expired_evidence",
            {"storage_key": old_key},
            dedupe_key=f"redact_evidence:{document.id}:{old_key}",
        )
        changed += 1
    if changed:
        session.execute(delete(LlmResponseCache))
    return changed


def expire_evidence(session: Session, now: datetime) -> int:
    """Invalidate expired evidence now and queue idempotent object deletion after commit."""
    from africasignal.operations.commercial_invalidation import invalidate_evidence_contexts
    from africasignal.publish.invalidation import invalidate

    documents = session.scalars(
        select(EvidenceDocument)
        .where(EvidenceDocument.status != "expired", EvidenceDocument.retention_until <= now)
        .with_for_update(skip_locked=True)
    ).all()
    for document in documents:
        document.status = "expired"
        document.text_content = None
        document.excerpt = None
        session.execute(
            update(Claim)
            .where(Claim.evidence_document_id == document.id)
            .values(text="", passage="", valid=False, invalid_reason="evidence retention expired")
        )
        invalidate(session, "evidence_document", [document.id], now)
        invalidate_evidence_contexts(
            session, document.id, reason="evidence_retention_expired", now=now
        )
        queue.enqueue(
            session,
            "purge_expired_evidence",
            {"storage_key": document.storage_key},
            dedupe_key=f"purge_expired_evidence:{document.id}",
        )
    if documents:
        # Cache entries do not track source dependencies; clearing them prevents reuse of quotes
        # from expired evidence. Billed call metadata stays for cost accounting.
        session.execute(delete(LlmResponseCache))
    return len(documents)


def run(session: Session, now: datetime, store: S3Store | None) -> RetentionResult:
    """The daily retention pass. The caller commits."""
    result = RetentionResult()
    result.evidence_expired = expire_evidence(session, now)
    result.evidence_redacted = redact_forbidden_copies(session, store, now)
    result.login_tokens, result.sessions = purge_login_records(session, now)
    result.unverified_accounts = purge_unverified_accounts(session, now)
    result.feedback_scrubbed = scrub_old_feedback(session, now, feedback_retention_months(session))
    result.ledger_mirrored = deletions.mirror_pending(session, store, now)
    result.ledger_pruned = deletions.prune(session, store, now, ledger_keep(session))
    result.delivery_events, result.delivery_aggregates = prune_delivery_data(session, now=now)
    return result


def reapply_deletions(session: Session, now: datetime, store: S3Store | None) -> int:
    """After a restore: delete again every account its holder had deleted after the backup was
    taken. Reads the ledger table and, when a store is given, the copy in object storage (which
    the restore did not roll back). Returns how many accounts were deleted. The caller commits.

    An account is deleted when its address matches an entry and it was created at or before the
    deletion; an account created after the deletion is a new sign-up and stays."""
    known = {(e.email_hmac, e.deleted_at) for e in deletions.database_entries(session)}
    entries = set(known)
    if store is not None:
        entries |= {(e.email_hmac, e.deleted_at) for e in deletions.stored_entries(store)}
    latest: dict[str, datetime] = {}
    for fingerprint, deleted_at in entries:
        if fingerprint not in latest or deleted_at > latest[fingerprint]:
            latest[fingerprint] = deleted_at

    removed = 0
    for user in session.scalars(select(AppUser)).all():
        when = latest.get(deletions.fingerprint(user.email))
        if when is not None and user.created_at <= when:
            accounts.erase_user(session, user)
            removed += 1

    # Entries only the object store had are added back to the table, so they stay on the books
    # until they are old enough to prune; entries only the table had are copied out again.
    for fingerprint, deleted_at in entries - known:
        session.add(AccountDeletion(email_hmac=fingerprint, deleted_at=deleted_at, mirrored_at=now))
    session.flush()
    deletions.mirror_pending(session, store, now)
    return removed
