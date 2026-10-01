"""The ledger of deleted accounts (plan B3.7, AS-043 gap G4).

Deleting an account is a hard delete. A backup taken before the deletion still holds the account,
so restoring it would bring the person back without anyone noticing. To stop that, every deletion
the account holder asks for is written to a ledger, and ``retention.reapply_deletions`` runs after
a restore and deletes those accounts again (``python -m africasignal.admin reapply-deletions``).

What the ledger holds: a keyed fingerprint of the address (HMAC-SHA256 under a key derived from
``SECRET_KEY``) and the time of deletion. Not the address. Someone who reads the ledger but does
not have ``SECRET_KEY`` cannot test a guessed address against it. It is kept for as long as a
backup holding the account could be restored, then pruned.

A database restore rolls the ``account_deletion`` table back with everything else, so each entry is
also written, by a job a minute or so after the deletion, to object storage (``deletions/<date>/``
in the app's bucket), which a database restore does not touch.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from africasignal.config import get_settings
from africasignal.jobs import queue
from africasignal.models import AccountDeletion
from africasignal.operators import derive_key
from africasignal.storage import S3Store

log = logging.getLogger("africasignal.deletions")

LEDGER_PREFIX = "deletions/"
MIRROR_JOB = "mirror_deletions"
# Kept this long after the backup retention period, so a dump taken just before a deletion is
# covered for as long as it exists.
LEDGER_MARGIN = timedelta(days=2)


@dataclass(frozen=True)
class Entry:
    email_hmac: str
    deleted_at: datetime


def fingerprint(email: str) -> str:
    return hmac.new(
        derive_key("account-deletion-ledger"), email.strip().lower().encode(), hashlib.sha256
    ).hexdigest()


def _key(entry: Entry) -> str:
    return f"{LEDGER_PREFIX}{entry.deleted_at.astimezone(UTC):%Y-%m-%d}/{entry.email_hmac}.json"


def record(session: Session, email: str, now: datetime) -> None:
    """Note that this address's account was deleted, and queue the copy to object storage.
    The caller commits together with the deletion."""
    row = AccountDeletion(email_hmac=fingerprint(email), deleted_at=now)
    session.add(row)
    session.flush()
    queue.enqueue(session, MIRROR_JOB, dedupe_key=f"{MIRROR_JOB}:{row.id}")


def mirror_pending(session: Session, store: S3Store | None, now: datetime) -> int:
    """Copy ledger entries that are not yet in object storage. Returns how many were copied.
    With no store configured nothing is copied and the entries stay pending."""
    if store is None:
        if get_settings().env != "development":
            raise RuntimeError("Deletion ledger mirroring requires object storage")
        return 0
    copied = 0
    for row in session.scalars(
        select(AccountDeletion)
        .where(AccountDeletion.mirrored_at.is_(None))
        .order_by(AccountDeletion.id)
    ):
        entry = Entry(row.email_hmac, row.deleted_at)
        body = json.dumps(
            {"email_hmac": entry.email_hmac, "deleted_at": entry.deleted_at.isoformat()}
        )
        store.put(_key(entry), body.encode(), "application/json")
        row.mirrored_at = now
        copied += 1
    session.flush()
    return copied


def stored_entries(store: S3Store) -> list[Entry]:
    """Read every entry; an unreadable ledger prevents restore acceptance."""
    found: list[Entry] = []
    for key in store.list_keys(LEDGER_PREFIX):
        try:
            data = json.loads(store.get(key))
            email_hmac = data["email_hmac"]
            deleted_at = datetime.fromisoformat(data["deleted_at"])
            if not isinstance(email_hmac, str) or re.fullmatch(r"[0-9a-f]{64}", email_hmac) is None:
                raise ValueError("invalid fingerprint")
            if deleted_at.tzinfo is None:
                raise ValueError("timestamp must include a timezone")
            found.append(Entry(email_hmac, deleted_at))
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f"unreadable deletion ledger object {key}") from exc
    return found


def database_entries(session: Session) -> list[Entry]:
    return [
        Entry(h, at)
        for h, at in session.execute(select(AccountDeletion.email_hmac, AccountDeletion.deleted_at))
    ]


def prune(session: Session, store: S3Store | None, now: datetime, keep: timedelta) -> int:
    """Drop entries older than ``keep`` from the database and object storage. Returns how many
    database rows were dropped."""
    cutoff = now - keep
    removed = session.execute(
        delete(AccountDeletion).where(AccountDeletion.deleted_at < cutoff)
    ).rowcount  # type: ignore[attr-defined]
    if store is not None:
        cutoff_day = cutoff.astimezone(UTC).date()
        for key in store.list_keys(LEDGER_PREFIX):
            try:
                day = date.fromisoformat(key[len(LEDGER_PREFIX) :].split("/", 1)[0])
            except ValueError:
                continue
            if day < cutoff_day:
                store.delete(key)
    return int(removed)
