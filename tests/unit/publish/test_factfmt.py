from __future__ import annotations

import pytest

from africasignal.publish.factfmt import (
    allowed_numbers,
    fact_value_text,
    format_change,
    format_number,
    format_value,
    numbers_in,
)


def test_money_changes_counts_and_units_read_naturally() -> None:
    assert format_value(1005.47, "NGN/litre") == "₦1,005.47 per litre"
    assert format_value(6500, "NGN/5kg") == "₦6,500.00 per 5kg"
    assert format_value(3.25, "%") == "3.3%"  # half rounds up, as the assessment does
    assert format_value(36, "states") == "36 states"
    assert format_value(4.5, "states") == "4.50 states"
    assert format_number(1234567.891) == "1,234,567.89"


@pytest.mark.parametrize(
    ("value", "text"), [(3.2, "+3.2%"), (-1.04, "-1.0%"), (0, "0.0%"), (82.9, "+82.9%")]
)
def test_changes_carry_their_sign(value: float, text: str) -> None:
    assert format_change(value) == text


def test_a_place_list_fact_names_each_place() -> None:
    fact = {
        "unit": "NGN/litre",
        "value": [{"place": "Jigawa", "value": 1288.18}, {"place": "Borno", "value": 1283.79}],
    }
    assert fact_value_text(fact) == "Jigawa ₦1,288.18 per litre, Borno ₦1,283.79 per litre"
    assert fact_value_text({"unit": "%", "value": 8.0}) == "+8.0%"
    assert fact_value_text({"unit": "", "value": None}) == ""


def test_numbers_are_found_as_written_and_unit_digits_are_not_numbers() -> None:
    text = "Average cooking gas (12.5 kg refill) in Lagos rose 3.2% in 2026 to ₦1,005.47 (NBS)."
    assert numbers_in(text) == ["3.2", "2026", "1,005.47"]
    assert numbers_in("NGN/5kg, 12.5kg, T1, price-lpg_5kg-ng, 5 kg") == []
    assert numbers_in("up 7%, then 1,288.18.") == ["7", "1,288.18"]


def test_allowed_numbers_cover_values_rounding_periods_and_lists() -> None:
    facts = [
        {"value": 1005.47, "unit": "NGN/litre", "period": "August 2026"},
        {"value": -3.2, "unit": "%", "period": "August 2026"},
        {"value": [{"place": "Jigawa", "value": 1288.18}], "unit": "NGN/litre", "period": "x"},
        {"value": 36, "unit": "states", "period": "x"},
    ]
    allowed = allowed_numbers(facts)
    for number in ("1,005.47", "1005.47", "1,005", "3.2", "3", "2026", "1,288.18", "36"):
        assert number in allowed, number
    for number in ("1,005.48", "4.2", "2025", "37"):
        assert number not in allowed, number
