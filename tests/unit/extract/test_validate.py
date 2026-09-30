from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from africasignal.extract.validate import (
    find_passage,
    number_in_passage,
    numbers_in,
    validate_claim,
)

DOC = (
    "LAGOS, 12 September 2026.\n"
    "Petrol now sells for N1,020 per litre   at most filling stations in Lagos,\n"
    "up from ₦940 per litre in August. Cooking gas costs N1.2 million per tonne.\n"
    "The tariff will take effect on 1 October 2026."
)
PUBLISHED = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)
ITEMS = {"pms_litre", "lpg_5kg"}
SERIES = {"pms_regulated_price"}
PETROL = "Petrol now sells for N1,020 per litre at most filling stations in Lagos"


def claim(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "claim_type": "price_statement",
        "text": "Petrol costs N1,020 a litre in Lagos.",
        "passage": PETROL,
        "item_code": "pms_litre",
        "policy_series": None,
        "stated_value": 1020,
        "stated_unit": "NGN/litre",
        "direction": "up",
        "occurred_from": "2026-09-12",
        "occurred_to": "2026-09-12",
        "time_precision": "day",
        "place_candidates": ["Lagos"],
    }
    base.update(overrides)
    return base


def check(published: datetime | None = PUBLISHED, **overrides: Any):  # type: ignore[no-untyped-def]
    return validate_claim(claim(**overrides), DOC, published, ITEMS, SERIES)


def test_a_good_claim_is_valid_and_keeps_its_offsets() -> None:
    c = check()
    assert c.valid and c.invalid_reason is None
    assert c.stated_value == Decimal(1020)
    assert c.passage_start is not None and c.passage_end is not None
    # offsets point into the ORIGINAL text, where the passage spans a newline and extra spaces
    assert " ".join(DOC[c.passage_start : c.passage_end].split()) == PETROL


# --- 1. the passage must be in the document --------------------------------------------------


def test_passage_found_across_different_whitespace() -> None:
    assert check(passage="Petrol  now sells for\nN1,020 per litre").valid


def test_a_paraphrased_passage_is_not_found() -> None:
    c = check(passage="Petrol costs about N1,020 in Lagos")
    assert (c.valid, c.invalid_reason) == (False, "passage_not_found")
    assert c.passage_start is None


def test_a_passage_with_changed_punctuation_is_not_found() -> None:
    assert check(passage="Petrol now sells for N1020 per litre").invalid_reason == (
        "passage_not_found"
    )


def test_an_empty_passage_is_not_found() -> None:
    assert check(passage="   ").invalid_reason == "passage_not_found"


def test_a_passage_from_a_different_case_is_not_found() -> None:
    assert check(passage=PETROL.upper()).invalid_reason == "passage_not_found"


def test_invalid_claims_are_still_returned_with_their_fields() -> None:
    c = check(passage="made up")
    assert c.text and c.item_code == "pms_litre" and c.place_candidates == ["Lagos"]


# --- 2. the number must be in the passage ----------------------------------------------------


@pytest.mark.parametrize("value", [1020, 1020.0, "1020", 1020.00])
def test_the_value_is_found_however_the_json_spells_it(value: Any) -> None:
    assert check(stated_value=value).valid


def test_a_value_not_in_the_passage_is_invalid() -> None:
    assert check(stated_value=1200).invalid_reason == "value_not_in_passage"


def test_a_value_from_elsewhere_in_the_document_is_invalid() -> None:
    # 940 is in the document but not in this passage
    assert check(stated_value=940).invalid_reason == "value_not_in_passage"


def test_the_naira_sign_and_thousands_separators_are_understood() -> None:
    passage = "up from ₦940 per litre in August"
    assert check(passage=passage, stated_value=940).valid


def test_scale_words_are_understood() -> None:
    passage = "Cooking gas costs N1.2 million per tonne"
    assert check(passage=passage, stated_value=1_200_000).valid
    assert check(passage=passage, stated_value=1.2).valid
    assert check(passage=passage, stated_value=12).invalid_reason == "value_not_in_passage"


def test_a_missing_value_is_fine() -> None:
    assert check(stated_value=None, stated_unit=None).valid


def test_numbers_in_reads_separators_decimals_and_ignores_glued_digits() -> None:
    assert numbers_in("N1,020.50 and 12,345,678 and 3.5 percent") == {
        Decimal("1020.50"),
        Decimal(12345678),
        Decimal("3.5"),
    }
    assert Decimal(2003) not in numbers_in("1,2003")
    assert number_in_passage(Decimal(209.5), "N209.5 per kWh")


# --- 3 and 4. dates --------------------------------------------------------------------------


def test_a_malformed_date_is_invalid() -> None:
    assert check(occurred_from="12 September").invalid_reason == "invalid_date"


def test_an_impossible_date_is_invalid() -> None:
    assert check(occurred_from="2026-02-30", occurred_to="2026-02-30").invalid_reason == (
        "invalid_date"
    )


def test_a_range_that_ends_before_it_starts_is_invalid() -> None:
    c = check(occurred_from="2026-09-12", occurred_to="2026-09-01")
    assert c.invalid_reason == "invalid_date"


def test_a_future_date_on_a_past_tense_passage_is_invalid() -> None:
    c = check(occurred_from="2026-10-15", occurred_to="2026-10-15")
    assert c.invalid_reason == "future_date"


def test_the_day_after_publication_is_still_allowed() -> None:
    assert check(occurred_from="2026-09-13", occurred_to="2026-09-13").valid
    assert check(occurred_from="2026-09-14", occurred_to="2026-09-14").invalid_reason == (
        "future_date"
    )


def test_a_future_date_is_allowed_when_the_passage_is_about_the_future() -> None:
    c = check(
        passage="The tariff will take effect on 1 October 2026.",
        stated_value=None,
        item_code=None,
        occurred_from="2026-10-01",
        occurred_to="2026-10-01",
    )
    assert c.valid


def test_dates_are_not_checked_when_the_document_has_no_publication_date() -> None:
    assert check(published=None, occurred_from="2030-01-01", occurred_to="2030-01-01").valid


def test_a_claim_without_dates_is_fine() -> None:
    assert check(occurred_from=None, occurred_to=None, time_precision="unknown").valid


# --- 5. allowed lists ------------------------------------------------------------------------


def test_an_unknown_item_code_is_invalid() -> None:
    assert check(item_code="diesel_barrel").invalid_reason == "unknown_item_code"


def test_an_unknown_policy_series_is_invalid() -> None:
    c = check(claim_type="policy_statement", item_code=None, policy_series="made_up_series")
    assert c.invalid_reason == "unknown_policy_series"


def test_a_known_policy_series_is_valid() -> None:
    c = check(claim_type="policy_statement", item_code=None, policy_series="pms_regulated_price")
    assert c.valid


def test_a_null_item_code_is_valid() -> None:
    assert check(item_code=None).valid


def test_the_first_failing_check_gives_the_reason() -> None:
    c = check(passage="nothing like the text", stated_value=5, item_code="nope")
    assert c.invalid_reason == "passage_not_found"


def test_find_passage_returns_none_for_an_empty_document() -> None:
    assert find_passage("", "anything") is None
