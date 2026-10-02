"""Cost accounting and the daily cap (spec B12).

A budget day is a calendar day in Africa/Lagos, the operator's day (the same zone the weekly
digest uses). Spend is the sum of ``llm_call.cost_usd`` for real provider calls; cache hits cost
nothing and are free even after the budget is spent. Before a provider call, the adapter reserves
a conservative upper bound in a short transaction serialized by a PostgreSQL advisory lock. The
reservation is settled to actual metered usage when the provider returns, or retained as uncertain
for the rest of the budget day when the network outcome is ambiguous.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from africasignal.jobs.queue import enqueue
from africasignal.llm.config import ModelPrice
from africasignal.llm.errors import BudgetExhausted, JobTokenLimitExceeded
from africasignal.models import LlmBudgetReservation, LlmCall

LAGOS = ZoneInfo("Africa/Lagos")
_PER_MILLION = Decimal(1_000_000)
_CENT_FRACTION = Decimal("0.00001")  # llm_call.cost_usd is numeric(10, 5)
_BUDGET_LOCK = (
    7_211_994_308_417  # serialize only the short admission transaction, not provider calls
)


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
    recorded = session.scalar(
        select(func.coalesce(func.sum(LlmCall.input_tokens + LlmCall.output_tokens), 0)).where(
            LlmCall.job_id == job_id, LlmCall.cache_hit.is_(False)
        )
    )
    reserved = session.scalar(
        select(
            func.coalesce(
                func.sum(
                    LlmBudgetReservation.input_token_bound + LlmBudgetReservation.output_token_bound
                ),
                0,
            )
        ).where(
            LlmBudgetReservation.job_id == job_id,
            LlmBudgetReservation.state.in_(("reserved", "uncertain")),
        )
    )
    return int(recorded or 0) + int(reserved or 0)


def reserve_call(
    session: Session,
    *,
    limit: Decimal,
    price: ModelPrice,
    input_token_bound: int,
    output_token_bound: int,
    job_id: int | None,
    per_job_limit: int,
    now: datetime,
) -> int:
    """Atomically reserve an upper-bound call cost before any provider request is sent."""
    if input_token_bound < 0 or output_token_bound < 0:
        raise ValueError("token bounds must be nonnegative")
    session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": _BUDGET_LOCK})
    day = budget_day_start(now)
    spent = spent_today(session, now)
    reserved = session.scalar(
        select(func.coalesce(func.sum(LlmBudgetReservation.reserved_usd), 0)).where(
            LlmBudgetReservation.budget_day == day,
            LlmBudgetReservation.state.in_(("reserved", "uncertain")),
        )
    )
    reserved_total = Decimal(reserved or 0)
    if job_id is not None:
        used = tokens_used_by_job(session, job_id)
        if used + input_token_bound + output_token_bound > per_job_limit:
            raise JobTokenLimitExceeded(
                f"job {job_id} needs up to {input_token_bound + output_token_bound} tokens; "
                f"{used} of its {per_job_limit} token allowance is already used or reserved"
            )
    raw_bound = (
        Decimal(input_token_bound) * price.input + Decimal(output_token_bound) * price.output
    ) / _PER_MILLION
    amount = raw_bound.quantize(_CENT_FRACTION, rounding=ROUND_CEILING)
    if spent + reserved_total + amount > limit:
        raise BudgetExhausted(spent + reserved_total + amount, limit, next_budget_day_start(now))
    reservation = LlmBudgetReservation(
        budget_day=day,
        job_id=job_id,
        input_token_bound=input_token_bound,
        output_token_bound=output_token_bound,
        reserved_usd=amount,
        state="reserved",
    )
    session.add(reservation)
    session.flush()
    reservation_id = reservation.id
    session.commit()
    return reservation_id


def settle_call(
    session: Session, reservation_id: int, actual_cost_usd: Decimal, now: datetime
) -> None:
    reservation = session.get(LlmBudgetReservation, reservation_id)
    if reservation is None or reservation.state not in ("reserved", "uncertain"):
        raise RuntimeError("model-spend reservation is missing or already settled")
    reservation.actual_cost_usd = actual_cost_usd
    reservation.state = "settled"
    reservation.settled_at = now
    reservation.error = None
    session.flush()


def mark_uncertain(session: Session, reservation_id: int, error: str) -> None:
    reservation = session.get(LlmBudgetReservation, reservation_id)
    if reservation is None or reservation.state != "reserved":
        return
    reservation.state = "uncertain"
    # Store only the exception type; provider errors can contain prompts, headers or credentials.
    reservation.error = error[:120]
    session.commit()


@dataclass(frozen=True)
class BudgetStatus:
    spent: Decimal
    limit: Decimal
    reserved: Decimal = Decimal(0)

    @property
    def committed_and_reserved(self) -> Decimal:
        return self.spent + self.reserved

    @property
    def fraction(self) -> float:
        return float(self.committed_and_reserved / self.limit) if self.limit else 1.0

    @property
    def exhausted(self) -> bool:
        return self.committed_and_reserved >= self.limit

    @property
    def warn(self) -> bool:
        """True from 80 % of the day's budget, the operator alert threshold (AS-041)."""
        return self.fraction >= 0.8


def budget_status(session: Session, limit: Decimal, now: datetime | None = None) -> BudgetStatus:
    moment = now or datetime.now(UTC)
    reserved = session.scalar(
        select(func.coalesce(func.sum(LlmBudgetReservation.reserved_usd), 0)).where(
            LlmBudgetReservation.budget_day == budget_day_start(moment),
            LlmBudgetReservation.state.in_(("reserved", "uncertain")),
        )
    )
    return BudgetStatus(
        spent=spent_today(session, moment), limit=limit, reserved=Decimal(reserved or 0)
    )


def defer_until_next_day(
    session: Session,
    kind: str,
    payload: dict[str, Any],
    *,
    dedupe_key: str,
    retry_at: datetime | None = None,
    now: datetime | None = None,
) -> int | None:
    """Hand a job's work to a fresh job that runs at the start of the next budget day.

    Call it from a handler that caught ``BudgetExhausted`` and then return normally: the current
    job completes, and the new one carries the same payload, so nothing is lost and no attempt is
    used up. Pass ``BudgetExhausted.retry_at`` as ``retry_at``; without it the job runs at the
    start of the budget day after ``now``. The new job's dedupe key is ``dedupe_key`` plus the day
    it is deferred to, so the same work is deferred at most once per day. Returns the new job id,
    or ``None`` when that job already exists.
    """
    run_at = retry_at or next_budget_day_start(now or datetime.now(UTC))
    day = run_at.astimezone(LAGOS).date().isoformat()
    return enqueue(session, kind, payload, dedupe_key=f"{dedupe_key}:after:{day}", run_at=run_at)
