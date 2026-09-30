"""Source health: consecutive failures degrade a source, then fail it (spec B4, B6.3)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from africasignal.models import Source

DEGRADED_AFTER = 2  # consecutive failures
FAILING_AFTER = 5


def record_failure(session: Session, source_id: int, error: str) -> None:
    """Persist a failure in its own transaction, because the job's transaction is rolled back
    when the handler raises."""
    with Session(bind=session.get_bind()) as own:
        source = own.get(Source, source_id)
        if source is None:
            return
        source.consecutive_failures += 1
        source.last_error = error[:1000]
        if source.consecutive_failures >= FAILING_AFTER:
            source.health = "failing"
        elif source.consecutive_failures >= DEGRADED_AFTER:
            source.health = "degraded"
        own.commit()
