"""Scheduler: every 60 s, enqueue due work and reclaim expired leases (spec B4)."""

from __future__ import annotations

import logging
import signal
import threading
from datetime import UTC, datetime, timedelta
from types import FrameType
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from africasignal.db import get_engine
from africasignal.jobs import handlers as handler_registry
from africasignal.jobs import queue
from africasignal.jobs.log import configure_logging

log = logging.getLogger("africasignal.scheduler")

TICK_SECONDS = 60
LAGOS = ZoneInfo("Africa/Lagos")
GDELT_SLOT_MINUTES = 15


def _slot(moment: datetime, minutes: int) -> int:
    """Index of the ``minutes``-long slot containing ``moment`` (floor to the interval)."""
    return int(moment.timestamp() // (minutes * 60))


def _enqueue_if_handled(
    session: Session, kind: str, dedupe_key: str, payload: dict[str, object] | None = None
) -> bool:
    """Enqueue only kinds that have a registered handler, so unbuilt features don't pile up
    dead jobs."""
    if handler_registry.get_handler(kind) is None:
        return False
    return queue.enqueue(session, kind, payload, dedupe_key=dedupe_key) is not None


def tick(session: Session, now: datetime | None = None) -> dict[str, int]:
    """One scheduler pass. The caller commits. Returns counts, for logging and tests."""
    now = now or datetime.now(UTC)
    counts = {"fetch_source": 0, "reclaimed": 0, "periodic": 0}

    # 1. Sources that are due. Missed runs collapse into one job through the dedupe key.
    due = session.execute(
        text(
            "SELECT id, schedule_minutes, next_due_at FROM source "
            "WHERE active AND adapter <> 'gdelt' AND next_due_at <= :now "  # gdelt: gdelt_poll job
            "ORDER BY next_due_at FOR UPDATE"
        ),
        {"now": now},
    ).mappings()
    for source in list(due):
        slot = _slot(source["next_due_at"], source["schedule_minutes"])
        key = f"fetch:{source['id']}:{slot}"
        if _enqueue_if_handled(session, "fetch_source", key, {"source_id": source["id"]}):
            counts["fetch_source"] += 1
        session.execute(
            text("UPDATE source SET next_due_at = :next WHERE id = :id"),
            {"next": now + timedelta(minutes=source["schedule_minutes"]), "id": source["id"]},
        )

    # 2-3. Periodic jobs, deduplicated on their time slot.
    periodic: list[tuple[str, str]] = [
        ("gdelt_poll", f"gdelt_poll:{_slot(now, GDELT_SLOT_MINUTES)}"),
        ("expire_assessments", f"expire_assessments:{_slot(now, 60)}"),
        ("release_held_versions", f"release_held_versions:{_slot(now, 1)}"),
        ("dispatch_outbox", f"dispatch_outbox:{_slot(now, 1)}"),
        ("prune_events", f"prune_events:{_slot(now, 24 * 60)}"),  # retention: 13 months
        ("check_backups", f"check_backups:{_slot(now, 60)}"),  # stale backup, failed drill alerts
    ]
    # 4. Weekly digest: Monday from 07:00 Africa/Lagos, deduplicated per ISO week.
    lagos = now.astimezone(LAGOS)
    if lagos.weekday() == 0 and lagos.hour >= 7:
        year, week, _ = lagos.isocalendar()
        periodic.append(("weekly_digest", f"weekly_digest:{year}-W{week:02d}"))
    for kind, key in periodic:
        if _enqueue_if_handled(session, kind, key):
            counts["periodic"] += 1

    # 5. Jobs whose worker died.
    counts["reclaimed"] = queue.reclaim_expired(session)
    return counts


def main() -> None:
    configure_logging()
    handler_registry.load_all()
    factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    stopping = threading.Event()

    def _handle_signal(signum: int, frame: FrameType | None) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    log.info("scheduler started")
    while not stopping.is_set():
        try:
            with factory() as session:
                counts = tick(session)
                session.commit()
            if any(counts.values()):
                log.info("tick %s", counts)
        except Exception:
            log.exception("scheduler tick failed")
        stopping.wait(TICK_SECONDS)
    log.info("scheduler stopped")


if __name__ == "__main__":
    main()
