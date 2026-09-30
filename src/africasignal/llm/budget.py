"""Cost accounting and the daily cap (spec B12).

A budget day is a calendar day in Africa/Lagos, the operator's day (the same zone the weekly
digest uses). Spend is the sum of ``llm_call.cost_usd`` for real provider calls; cache hits cost
nothing and are free even after the budget is spent.

The cap is checked before each call, not reserved, so several workers calling at once can
overshoot it by the cost of their in-flight calls. That is deliberate: a hard reservation would
need a lock around every model call for a soft cost control.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from africasignal.jobs.queue import enqueue
from africasignal.llm.config import ModelPrice
from africasignal.models import LlmCall

LAGOS = ZoneInfo("Africa/Lagos")
_PER_MILLION = Decimal(1_000_000)
_CENT_FRACTION = Decimal("0.00001")  # llm_call.cost_usd is numeric(10, 5)


def cost_usd(price: ModelPrice, input_tokens: int, output_tokens: int) -> Decimal:
    raw = (input_tokens * price.input + output_tokens * price.output) / _PER_MILLION
    return raw.quantize(_CENT_FRACTION, rounding=ROUND_HALF_UP)


def budget_day_start(now: datetime) -> datetime:
    """Midnight Africa/Lagos at the start of ``now``'s day, as a UTC instant."""
    local = now.astimezone(LAGOS)
    return datetime.combine(local.date(), time.min, tzinfo=LAGOS).astimezone(UTC)


def next_budget_day_start(now: datetime) -> datetime:
    local = now.astimezone(LAGOS)
    return datetime.combine(local.date() + timedelta(days=1), time.min, tzinfo=LAGOS).astimezone(
        UTC
    )


def spent_today(session: Session, now: datetime) -> Decimal:
    total = session.scalar(
        select(func.coalesce(func.sum(LlmCall.cost_usd), 0)).where(
            LlmCall.ts >= budget_day_start(now),
            LlmCall.ts < next_budget_day_start(now),
            LlmCall.cache_hit.is_(False),
        )
    )
    return Decimal(total or 0)


def tokens_used_by_job(session: Session, job_id: int) -> int:
    total = session.scalar(
        select(func.coalesce(func.sum(LlmCall.input_tokens + LlmCall.output_tokens), 0)).where(
            LlmCall.job_id == job_id, LlmCall.cache_hit.is_(False)
        )
    )
    return int(total or 0)


@dataclass(frozen=True)
class BudgetStatus:
    spent: Decimal
    limit: Decimal

    @property
    def fraction(self) -> float:
        return float(self.spent / self.limit) if self.limit else 1.0

    @property
    def exhausted(self) -> bool:
        return self.spent >= self.limit

    @property
    def warn(self) -> bool:
        """True from 80 % of the day's budget, the operator alert threshold (AS-041)."""
        return self.fraction >= 0.8


def budget_status(session: Session, limit: Decimal, now: datetime | None = None) -> BudgetStatus:
    return BudgetStatus(spent=spent_today(session, now or datetime.now(UTC)), limit=limit)


def defer_until_next_day(
    session: Session,
    kind: str,
    payload: dict[str, Any],
    *,
    dedupe_key: str,
    now: datetime | None = None,
) -> int | None:
    """Hand a job's work to a fresh job that runs at the start of the next budget day.

    Call it from a handler that caught ``BudgetExhausted`` and then return normally: the current
    job completes, and the new one carries the same payload, so nothing is lost and no attempt is
    used up. The new job's dedupe key is ``dedupe_key`` plus the day it is deferred to, so the
    same work is deferred at most once per day. Returns the new job id, or ``None`` when that job
    already exists.
    """
    run_at = next_budget_day_start(now or datetime.now(UTC))
    day = run_at.astimezone(LAGOS).date().isoformat()
    return enqueue(session, kind, payload, dedupe_key=f"{dedupe_key}:after:{day}", run_at=run_at)
