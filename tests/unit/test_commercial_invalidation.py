from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session

from africasignal.jobs.policy import workload_defaults
from africasignal.operations import commercial_invalidation


def test_periodic_context_scan_is_enqueued_only_inside_workload_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schedule = workload_defaults()["commercial"][1]
    workload = SimpleNamespace(schedule=schedule.model_dump(mode="json"))
    session = Mock(spec=Session)
    session.get.return_value = workload
    enqueue = Mock(return_value=42)
    monkeypatch.setattr(commercial_invalidation, "_rebuilds_enabled", lambda _session: True)
    monkeypatch.setattr(commercial_invalidation.queue, "enqueue", enqueue)

    closed_window_result = commercial_invalidation.enqueue_context_scan(
        session, now=datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    )
    assert closed_window_result is None
    enqueue.assert_not_called()

    open_window_result = commercial_invalidation.enqueue_context_scan(
        session, now=datetime(2026, 10, 3, 2, 0, tzinfo=UTC)
    )
    assert open_window_result == 42
    enqueue.assert_called_once()
    assert enqueue.call_args.kwargs["run_at"] == datetime(2026, 10, 3, 2, 0, tzinfo=UTC)
    assert enqueue.call_args.kwargs["dedupe_key"].startswith("commercial-context-scan:")
