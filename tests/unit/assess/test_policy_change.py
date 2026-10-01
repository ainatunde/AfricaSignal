"""T2 policy change maths and evidence rules (spec B8.3, AS-027): one test per rule."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from africasignal.assess.policy_change import (
    TEMPLATE_VERSION,
    PolicyInputs,
    compute_policy_change,
)
from tests.unit.assess.claim_support import claim, official

D = Decimal
NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
SEP1, AUG1, JUL1 = date(2026, 9, 1), date(2026, 8, 1), date(2026, 7, 1)


def rate(
    value: str,
    effective: date | None = SEP1,
    *,
    published: datetime | None = None,
    precision: str = "month",
    **extra: object,
):  # type: ignore[no-untyped-def]
    """A primary-document claim giving the rate from an effective date."""
    published = published or datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
    return official(
        text=f"Band A tariff is N{value} per kWh.",
        passage=f"N{value}/kWh",
        stated_value=value,
        direction="unknown",
        occurred_from=effective,
        occurred_to=None,
        time_precision=precision if effective else "unknown",
        published_at=published,
        **extra,
    )


def report(
    value: str | None = None,
    *,
    text: str = "Customers are now being charged the new rate.",
    **extra: object,
):  # type: ignore[no-untyped-def]
    """A news claim about the series."""
    return claim(
        text=text,
        passage=text,
        stated_value=value,
        direction="unknown",
        published_at=datetime(2026, 9, 10, 9, 0, tzinfo=UTC),
        occurred_from=None,
        occurred_to=None,
        time_precision="unknown",
        **extra,
    )


def inputs(*claims, now: datetime = NOW, **kwargs):  # type: ignore[no-untyped-def]
    fields = dict(
        series_code="electricity_tariff_band_a:ikeja-electric",
        title="Electricity tariff, Band A, Ikeja Electric",
        unit="NGN/kWh",
        affected_groups="Band A customers of Ikeja Electric",
        materiality_pct=D("5"),
        place_code="NG-LA",
        place_name="Lagos",
        place_kind="state",
        claims=tuple(claims),
        now=now,
    )
    fields.update(kwargs)
    return PolicyInputs(**fields)  # type: ignore[arg-type]


def assess(*claims, **kwargs):  # type: ignore[no-untyped-def]
    result = compute_policy_change(inputs(*claims, **kwargs))
    assert result is not None
    return result


def fact(result, label):  # type: ignore[no-untyped-def]
    return next(f for f in result.facts if f["label"] == label)


# --- values and change -------------------------------------------------------------------------


def test_current_previous_and_change_are_computed_in_code() -> None:
    result = assess(rate("209.50", SEP1), rate("206.80", AUG1))
    assert fact(result, "Current rate")["value"] == 209.5
    assert fact(result, "Previous rate")["value"] == 206.8
    assert fact(result, "Increase in rate")["value"] == 2.7
    assert fact(result, "Change in rate")["value"] == 1.3  # rounded half-up to one decimal
    assert fact(result, "Change in rate")["unit"] == "%"
    assert result.change_pct == D("1.3")
    assert result.template_version == TEMPLATE_VERSION == "T2-2"


def test_a_fall_is_labelled_as_a_decrease_with_the_size_unsigned() -> None:
    result = assess(rate("190.00", SEP1), rate("200.00", AUG1))
    assert fact(result, "Decrease in rate")["value"] == 10.0
    assert fact(result, "Change in rate")["value"] == -5.0
    assert "fell 5.0%" in result.headline


def test_the_headline_attributes_the_statement_to_the_source() -> None:
    result = assess(rate("209.50", SEP1), rate("200.00", AUG1))
    assert result.headline == (
        "Electricity tariff, Band A, Ikeja Electric: NERC says the rate rose 4.8% "
        "to ₦209.50 per kWh from 1 September 2026"
    )
    assert result.scope_label == "Lagos State (Band A customers of Ikeja Electric)"
    assert result.period_label == "from 1 September 2026"


def test_an_unchanged_rate_says_so_and_has_no_increase_fact() -> None:
    result = assess(rate("200.00", SEP1), rate("200.00", AUG1))
    assert "unchanged at ₦200.00 per kWh" in result.headline
    assert not any(
        f["label"].endswith("in rate") and f["label"] != "Change in rate" for f in result.facts
    )
    assert result.severity == "none"


def test_with_one_rate_there_is_no_change_to_show() -> None:
    result = assess(rate("209.50", SEP1))
    assert result.change_pct is None
    assert result.headline == (
        "Electricity tariff, Band A, Ikeja Electric: the rate in force is ₦209.50 per kWh "
        "(NERC document of 28 August 2026)"
    )
    assert "The rate before this one is not available" in result.unknowns
    assert result.severity == "none"


def test_a_zero_previous_rate_gives_no_percentage() -> None:
    result = assess(rate("10.00", SEP1), rate("0.00", AUG1))
    assert result.change_pct is None
    assert any("previous rate is zero" in u for u in result.unknowns)


def test_the_latest_rate_in_force_wins_whatever_order_the_claims_arrive_in() -> None:
    claims = [rate("190.00", JUL1), rate("209.50", SEP1), rate("200.00", AUG1)]
    forward, backward = assess(*claims), assess(*reversed(claims))
    assert fact(forward, "Current rate")["value"] == 209.5
    assert fact(forward, "Previous rate")["value"] == 200.0
    assert forward.inputs_hash == backward.inputs_hash


def test_a_rate_that_takes_effect_later_is_announced_not_current() -> None:
    oct1 = date(2026, 10, 1)
    result = assess(rate("200.00", AUG1), rate("230.00", oct1))
    assert fact(result, "Current rate")["value"] == 200.0
    assert fact(result, "Announced rate")["value"] == 230.0
    assert fact(result, "Announced rate")["period"] == "from 1 October 2026"
    assert "A newer rate has been announced but is not in force yet" in result.unknowns


def test_only_an_announced_rate_is_insufficient() -> None:
    result = assess(rate("230.00", date(2026, 10, 1)))
    assert result.evidence_state == "insufficient" and result.severity == "none"
    assert result.has_primary_document
    assert "has announced a rate of ₦230.00 per kWh from 1 October 2026" in result.headline
    assert fact(result, "Announced rate")["evidence_ids"]


def test_documents_giving_the_same_rate_all_count_as_evidence() -> None:
    a, b = rate("209.50", SEP1), rate("209.50", SEP1)
    result = assess(a, b, rate("200.00", AUG1))
    assert fact(result, "Current rate")["evidence_ids"] == sorted(
        [a.evidence_document_id, b.evidence_document_id]
    )


def test_documents_that_disagree_on_a_date_use_the_newest_and_say_so() -> None:
    older = rate("205.00", SEP1, published=datetime(2026, 8, 20, tzinfo=UTC))
    newer = rate("209.50", SEP1, published=datetime(2026, 8, 28, tzinfo=UTC))
    result = assess(older, newer, rate("200.00", AUG1))
    assert fact(result, "Current rate")["value"] == 209.5
    assert any("different rates for the same date" in u for u in result.unknowns)
    assert fact(result, "Current rate")["evidence_ids"] == [newer.evidence_document_id]


# --- severity ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("new", "expected"),
    [
        ("204.00", "none"),  # 2 % is below the 5 % threshold
        ("210.00", "low"),  # 5 %
        ("220.00", "medium"),  # 10 %: twice the threshold
        ("240.00", "medium"),  # 20 %: exactly four times is still medium
        ("250.00", "high"),  # 25 %: above four times
    ],
)
def test_severity_follows_the_size_of_the_change(new: str, expected: str) -> None:
    result = assess(rate(new, SEP1), rate("200.00", AUG1))
    assert result.severity == expected
    assert result.material == (expected != "none")


def test_an_unknown_effective_date_reports_no_change_and_no_severity() -> None:
    result = assess(rate("300.00", None), rate("200.00", AUG1))
    assert result.severity == "none" and not result.material and result.change_pct is None
    assert (
        "rose" not in result.headline and "the rate in force is ₦300.00 per kWh" in result.headline
    )
    assert not any(f["label"].endswith("in rate") for f in result.facts)
    assert fact(result, "Previous rate")["value"] == 200.0  # still shown, with its own date
    assert any("date this rate takes effect is not stated" in u for u in result.unknowns)
    assert any("No change is reported" in u for u in result.unknowns)
    assert result.period_label == "effective date not stated"
    assert result.effective_from is None


def test_a_year_only_effective_date_counts_as_unknown() -> None:
    result = assess(rate("300.00", date(2026, 1, 1), precision="year"), rate("200.00", JUL1))
    assert result.severity == "none" and result.change_pct is None
    assert any("not stated" in u for u in result.unknowns)


def test_a_previous_rate_without_a_date_means_no_change_is_reported() -> None:
    result = assess(rate("300.00", SEP1), rate("200.00", None))
    assert result.change_pct is None and result.severity == "none"
    assert result.headline.startswith(
        "Electricity tariff, Band A, Ikeja Electric: the rate in force"
    )
    assert any("No change is reported" in u for u in result.unknowns)


def test_a_schedule_listing_an_old_rate_is_not_reported_as_a_recent_change() -> None:
    """The Ikeja case: a September 2026 schedule lists the rate for "August 2024 to September
    2026" after an earlier one for May to July 2024. The page must not say it rose from 1 August
    2024 as if that were news, and a 20 % rise two years ago must not carry a severity."""
    schedule = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
    result = assess(
        rate("240.00", date(2024, 8, 1), published=schedule),
        rate("200.00", date(2024, 5, 1), published=schedule),
    )
    assert result.headline == (
        "Electricity tariff, Band A, Ikeja Electric: the rate in force is ₦240.00 per kWh "
        "(NERC document of 17 September 2026)"
    )
    assert "rose" not in result.headline and "from 1 August 2024" not in result.headline
    assert result.change_pct is None and result.severity == "none" and not result.material
    assert [f["label"] for f in result.facts] == ["Current rate", "Previous rate"]
    assert fact(result, "Current rate")["period"] == "from 1 August 2024"  # the table's own date
    assert any("long before the document" in u for u in result.unknowns)


def test_a_change_just_inside_the_recent_window_is_still_reported() -> None:
    published = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
    inside = rate("240.00", date(2026, 5, 20), published=published)  # 120 days before
    outside = rate("240.00", date(2026, 5, 19), published=published)  # 121 days before
    assert (
        "rose 20.0%"
        in assess(inside, rate("200.00", date(2026, 4, 1), published=published)).headline
    )
    assert (
        "rose"
        not in assess(outside, rate("200.00", date(2026, 4, 1), published=published)).headline
    )


def test_the_threshold_comes_from_the_series() -> None:
    result = assess(rate("204.00", SEP1), rate("200.00", AUG1), materiality_pct=D("1"))
    assert result.material and result.severity == "medium"  # 2 % against a 1 % threshold


# --- evidence states ---------------------------------------------------------------------------


def test_a_primary_document_alone_is_reported_and_attributed() -> None:
    result = assess(rate("209.50", SEP1), rate("200.00", AUG1))
    assert result.evidence_state == "reported"
    assert "NERC says" in result.headline
    assert "No independent report that this rate is being applied" in result.unknowns
    assert result.has_primary_document


def test_news_that_the_rate_is_charged_corroborates() -> None:
    news = report()
    result = assess(rate("209.50", SEP1), rate("200.00", AUG1), news)
    assert result.evidence_state == "corroborated"
    implemented = fact(result, "Independent reports that the rate is applied")
    assert implemented["value"] == 1 and implemented["evidence_ids"] == [news.evidence_document_id]
    assert ("claim", news.claim_id) in result.inputs
    assert "No independent report that this rate is being applied" not in result.unknowns


def test_news_stating_the_current_value_corroborates_without_implementation_words() -> None:
    news = report("209.50", text="Ikeja Electric's Band A rate is N209.50 per kWh.")
    assert assess(rate("209.50", SEP1), rate("200.00", AUG1), news).evidence_state == (
        "corroborated"
    )


def test_news_stating_another_value_does_not_corroborate() -> None:
    news = report("206.80", text="Customers are now being charged N206.80 per kWh.")
    assert assess(rate("209.50", SEP1), rate("200.00", AUG1), news).evidence_state == "reported"


def test_news_without_a_value_or_implementation_wording_does_not_corroborate() -> None:
    news = report(text="The commission discussed the tariff on Tuesday.")
    assert assess(rate("209.50", SEP1), rate("200.00", AUG1), news).evidence_state == "reported"


def test_news_published_before_the_effective_date_is_not_implementation() -> None:
    early = report()
    early = replace(early, published_at=datetime(2026, 8, 30, tzinfo=UTC))
    assert assess(rate("209.50", SEP1), rate("200.00", AUG1), early).evidence_state == "reported"


def test_implementation_cannot_be_judged_without_an_effective_date() -> None:
    result = assess(rate("209.50", None), rate("200.00", AUG1), report())
    assert result.evidence_state == "reported"


def test_news_that_suspends_the_rate_is_not_implementation() -> None:
    news = report("209.50", text="The tariff was suspended after protests.")
    assert assess(rate("209.50", SEP1), rate("200.00", AUG1), news).evidence_state == "reported"


def test_news_from_the_origin_of_the_primary_document_is_not_independent() -> None:
    primary = rate("209.50", SEP1, origin_id=42)
    news = report(origin_id=42)
    assert assess(primary, rate("200.00", AUG1, origin_id=43), news).evidence_state == "reported"


def test_news_from_an_unclustered_document_does_not_corroborate() -> None:
    assert assess(rate("209.50"), rate("200.00", AUG1), report(origin_id=None)).evidence_state == (
        "reported"
    )


def test_a_reprint_of_the_official_text_does_not_corroborate() -> None:
    copy = replace(report(), copies_official=True)
    assert assess(rate("209.50"), rate("200.00", AUG1), copy).evidence_state == "reported"


def test_copies_of_one_story_are_one_origin() -> None:
    copies = [report(origin_id=900, evidence_document_id=2000 + i) for i in range(3)]
    result = assess(rate("209.50"), rate("200.00", AUG1), *copies)
    assert result.evidence_state == "corroborated"
    assert fact(result, "Independent reports that the rate is applied")["value"] == 1


def test_a_later_suspension_disputes() -> None:
    suspension = official(
        text="NERC suspends the new Band A tariff.",
        passage="NERC suspends the new Band A tariff",
        stated_value="209.50",
        published_at=datetime(2026, 9, 12, tzinfo=UTC),
        occurred_from=None,
        occurred_to=None,
        time_precision="unknown",
    )
    result = assess(rate("209.50", SEP1), rate("200.00", AUG1), suspension)
    assert result.evidence_state == "disputed"
    later = fact(result, "Later official statements that suspend or reverse it")
    assert later["value"] == 1 and later["evidence_ids"] == [suspension.evidence_document_id]
    assert ("claim", suspension.claim_id) in result.inputs
    # the suspension is not a rate: the figures are still the last published ones
    assert fact(result, "Current rate")["value"] == 209.5
    assert any("suspends or reverses" in u for u in result.unknowns)


def test_a_suspension_published_before_the_rate_is_history_not_a_dispute() -> None:
    old = official(
        text="NERC suspended the earlier tariff.",
        passage="suspended the earlier tariff",
        stated_value="190.00",
        published_at=datetime(2026, 7, 2, tzinfo=UTC),
    )
    result = assess(rate("209.50", SEP1), rate("200.00", AUG1), old)
    assert result.evidence_state == "reported"


def test_disputed_wins_over_corroborated() -> None:
    suspension = official(
        text="The tariff increase is reversed.",
        passage="increase is reversed",
        published_at=datetime(2026, 9, 15, tzinfo=UTC),
        occurred_from=None,
        occurred_to=None,
        time_precision="unknown",
    )
    result = assess(rate("209.50", SEP1), rate("200.00", AUG1), report(), suspension)
    assert result.evidence_state == "disputed"


# --- news only and nothing at all --------------------------------------------------------------


def test_news_only_is_insufficient_with_attributed_facts() -> None:
    news = report("209.50", text="Ikeja Electric's Band A rate is N209.50 per kWh.")
    result = assess(news)
    assert result.evidence_state == "insufficient" and result.severity == "none"
    assert not result.has_primary_document
    assert result.headline.endswith("no official document has been found yet")
    only = result.facts[0]
    assert only["label"].startswith("Reported by ") and only["value"] == 209.5
    assert only["evidence_ids"] == [news.evidence_document_id]
    assert any("No official document" in u for u in result.unknowns)


def test_news_without_a_value_leaves_nothing_to_show() -> None:
    assert compute_policy_change(inputs(report())) is None


def test_no_claims_leaves_nothing_to_show() -> None:
    assert compute_policy_change(inputs()) is None


def test_a_suspension_alone_leaves_nothing_to_show() -> None:
    suspension = official(text="NERC suspends the tariff.", passage="suspends the tariff")
    assert compute_policy_change(inputs(suspension)) is None


# --- validity and the hash ---------------------------------------------------------------------


def test_valid_for_45_days_after_the_latest_input() -> None:
    news = report()  # published 10 September, the latest input
    result = assess(rate("209.50", SEP1), rate("200.00", AUG1), news)
    assert result.valid_until == datetime(2026, 10, 25, 23, 59, 59, tzinfo=UTC)
    plain = assess(rate("209.50", SEP1), rate("200.00", AUG1))  # latest input: 28 August
    assert plain.valid_until == datetime(2026, 10, 12, 23, 59, 59, tzinfo=UTC)


def test_the_hash_ignores_claim_order_and_the_clock_and_tracks_the_inputs() -> None:
    a, b = rate("209.50", SEP1), rate("200.00", AUG1)
    first = assess(a, b)
    assert first.inputs_hash == assess(b, a).inputs_hash
    assert first.inputs_hash == assess(a, b, now=datetime(2026, 9, 21, tzinfo=UTC)).inputs_hash
    assert first.inputs_hash != assess(a, b, report()).inputs_hash
    assert first.inputs_hash != assess(a).inputs_hash
    assert first.inputs_hash != assess(a, b, place_code="NG-OG").inputs_hash


def test_every_fact_points_at_documents_the_assessment_used() -> None:
    result = assess(rate("209.50", SEP1), rate("200.00", AUG1), report())
    used = {i for kind, i in result.inputs if kind == "evidence_document"}
    assert all(f["evidence_ids"] and set(f["evidence_ids"]) <= used for f in result.facts)
    assert all(f["place_code"] == "NG-LA" for f in result.facts)
