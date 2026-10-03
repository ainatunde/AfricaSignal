"""Lease cancellation and transaction/effect fencing for the current worker attempt.

External effects hold the job row lock through dispatch, so reclaim cannot overlap a
live dispatch. Provider idempotency and reconciliation remain necessary after a crash.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from threading import Event

from sqlalchemy import event, text
from sqlalchemy.orm import Session, sessionmaker

from africasignal.jobs.queue import ClaimedJob


class LeaseLost(RuntimeError):
    """This attempt must stop without changing the replacement attempt."""


@dataclass
class Attempt:
    job: ClaimedJob
    factory: sessionmaker[Session]
    lost: Event

    def check(self, session: Session, *, lock: bool = False) -> None:
        if self.lost.is_set():
            raise LeaseLost("worker lease was lost")
        statement = (
            "SELECT id FROM job WHERE id = :id AND status = 'running' "
            "AND locked_by = :owner AND lease_token = CAST(:token AS uuid) "
            "AND lease_until > clock_timestamp()"
        )
        if lock:
            statement += " FOR UPDATE"
        if self.job.lease_token is None or self.job.lock_owner is None:
            raise LeaseLost("worker attempt has no lease identity")
        if (
            session.execute(
                text(statement),
                {
                    "id": self.job.id,
                    "owner": self.job.lock_owner,
                    "token": str(self.job.lease_token),
                },
            ).scalar_one_or_none()
            is None
        ):
            self.lost.set()
            raise LeaseLost("worker lease expired or was replaced")


_effect_active: ContextVar[bool] = ContextVar("worker_effect_active", default=False)

_current: ContextVar[Attempt | None] = ContextVar("worker_attempt", default=None)


def checkpoint() -> None:
    """Fail closed before another work unit if this attempt no longer owns its lease."""
    attempt = _current.get()
    if attempt is not None:
        with attempt.factory() as session:
            attempt.check(session)


def check_cancelled() -> None:
    """Cheap cancellation check within CPU/body-stream loops."""
    attempt = _current.get()
    if attempt is not None and attempt.lost.is_set():
        raise LeaseLost("worker lease was lost")


@contextmanager
def fenced_effect() -> Iterator[None]:
    """Serialize dispatch against reclaim, preserving the existing provider identity."""
    attempt = _current.get()
    if attempt is None or _effect_active.get():
        yield
        return
    with attempt.factory() as session:
        session.execute(text("SET LOCAL lock_timeout = '2s'"))
        attempt.check(session, lock=True)
        token = _effect_active.set(True)
        try:
            yield
        finally:
            _effect_active.reset(token)
        # No handler writes are committed here. Release the fence after dispatch;
        # the handler transaction is checked independently before its commit.
        session.rollback()


@contextmanager
def executing(attempt: Attempt, session: Session) -> Iterator[None]:
    token = _current.set(attempt)

    def before_flush(current: Session, *_: object) -> None:
        attempt.check(current)

    def before_commit(current: Session) -> None:
        # The row lock covers the transaction commit; reclaim waits until it finishes.
        attempt.check(current, lock=True)

    event.listen(session, "before_flush", before_flush)
    event.listen(session, "before_commit", before_commit)
    try:
        checkpoint()
        yield
    finally:
        event.remove(session, "before_flush", before_flush)
        event.remove(session, "before_commit", before_commit)
        _current.reset(token)
