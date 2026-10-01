"""Costs page (spec B11.5): LLM spend by day and purpose from ``llm_call``, today's budget, and
email counts from the outbox. Days are Africa/Lagos days, the same as the daily budget."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import Date, cast, func, select
from sqlalchemy.orm import Session

from africasignal import settings_store
from africasignal.llm.adapter import DEFAULT_DAILY_BUDGET_USD
from africasignal.llm.budget import LAGOS, BudgetStatus, budget_status
from africasignal.models import LlmCall, Outbox

DEFAULT_DAYS = 14
MAX_DAYS = 90


@dataclass(frozen=True)
class SpendRow:
    day: date
    purpose: str
    calls: int
    cache_hits: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


@dataclass(frozen=True)
class EmailRow:
    day: date
    kind: str
    status: str
    count: int


def _window(days: int, now: datetime) -> datetime:
    days = min(max(days, 1), MAX_DAYS)
    first_day = now.astimezone(LAGOS).date() - timedelta(days=days - 1)
    return datetime.combine(first_day, datetime.min.time(), tzinfo=LAGOS).astimezone(UTC)


def llm_spend(
    session: Session, *, days: int = DEFAULT_DAYS, now: datetime | None = None
) -> list[SpendRow]:
    """Newest day first. A cache hit is a call that cost nothing; its tokens are not counted."""
    now = now or datetime.now(UTC)
    day = cast(func.timezone("Africa/Lagos", LlmCall.ts), Date)
    live = LlmCall.cache_hit.is_(False)
    rows = session.execute(
        select(
            day.label("day"),
            LlmCall.purpose,
            func.count(),
            func.count().filter(LlmCall.cache_hit.is_(True)),
            func.coalesce(func.sum(LlmCall.input_tokens).filter(live), 0),
            func.coalesce(func.sum(LlmCall.output_tokens).filter(live), 0),
            func.coalesce(func.sum(LlmCall.cost_usd).filter(live), 0),
        )
        .where(LlmCall.ts >= _window(days, now))
        .group_by(day, LlmCall.purpose)
        .order_by(day.desc(), LlmCall.purpose)
    )
    return [SpendRow(r[0], r[1], r[2], r[3], int(r[4]), int(r[5]), Decimal(r[6])) for r in rows]


def day_totals(rows: list[SpendRow]) -> list[tuple[date, Decimal]]:
    totals: dict[date, Decimal] = {}
    for row in rows:
        totals[row.day] = totals.get(row.day, Decimal(0)) + row.cost_usd
    return sorted(totals.items(), reverse=True)


def today(session: Session, now: datetime | None = None) -> BudgetStatus:
    value = settings_store.get_float(session, "llm_daily_budget_usd")
    limit = Decimal(str(DEFAULT_DAILY_BUDGET_USD if value is None else value))
    return budget_status(session, limit, now)


def email_counts(
    session: Session, *, days: int = DEFAULT_DAYS, now: datetime | None = None
) -> list[EmailRow]:
    """Outbox rows by day created, kind and status. Newest day first. Addresses are never read."""
    now = now or datetime.now(UTC)
    day = cast(func.timezone("Africa/Lagos", Outbox.created_at), Date)
    rows = session.execute(
        select(day.label("day"), Outbox.kind, Outbox.status, func.count())
        .where(Outbox.created_at >= _window(days, now))
        .group_by(day, Outbox.kind, Outbox.status)
        .order_by(day.desc(), Outbox.kind, Outbox.status)
    )
    return [EmailRow(r[0], r[1], r[2], r[3]) for r in rows]
