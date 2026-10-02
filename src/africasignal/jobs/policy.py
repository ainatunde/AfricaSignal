"""Validated, timezone-aware workload schedules and queue admission policy."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from africasignal.models import Job, WorkloadControl

LLM_JOB_KINDS = frozenset({"extract_claims", "explain_version", "explain_backfill"})
REACH_JOB_KINDS = frozenset({"agent_reach_search", "agent_reach_poll"})
EXTERNAL_AGENT_JOB_KINDS = frozenset({"external_agent_submit"})
CONTENT_JOB_KINDS = frozenset(
    {"process_document", "gdelt_fetch_article", "import_nbs_file", "resolve_places"}
)
WORKLOAD_KINDS = {
    "ai": LLM_JOB_KINDS,
    "agent_reach": REACH_JOB_KINDS,
    "external_agents": EXTERNAL_AGENT_JOB_KINDS,
    "processing": CONTENT_JOB_KINDS,
}


class TimeWindow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    days: list[int] = Field(min_length=1, max_length=7)
    start: str
    end: str

    @field_validator("days")
    @classmethod
    def unique_days(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value) or any(day < 0 or day > 6 for day in value):
            raise ValueError("days must be unique ISO weekdays from 0 (Monday) through 6")
        return sorted(value)

    @field_validator("start", "end")
    @classmethod
    def validate_clock(cls, value: str, info: Any) -> str:
        import re

        if value == "24:00" and info.field_name == "end":
            return value
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("use 24-hour HH:MM; only an end time may be 24:00")
        return value

    @model_validator(mode="after")
    def nonzero_interval(self) -> TimeWindow:
        if _minutes(self.start) == _minutes(self.end):
            raise ValueError("a window cannot have identical start and end")
        return self


class WorkloadSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    timezone: str = "Africa/Lagos"
    windows: list[TimeWindow] = Field(min_length=1, max_length=21)
    max_concurrency: int = Field(default=2, ge=1, le=32)
    max_items_per_run: int = Field(default=100, ge=1, le=10_000)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("timezone must be a valid IANA timezone") from None
        return value

    @model_validator(mode="after")
    def no_overlapping_windows(self) -> WorkloadSchedule:
        occupied: set[int] = set()
        for window in self.windows:
            start, end = _minutes(window.start), _minutes(window.end)
            for day in window.days:
                first = day * 1440 + start
                last = day * 1440 + end if end > start else (day + 1) * 1440 + end
                if window.end == "24:00":
                    last = (day + 1) * 1440
                for minute in range(first, last):
                    slot = minute % (7 * 1440)
                    if slot in occupied:
                        raise ValueError("time windows may not overlap")
                    occupied.add(slot)
        return self

    def allows(self, moment: datetime) -> bool:
        if moment.tzinfo is None:
            raise ValueError("schedule checks require a timezone-aware instant")
        local = moment.astimezone(ZoneInfo(self.timezone))
        candidate = moment.astimezone(UTC)
        for offset in (-1, 0):
            anchor = local.date() + timedelta(days=offset)
            for window in self.windows:
                if anchor.isoweekday() - 1 not in window.days:
                    continue
                begin, finish = _utc_interval(anchor, window, self.timezone)
                if begin <= candidate < finish:
                    return True
        return False

    def current_window_end(self, moment: datetime) -> datetime | None:
        """Return the active window's exclusive UTC end, or None when the schedule is closed."""
        if moment.tzinfo is None:
            raise ValueError("schedule checks require a timezone-aware instant")
        local = moment.astimezone(ZoneInfo(self.timezone))
        candidate = moment.astimezone(UTC)
        for offset in (-1, 0):
            anchor = local.date() + timedelta(days=offset)
            for window in self.windows:
                if anchor.isoweekday() - 1 not in window.days:
                    continue
                begin, finish = _utc_interval(anchor, window, self.timezone)
                if begin <= candidate < finish:
                    return finish
        return None

    def next_open(self, moment: datetime) -> datetime:
        if moment.tzinfo is None:
            raise ValueError("schedule checks require a timezone-aware instant")
        now = moment.astimezone(UTC)
        if self.allows(now):
            return now
        local_day = now.astimezone(ZoneInfo(self.timezone)).date()
        starts: list[datetime] = []
        for offset in range(-1, 9):
            anchor = local_day + timedelta(days=offset)
            for window in self.windows:
                if anchor.isoweekday() - 1 in window.days:
                    begin, _ = _utc_interval(anchor, window, self.timezone)
                    if begin >= now:
                        starts.append(begin)
        if not starts:
            raise RuntimeError("no next workload window found")
        return min(starts)


def _minutes(value: str) -> int:
    if value == "24:00":
        return 1440
    hours, minutes = (int(part) for part in value.split(":"))
    return hours * 60 + minutes


def _wall_time(day: date, minute: int) -> datetime:
    return datetime.combine(day, time.min) + timedelta(minutes=minute)


def _valid_local(day: date, minute: int, zone_name: str, *, fold: int) -> datetime:
    zone = ZoneInfo(zone_name)
    probe = _wall_time(day, minute)
    for _ in range(181):
        local = probe.replace(tzinfo=zone, fold=fold)
        roundtrip = local.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
        if roundtrip == probe:
            return local
        probe += timedelta(minutes=1)
    raise ValueError("could not resolve local schedule time")


def _utc_interval(anchor: date, window: TimeWindow, zone_name: str) -> tuple[datetime, datetime]:
    start_minute = _minutes(window.start)
    end_minute = _minutes(window.end)
    if end_minute == 1440:
        end_day, end_minute = anchor + timedelta(days=1), 0
    elif end_minute <= start_minute:
        end_day = anchor + timedelta(days=1)
    else:
        end_day = anchor
    start_local = _valid_local(anchor, start_minute, zone_name, fold=0)
    end_local = _valid_local(end_day, end_minute, zone_name, fold=1)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


def schedule_from_text(raw: str) -> WorkloadSchedule:
    """Parse one days HH:MM-HH:MM rule per line; days are ISO 0-6 or asterisk."""
    windows: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(raw.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            days_raw, hours = line.split(None, 1)
            start, end = hours.split("-", 1)
            days = (
                list(range(7)) if days_raw == "*" else [int(part) for part in days_raw.split(",")]
            )
        except (ValueError, TypeError):
            raise ValueError(
                f"window line {line_number}: use '* HH:MM-HH:MM' or '0,1,2 HH:MM-HH:MM'"
            ) from None
        windows.append({"days": days, "start": start, "end": end})
    return WorkloadSchedule.model_validate({"windows": windows})


def workload_defaults() -> dict[str, tuple[bool, WorkloadSchedule]]:
    return {
        "ai": (
            True,
            WorkloadSchedule.model_validate(
                {
                    "windows": [{"days": list(range(7)), "start": "00:00", "end": "24:00"}],
                    "max_concurrency": 4,
                    "max_items_per_run": 100,
                }
            ),
        ),
        "agent_reach": (
            False,
            WorkloadSchedule.model_validate(
                {
                    "windows": [{"days": list(range(7)), "start": "01:00", "end": "04:00"}],
                    "max_concurrency": 1,
                    "max_items_per_run": 10,
                }
            ),
        ),
        "external_agents": (
            False,
            WorkloadSchedule.model_validate(
                {
                    "windows": [{"days": list(range(7)), "start": "01:00", "end": "04:00"}],
                    "max_concurrency": 1,
                    "max_items_per_run": 5,
                }
            ),
        ),
        "processing": (
            True,
            WorkloadSchedule.model_validate(
                {
                    "windows": [{"days": list(range(7)), "start": "00:00", "end": "24:00"}],
                    "max_concurrency": 2,
                    "max_items_per_run": 50,
                }
            ),
        ),
    }


def locked_workload_rows(session: Any) -> list[WorkloadControl]:
    """Lock durable rows so concurrent replicas cannot exceed a workload concurrency cap."""
    defaults = workload_defaults()
    rows = cast(
        list[WorkloadControl],
        session.query(WorkloadControl)
        .filter(WorkloadControl.name.in_(defaults))
        .order_by(WorkloadControl.name)
        .with_for_update()
        .all(),
    )
    present = {row.name for row in rows}
    if present != set(defaults):
        raise RuntimeError(
            "workload control rows are missing; apply the current database migration"
        )
    return rows


def blocked_kinds(session: Any, rows: list[WorkloadControl], now: datetime) -> set[str]:
    """Job kinds not eligible for admission; caller holds the policy row locks."""
    running = list(
        session.query(Job.kind)
        .filter(Job.status == "running", Job.kind.in_(set().union(*WORKLOAD_KINDS.values())))
        .all()
    )
    running_counts: dict[str, int] = {}
    for (kind,) in running:
        running_counts[kind] = running_counts.get(kind, 0) + 1
    blocked: set[str] = set()
    for row in rows:
        kinds = WORKLOAD_KINDS[row.name]
        schedule = WorkloadSchedule.model_validate(row.schedule)
        current = sum(running_counts.get(kind, 0) for kind in kinds)
        if not row.enabled or not schedule.allows(now) or current >= schedule.max_concurrency:
            blocked.update(kinds)
    return blocked
