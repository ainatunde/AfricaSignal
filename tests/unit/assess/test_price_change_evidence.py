"""T1 evidence states, possible factors and independence (spec B8.2, AS-027): one test per rule."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from africasignal.assess.price_change import FactorSpec, compute_price_change
from tests.unit.assess.claim_support import claim, official
from tests.unit.assess.test_price_change import inputs


def assess(*claims, **kwargs):  # type: ignore[no-untyped-def]
    base = inputs(**kwargs)
    return compute_price_change(base.__class__(**{**base.__dict__, "claims": tuple(claims)}))


def with_origin(origin_id: int, **kwargs):  # type: ignore[no-untyped-def]
    """Inputs whose current and previous measurements come from one official origin."""
    base = inputs(**kwargs)
    current = base.current.__class__(**{**base.current.__dict__, "origin_id": origin_id})
    previous = base.previous.__class__(**{**base.previous.__dict__, "origin_id": origin_id})
    return base.__class__(**{**base.__dict__, "current": current, "previous": previous})


# --- reported ----------------------------------------------------------------------------------


def test_no_claims_is_reported() -> None:
    result = assess()
    assert result.evidence_state == "reported"
    assert not any(kind == "claim" for kind, _ in result.inputs)


# --- corroborated ------------------------------------------------------------------------------


def test_a_news_claim_in_the_same_direction_from_another_origin_corroborates() -> None:
    news = claim()
    result = assess(news)
    assert result.evidence_state == "corroborated"
    fact = next(f for f in result.facts if f["label"] == "Independent reports")
    assert fact["value"] == 1 and fact["unit"] == "independent outlets"
    assert fact["evidence_ids"] == [news.evidence_document_id]
    assert fact["place_code"] == "NG-LA"  # the situation's scope, whatever place the claim named
    assert ("claim", news.claim_id) in result.inputs
    assert ("evidence_document", news.evidence_document_id) in result.inputs
    assert "No independent report for this state and month" not in result.unknowns


def test_corroboration_does_not_change_the_figures_or_severity() -> None:
    plain, corroborated = assess(), assess(claim())
    assert corroborated.headline == plain.headline
    assert (corroborated.severity, corroborated.mom_pct) == (plain.severity, plain.mom_pct)


def test_unchanged_is_corroborated_by_unchanged() -> None:
    result = assess(claim(direction="unchanged"), current="1000.48", previous="1000.48")
    assert result.evidence_state == "corroborated"


def test_a_fall_is_corroborated_by_a_fall_not_a_rise() -> None:
    down = assess(claim(direction="down"), current="900.00", previous="1000.48")
    assert down.evidence_state == "corroborated"
    up = assess(claim(direction="up"), current="900.00", previous="1000.48")
    assert up.evidence_state == "reported"


def test_an_unknown_direction_corroborates_nothing() -> None:
    assert assess(claim(direction="unknown")).evidence_state == "reported"


@pytest.mark.parametrize(
    ("published", "expected"),
    [
        (date(2024, 9, 1), "corroborated"),  # a month before the period starts
        (date(2024, 11, 30), "corroborated"),  # a month after it ends
        (date(2024, 8, 31), "reported"),
        (date(2024, 12, 1), "reported"),
    ],
)
def test_the_window_is_one_month_either_side_of_the_period(published: date, expected: str) -> None:
    news = claim(
        occurred_from=published,
        occurred_to=published,
        time_precision="day",
        published_at=datetime(2025, 1, 15, tzinfo=UTC),
    )
    assert assess(news).evidence_state == expected


def test_a_claim_without_a_date_is_placed_by_its_publication_day() -> None:
    inside = claim(occurred_from=None, occurred_to=None, time_precision="unknown")
    assert assess(inside).evidence_state == "corroborated"
    outside = claim(
        occurred_from=None,
        occurred_to=None,
        time_precision="unknown",
        published_at=datetime(2025, 3, 1, tzinfo=UTC),
    )
    assert assess(outside).evidence_state == "reported"


def test_a_claim_only_as_precise_as_a_year_is_about_no_month() -> None:
    year = claim(time_precision="year", occurred_from=date(2024, 1, 1), occurred_to=None)
    assert assess(year).evidence_state == "reported"


def test_a_news_claim_from_an_unclustered_document_does_not_count() -> None:
    assert assess(claim(origin_id=None)).evidence_state == "reported"


def test_a_document_in_the_origin_of_the_measurement_is_not_independent() -> None:
    same = claim(origin_id=77)
    result = compute_price_change(_with_claims(with_origin(77), same))
    assert result.evidence_state == "reported"
    other = claim(origin_id=78)
    assert compute_price_change(_with_claims(with_origin(77), other)).evidence_state == (
        "corroborated"
    )


def test_a_copy_of_the_official_document_is_not_a_second_report() -> None:
    from dataclasses import replace

    copy = replace(claim(), copies_official=True)
    assert assess(copy).evidence_state == "reported"


def test_syndicated_copies_are_one_origin() -> None:
    copies = [claim(origin_id=900, evidence_document_id=2000 + i) for i in range(3)]
    result = assess(*copies)
    assert result.evidence_state == "corroborated"
    fact = next(f for f in result.facts if f["label"] == "Independent reports")
    assert fact["value"] == 1  # three documents, one story
    assert fact["evidence_ids"] == [2000, 2001, 2002]


def test_separate_origins_are_counted_separately() -> None:
    result = assess(claim(), claim(), claim())
    fact = next(f for f in result.facts if f["label"] == "Independent reports")
    assert fact["value"] == 3


def test_an_official_claim_in_the_same_direction_is_not_news() -> None:
    assert assess(official()).evidence_state == "reported"


def test_a_news_claim_in_the_opposite_direction_is_not_confirmation_but_is_noted() -> None:
    result = assess(claim(direction="down"))
    assert result.evidence_state == "reported"
    assert any("another way" in u or "other way" in u for u in result.unknowns)
    assert any(kind == "claim" for kind, _ in result.inputs)  # recorded, so a correction finds it


# --- disputed ----------------------------------------------------------------------------------


def test_an_official_claim_in_the_opposite_direction_disputes() -> None:
    disputing = official(direction="down")
    result = assess(disputing)
    assert result.evidence_state == "disputed"
    fact = next(f for f in result.facts if f["label"] == "Official statements that disagree")
    assert fact["value"] == 1 and fact["evidence_ids"] == [disputing.evidence_document_id]
    assert any("other way" in u for u in result.unknowns)
    assert ("claim", disputing.claim_id) in result.inputs


def test_disputed_wins_over_corroborated() -> None:
    result = assess(claim(), official(direction="down"))
    assert result.evidence_state == "disputed"


def test_a_company_announcement_counts_as_official() -> None:
    assert assess(official(source_kind="company", direction="down")).evidence_state == "disputed"


def test_an_official_claim_about_another_month_does_not_dispute() -> None:
    other_month = official(
        direction="down", occurred_from=date(2024, 9, 5), occurred_to=None, time_precision="day"
    )
    assert assess(other_month).evidence_state == "reported"


def test_unchanged_does_not_dispute_a_rise_or_fall() -> None:
    assert assess(official(direction="unchanged")).evidence_state == "reported"


def test_an_official_claim_in_the_origin_of_the_measurement_does_not_dispute_it() -> None:
    same = official(direction="down", origin_id=77)
    assert compute_price_change(_with_claims(with_origin(77), same)).evidence_state == "reported"


def test_disputing_needs_a_dated_moment_inside_the_period() -> None:
    year = official(direction="down", time_precision="year", occurred_from=date(2024, 1, 1))
    assert assess(year).evidence_state == "reported"


# --- insufficient beats the claims -------------------------------------------------------------


def test_insufficient_when_the_previous_month_is_missing_whatever_the_claims_say() -> None:
    result = assess(claim(), official(direction="down"), previous=None)
    assert result.evidence_state == "insufficient"
    assert result.severity == "none"
    assert not any(kind == "claim" for kind, _ in result.inputs)


def test_insufficient_when_the_latest_period_is_stale() -> None:
    late = datetime(2025, 6, 1, tzinfo=UTC)
    result = assess(claim(), now=late)
    assert result.evidence_state == "insufficient"


# --- possible factors --------------------------------------------------------------------------


def factors(*specs: FactorSpec):  # type: ignore[no-untyped-def]
    base = inputs()
    return base.__class__(**{**base.__dict__, "factors": specs})


SPECS = (
    FactorSpec("crude_price", "Crude oil price", ("crude oil", "brent")),
    FactorSpec("fx", "Exchange rate", ("exchange rate", "naira")),
)


def factor(result, code):  # type: ignore[no-untyped-def]
    return next(f for f in result.possible_factors if f["code"] == code)


def test_a_factor_is_supported_only_by_a_claim_that_names_it() -> None:
    mention = claim(text="Dealers blamed the higher crude oil price.", passage="higher crude oil")
    base = factors(*SPECS)
    result = compute_price_change(_with_claims(base, mention))
    assert factor(result, "crude_price")["status"] == "supported"
    assert factor(result, "crude_price")["evidence_ids"] == [mention.evidence_document_id]
    assert factor(result, "crude_price")["claim_ids"] == [mention.claim_id]
    assert factor(result, "fx")["status"] == "not_checked"
    assert ("claim", mention.claim_id) in result.inputs


def test_factor_keywords_match_whole_words_ignoring_case() -> None:
    base = factors(*SPECS)
    upper = claim(text="BRENT rose.", passage="BRENT rose")
    assert (
        factor(compute_price_change(_with_claims(base, upper)), "crude_price")["status"]
        == "supported"
    )
    part = claim(text="Nairaland users complained.", passage="Nairaland users")
    assert factor(compute_price_change(_with_claims(base, part)), "fx")["status"] == ("not_checked")


def test_a_factor_claim_outside_the_window_does_not_support() -> None:
    old = claim(
        text="Crude oil fell.",
        passage="Crude oil fell",
        occurred_from=date(2023, 1, 1),
        occurred_to=None,
        time_precision="day",
    )
    result = compute_price_change(_with_claims(factors(*SPECS), old))
    assert factor(result, "crude_price")["status"] == "not_checked"


def test_no_factor_is_supported_when_the_evidence_is_insufficient() -> None:
    mention = claim(text="Crude oil rose.", passage="Crude oil rose")
    base = factors(*SPECS)
    base = base.__class__(**{**base.__dict__, "previous": None})
    result = compute_price_change(_with_claims(base, mention))
    assert factor(result, "crude_price")["status"] == "not_checked"


# --- the hash ----------------------------------------------------------------------------------


def test_the_same_claims_give_the_same_hash_and_another_claim_changes_it() -> None:
    news = claim()
    assert assess(news).inputs_hash == assess(news).inputs_hash
    assert assess(news).inputs_hash != assess().inputs_hash
    assert assess(news, claim()).inputs_hash != assess(news).inputs_hash


def test_a_claim_moving_to_another_origin_changes_the_hash() -> None:
    from dataclasses import replace

    news = claim()
    assert assess(news).inputs_hash != assess(replace(news, origin_id=1)).inputs_hash


def _with_claims(base, *claims):  # type: ignore[no-untyped-def]
    return base.__class__(**{**base.__dict__, "claims": tuple(claims)})
