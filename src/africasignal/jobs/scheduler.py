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
from africasignal.ops_heartbeat import ProcessHeartbeat
from africasignal.publish.recovery import require_recovery_complete
from africasignal.settings_store import get_int

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
    gdelt_poll_minutes = get_int(session, "gdelt_poll_minutes") or GDELT_SLOT_MINUTES
    periodic: list[tuple[str, str]] = [
        ("gdelt_poll", f"gdelt_poll:{_slot(now, gdelt_poll_minutes)}"),
        ("expire_assessments", f"expire_assessments:{_slot(now, 60)}"),
        ("release_held_versions", f"release_held_versions:{_slot(now, 1)}"),
        ("explain_backfill", f"explain_backfill:{_slot(now, 60)}"),  # AS-028
        ("dispatch_outbox", f"dispatch_outbox:{_slot(now, 1)}"),
        ("prune_events", f"prune_events:{_slot(now, 24 * 60)}"),  # retention: 13 months
        ("apply_retention", f"apply_retention:{_slot(now, 24 * 60)}"),  # accounts, feedback
        (
            "agent_reach_expire",
            f"agent_reach_expire:{_slot(now, 24 * 60)}",
        ),  # candidate/task retention
        (
            "external_agent_expire",
            f"external_agent_expire:{_slot(now, 24 * 60)}",
        ),  # task output retention and deadline cancellation
        ("check_backups", f"check_backups:{_slot(now, 60)}"),  # stale backup, failed drill alerts
        ("check_health", f"check_health:{_slot(now, 15)}"),  # failing sources, dead jobs, budget
    ]
    # 4. Weekly digest: the operator-configured weekday/hour in Africa/Lagos.
    lagos = now.astimezone(LAGOS)
    digest_weekday = get_int(session, "weekly_digest_weekday")
    digest_hour = get_int(session, "weekly_digest_hour")
    if lagos.weekday() == (0 if digest_weekday is None else digest_weekday) and lagos.hour >= (
        7 if digest_hour is None else digest_hour
    ):
        year, week, _ = lagos.isocalendar()
        periodic.append(("weekly_digest", f"weekly_digest:{year}-W{week:02d}"))
    for kind, key in periodic:
        if _enqueue_if_handled(session, kind, key):
            counts["periodic"] += 1
    from africasignal.operations.commercial_invalidation import enqueue_context_scan

    if enqueue_context_scan(session, now=now) is not None:
        counts["periodic"] += 1

    # 5. Jobs whose worker died.
    counts["reclaimed"] = queue.reclaim_expired(session)
    return counts


def main() -> None:
    configure_logging()
    require_recovery_complete()
    handler_registry.load_all()
    factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    stopping = threading.Event()
    monitor = ProcessHeartbeat("SCHEDULER_HEARTBEAT_URL")

    def _handle_signal(signum: int, frame: FrameType | None) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    log.info("scheduler started")
    monitor.start()
    while not stopping.is_set():
        try:
            with factory() as session:
                counts = tick(session)
                session.commit()
            monitor.mark_healthy()
            if any(counts.values()):
                log.info("tick %s", counts)
        except Exception:
            log.exception("scheduler tick failed")
            monitor.ping("fail")
        stopping.wait(TICK_SECONDS)
    monitor.close()
    log.info("scheduler stopped")


if __name__ == "__main__":
    main()
