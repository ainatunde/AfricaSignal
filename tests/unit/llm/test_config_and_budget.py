from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from africasignal.llm.budget import (
    BudgetStatus,
    budget_day_start,
    cost_usd,
    next_budget_day_start,
)
from africasignal.llm.config import LlmConfig, ModelPrice, load_llm_config


def test_shipped_config_loads_and_prices_every_model() -> None:
    cfg = load_llm_config()
    assert cfg.purpose("claim_extract").model in cfg.prices
    assert cfg.purpose("something_unlisted").model == cfg.default_model


def test_config_rejects_a_model_without_a_price() -> None:
    with pytest.raises(ValidationError, match="no price"):
        LlmConfig.model_validate(
            {
                "default_model": "m1",
                "purposes": {"p": {"model": "m2"}},
                "prices": {"m1": {"input": 1, "output": 2}},
            }
        )


def test_cost_is_per_million_tokens_rounded_to_five_places() -> None:
    price = ModelPrice(input=Decimal("2.00"), output=Decimal("10.00"))
    assert cost_usd(price, 1_000_000, 0) == Decimal("2.00000")
    assert cost_usd(price, 0, 1_000_000) == Decimal("10.00000")
    assert cost_usd(price, 12_345, 678) == Decimal("0.03147")  # 0.02469 + 0.00678
    assert cost_usd(price, 0, 0) == Decimal("0.00000")


def test_budget_day_is_a_lagos_calendar_day() -> None:
    # 23:30 UTC on 30 Sep is 00:30 on 1 Oct in Lagos (UTC+1), so the day began at 23:00 UTC.
    now = datetime(2026, 9, 30, 23, 30, tzinfo=UTC)
    assert budget_day_start(now) == datetime(2026, 9, 30, 23, 0, tzinfo=UTC)
    assert next_budget_day_start(now) == datetime(2026, 10, 1, 23, 0, tzinfo=UTC)
    noon = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    assert budget_day_start(noon) == datetime(2026, 9, 29, 23, 0, tzinfo=UTC)
    assert next_budget_day_start(noon) == datetime(2026, 9, 30, 23, 0, tzinfo=UTC)


def test_status_warns_from_eighty_percent_and_is_exhausted_at_the_limit() -> None:
    assert not BudgetStatus(Decimal("7.9"), Decimal(10)).warn
    assert BudgetStatus(Decimal("8"), Decimal(10)).warn
    assert not BudgetStatus(Decimal("9.99"), Decimal(10)).exhausted
    assert BudgetStatus(Decimal("10"), Decimal(10)).exhausted
