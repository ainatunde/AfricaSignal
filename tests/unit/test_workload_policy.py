from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from africasignal.jobs.policy import WorkloadSchedule


def schedule(timezone: str, windows: list[dict]) -> WorkloadSchedule:
    return WorkloadSchedule.model_validate({"timezone": timezone, "windows": windows})


def test_window_end_is_exclusive_and_start_is_inclusive() -> None:
    policy = schedule(
        "Africa/Lagos",
        [{"days": [0], "start": "09:00", "end": "11:00"}],
    )
    assert policy.allows(datetime(2026, 10, 5, 8, 0, tzinfo=UTC))
    assert not policy.allows(datetime(2026, 10, 5, 10, 0, tzinfo=UTC))


def test_overnight_window_applies_to_the_previous_weekday() -> None:
    policy = schedule(
        "Africa/Lagos",
        [{"days": [0], "start": "22:00", "end": "02:00"}],
    )
    assert policy.allows(datetime(2026, 10, 6, 0, 30, tzinfo=UTC))
    assert not policy.allows(datetime(2026, 10, 6, 1, 0, tzinfo=UTC))


def test_spring_forward_window_uses_valid_local_boundaries() -> None:
    policy = schedule(
        "America/New_York",
        [{"days": [6], "start": "01:00", "end": "03:00"}],
    )
    assert policy.allows(datetime(2026, 3, 8, 6, 30, tzinfo=UTC))
    assert not policy.allows(datetime(2026, 3, 8, 7, 0, tzinfo=UTC))


def test_fall_back_window_covers_both_repeated_local_hours() -> None:
    policy = schedule(
        "America/New_York",
        [{"days": [6], "start": "01:00", "end": "02:00"}],
    )
    assert policy.allows(datetime(2026, 11, 1, 5, 30, tzinfo=UTC))
    assert policy.allows(datetime(2026, 11, 1, 6, 30, tzinfo=UTC))
    assert not policy.allows(datetime(2026, 11, 1, 7, 0, tzinfo=UTC))


def test_overlaps_invalid_timezones_and_excessive_concurrency_fail_closed() -> None:
    with pytest.raises(ValidationError):
        schedule(
            "Africa/Lagos",
            [
                {"days": [0], "start": "22:00", "end": "02:00"},
                {"days": [1], "start": "01:00", "end": "03:00"},
            ],
        )
    with pytest.raises(ValidationError):
        schedule("Not/A_Real_Zone", [{"days": [0], "start": "09:00", "end": "10:00"}])
    with pytest.raises(ValidationError):
        WorkloadSchedule.model_validate(
            {
                "windows": [{"days": [0], "start": "09:00", "end": "10:00"}],
                "max_concurrency": 33,
            }
        )


def test_current_window_end_returns_exclusive_utc_close() -> None:
    policy = schedule(
        "Africa/Lagos",
        [{"days": [0], "start": "09:00", "end": "11:00"}],
    )
    assert policy.current_window_end(datetime(2026, 10, 5, 8, 30, tzinfo=UTC)) == datetime(
        2026, 10, 5, 10, 0, tzinfo=UTC
    )
    assert policy.current_window_end(datetime(2026, 10, 5, 10, 0, tzinfo=UTC)) is None
    with pytest.raises(ValueError, match="timezone-aware"):
        policy.current_window_end(datetime(2026, 10, 5, 8, 30))
