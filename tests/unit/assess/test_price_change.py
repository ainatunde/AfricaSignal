"""T1 price change maths and rules (spec B8.2): one test per rule, on plain numbers."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from africasignal.assess import price_change as pc
from africasignal.assess.price_change import (
    FactorSpec,
    PriceInputs,
    PricePoint,
    StatePoint,
    compute_price_change,
    pct_change,
    severity_for,
)

D = Decimal
OCT = date(2024, 10, 1)
NOW = datetime(2024, 11, 25, 12, 0, tzinfo=UTC)  # a week after the October 2024 release
SOURCE = "Premium Motor Spirit (Petrol) Price Watch (October 2024)"


def point(mid: int, value: str, month: date = OCT, doc: int = 1) -> PricePoint:
    end = date(month.year + (month.month == 12), month.month % 12 + 1, 1)
    return PricePoint(
        measurement_id=mid,
        period_start=month,
        period_end=date.fromordinal(end.toordinal() - 1),
        value=D(value),
        evidence_document_id=doc,
        source_label=SOURCE,
    )


def inputs(
    current: str = "1080.95",
    previous: str | None = "1000.48",
    year_ago: str | None = "590.95",
    *,
    kind: str = "state",
    code: str = "NG-LA",
    name: str = "Lagos",
    now: datetime = NOW,
    states: tuple[StatePoint, ...] = (),
    states_before: tuple[StatePoint, ...] = (),
    mom_threshold: str = "5",
    yoy_threshold: str = "20",
    month: date = OCT,
) -> PriceInputs:
    prev_month, ago_month = date(2024, 9, 1), date(2023, 10, 1)
    return PriceInputs(
        item_code="pms_litre",
        item_label="petrol (PMS)",
        unit="NGN/litre",
        mom_threshold_pct=D(mom_threshold),
        yoy_threshold_pct=D(yoy_threshold),
        factors=(FactorSpec("crude_price", "Crude oil price"), FactorSpec("fx", "Exchange rate")),
        place_code=code,
        place_name=name,
        place_kind="country" if kind == "country" else "state",
        source_short="NBS",
        current=point(1, current, month),
        previous=point(2, previous, prev_month, doc=2) if previous is not None else None,
        year_ago=point(3, year_ago, ago_month, doc=3) if year_ago is not None else None,
        now=now,
        states_current=states,
        states_previous=states_before,
    )


# --- maths -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "base", "expected"),
    [
        ("105.25", "100", "5.3"),  # 5.25 rounds half up, not to even
        ("105.15", "100", "5.2"),  # 5.15 rounds half up
        ("94.75", "100", "-5.3"),  # half away from zero for falls too
        ("100", "100", "0.0"),
        ("1080.95", "1000.48", "8.0"),  # Lagos petrol, October 2024 against September
        ("1080.95", "590.95", "82.9"),  # and against October 2023
        ("1184.83", "1030.46", "15.0"),  # national petrol: NBS states 14.98 %
        ("1184.83", "630.63", "87.9"),  # NBS states 87.88 %
    ],
)
def test_percentage_change_is_rounded_half_up_to_one_decimal(
    value: str, base: str, expected: str
) -> None:
    assert pct_change(D(value), D(base)) == D(expected)


def test_a_result_computed_from_the_headline_figures_agrees_with_nbs_to_one_decimal() -> None:
    # NBS summary text, October 2024 petrol: +14.98 % on the month and +87.88 % on the year.
    assert pct_change(D("1184.83"), D("1030.46")) == D("14.98").quantize(D("0.1"))
    assert pct_change(D("1184.83"), D("630.63")) == D("87.88").quantize(D("0.1"))


def test_changes_are_computed_for_both_horizons() -> None:
    result = compute_price_change(inputs())
    assert (result.mom_pct, result.yoy_pct) == (D("8.0"), D("82.9"))
    by_label = {f["label"]: f for f in result.facts}
    assert by_label["Month-on-month change"]["value"] == 8.0
    assert by_label["Year-on-year change"]["value"] == 82.9
    assert by_label["Current price"]["value"] == 1080.95


# --- materiality and severity ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("current", "material", "severity"),
    [
        ("104.9", False, "none"),  # +4.9 % month on month, year on year level
        ("105.0", True, "low"),  # exactly the 5 % threshold is material
        ("109.9", True, "low"),  # ratio 1.98
        ("110.0", True, "medium"),  # ratio 2.0
        ("120.0", True, "medium"),  # ratio 4.0 is still medium
        ("120.1", True, "high"),  # above 4x
        ("95.0", True, "low"),  # falls count the same as rises
        ("80.0", True, "medium"),
        ("79.9", True, "high"),
    ],
)
def test_materiality_and_severity_follow_the_month_on_month_threshold(
    current: str, material: bool, severity: str
) -> None:
    # A flat year on year so that only the month-on-month change matters.
    result = compute_price_change(inputs(current, "100", current))
    assert (result.material, result.severity) == (material, severity)


@pytest.mark.parametrize(
    ("year_ago", "yoy", "material", "severity"),
    [
        ("100", "19.9", False, "none"),  # current 119.9, flat on the month
        ("99.9", "20.0", True, "low"),  # 119.9 / 99.9 = +20.02 %: the threshold
        ("85", "41.1", True, "medium"),  # ratio 2.06
        ("50", "139.8", True, "high"),  # ratio 6.99
        ("150", "-20.1", True, "low"),  # a year-on-year fall counts too
    ],
)
def test_year_on_year_threshold_is_20_percent_by_default(
    year_ago: str, yoy: str, material: bool, severity: str
) -> None:
    current = "119.9"  # flat month on month, so only the year-on-year change matters
    result = compute_price_change(inputs(current, current, year_ago))
    assert result.mom_pct == D("0.0")
    assert (result.yoy_pct, result.material, result.severity) == (D(yoy), material, severity)


def test_severity_uses_the_larger_of_the_two_ratios() -> None:
    # month on month +6 % (ratio 1.2) but year on year +100 % (ratio 5): high
    result = compute_price_change(inputs("106", "100", "53"))
    assert (result.mom_pct, result.yoy_pct, result.severity) == (D("6.0"), D("100.0"), "high")


def test_thresholds_come_from_the_item() -> None:
    lenient = compute_price_change(inputs("110", "100", "110", mom_threshold="20"))
    strict = compute_price_change(inputs("110", "100", "110", mom_threshold="2"))
    assert (lenient.material, lenient.severity) == (False, "none")
    assert (strict.material, strict.severity) == (True, "high")


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        (None, "none"),
        (D("0.99"), "none"),
        (D("1"), "low"),
        (D("1.99"), "low"),
        (D("2"), "medium"),
        (D("4"), "medium"),
        (D("4.01"), "high"),
    ],
)
def test_severity_bands(ratio: Decimal | None, expected: str) -> None:
    assert severity_for(ratio) == expected


# --- evidence state ----------------------------------------------------------------------------


def test_the_official_measurement_alone_is_reported() -> None:
    assert compute_price_change(inputs()).evidence_state == "reported"


def test_a_missing_previous_month_is_insufficient_and_has_no_severity() -> None:
    result = compute_price_change(inputs(previous=None, year_ago="500"))
    assert (result.evidence_state, result.severity) == ("insufficient", "none")
    assert result.mom_pct is None and result.yoy_pct is not None
    assert "The value for September 2024 is not available" in result.unknowns


@pytest.mark.parametrize(
    ("days_after_period_end", "state"),
    [(120, "reported"), (121, "insufficient")],
)
def test_a_latest_period_more_than_120_days_old_is_insufficient(
    days_after_period_end: int, state: str
) -> None:
    period_end = date(2024, 10, 31)
    now = datetime.fromordinal(period_end.toordinal() + days_after_period_end).replace(tzinfo=UTC)
    result = compute_price_change(inputs(now=now))
    assert result.evidence_state == state
    assert (result.severity == "none") is (state == "insufficient")
    assert any("more than 120 days ago" in u for u in result.unknowns) is (state == "insufficient")


def test_nbs_data_from_october_2024_is_insufficient_today() -> None:
    """The newest price watch NBS lists is October 2024; on 30 September 2026 that is stale."""
    result = compute_price_change(inputs(now=datetime(2026, 9, 30, tzinfo=UTC)))
    assert result.evidence_state == "insufficient" and result.severity == "none"


def test_valid_until_is_75_days_after_the_period_ends() -> None:
    result = compute_price_change(inputs())
    assert result.period_end == date(2024, 10, 31)
    assert result.valid_until == datetime(2025, 1, 14, 23, 59, 59, tzinfo=UTC)
    assert result.last_checked_at == NOW


# --- headline ----------------------------------------------------------------------------------


def test_headline_for_a_rise() -> None:
    assert compute_price_change(inputs()).headline == (
        "Average petrol (PMS) price in Lagos State rose 8.0% in October 2024 to ₦1,080.95 (NBS)"
    )


def test_headline_for_a_fall_uses_the_absolute_change() -> None:
    assert compute_price_change(inputs("950", "1000", "900")).headline == (
        "Average petrol (PMS) price in Lagos State fell 5.0% in October 2024 to ₦950.00 (NBS)"
    )


def test_headline_when_unchanged() -> None:
    assert compute_price_change(inputs("1000.04", "1000", "900")).headline == (
        "Average petrol (PMS) price in Lagos State was unchanged in October 2024 at ₦1,000.04 (NBS)"
    )


def test_headline_without_a_previous_month_says_so() -> None:
    assert compute_price_change(inputs(previous=None)).headline == (
        "Average petrol (PMS) price in Lagos State was ₦1,080.95 in October 2024 (NBS); "
        "there is no previous month to compare with"
    )


def test_headline_names_the_country_and_the_fct_naturally() -> None:
    national = compute_price_change(inputs(kind="country", code="NG", name="Nigeria"))
    assert "price in Nigeria rose" in national.headline
    fct = compute_price_change(inputs(code="NG-FC", name="Federal Capital Territory"))
    assert "price in the Federal Capital Territory rose" in fct.headline
    assert fct.scope_label == "Federal Capital Territory (state average, NBS)"


def test_scope_and_period_labels() -> None:
    state = compute_price_change(inputs())
    assert state.scope_label == "Lagos State (state average, NBS)"
    assert state.period_label == "October 2024"
    national = compute_price_change(inputs(kind="country", code="NG", name="Nigeria"))
    assert national.scope_label == "Nigeria (national average, NBS)"


def test_headline_large_values_use_thousands_separators() -> None:
    result = compute_price_change(inputs("16841.25", "16427.78", "9071.05"))
    assert "to ₦16,841.25 (NBS)" in result.headline


# --- national aggregates -----------------------------------------------------------------------


def _states(values: dict[str, str], month: date, start_id: int) -> tuple[StatePoint, ...]:
    return tuple(
        StatePoint(f"NG-{name[:2].upper()}", name, point(start_id + i, v, month, doc=10))
        for i, (name, v) in enumerate(values.items())
    )


def national(now: datetime = NOW) -> pc.PriceAssessment:
    before = {"Abia": "100", "Benue": "100", "Delta": "100", "Edo": "100", "Gombe": "100",
              "Imo": "100", "Kano": "100", "Lagos": "100"}  # fmt: skip
    after = {"Abia": "110", "Benue": "100.5", "Delta": "100.51", "Edo": "99.5", "Gombe": "99.49",
             "Imo": "150", "Kano": "100", "Lagos": "90"}  # fmt: skip
    return compute_price_change(
        inputs(
            kind="country",
            code="NG",
            name="Nigeria",
            now=now,
            states=_states(after, OCT, 100),
            states_before=_states(before, date(2024, 9, 1), 200),
        )  # fmt: skip
    )


def facts(result: pc.PriceAssessment) -> dict[str, dict[str, object]]:
    return {f["label"]: f for f in result.facts}


def test_national_situation_gets_the_median_of_state_values() -> None:
    # sorted: 90, 99.49, 99.5, 100, 100.5, 100.51, 110, 150 -> median (100 + 100.5) / 2
    assert facts(national())["Median of state averages"]["value"] == 100.25


def test_states_up_down_and_unchanged_use_a_half_percent_band_inclusive() -> None:
    f = facts(national())
    # Abia +10 % up; Benue +0.5 % unchanged (inclusive); Delta +0.51 % up; Edo -0.5 % unchanged;
    # Gombe -0.51 % down; Imo +50 % up; Kano 0 % unchanged; Lagos -10 % down.
    assert (f["States up"]["value"], f["States down"]["value"]) == (3, 2)
    assert f["States unchanged"]["value"] == 3


def test_three_highest_and_lowest_states_are_listed_in_order() -> None:
    f = facts(national())
    assert [s["place"] for s in f["Highest states"]["value"]] == ["Imo", "Abia", "Delta"]  # type: ignore[attr-defined]
    assert [s["place"] for s in f["Lowest states"]["value"]] == ["Lagos", "Gombe", "Edo"]  # type: ignore[attr-defined]


def test_ties_between_states_are_broken_by_name_so_output_is_stable() -> None:
    tied = {"Zamfara": "100", "Abia": "100", "Kano": "100", "Niger": "100"}
    result = compute_price_change(
        inputs(kind="country", code="NG", name="Nigeria", states=_states(tied, OCT, 100))
    )
    lowest = facts(result)["Lowest states"]["value"]
    assert [s["place"] for s in lowest] == ["Abia", "Kano", "Niger"]  # type: ignore[attr-defined]


def test_state_situations_have_no_aggregates_and_country_without_states_has_none_either() -> None:
    assert "Median of state averages" not in facts(compute_price_change(inputs()))
    bare = compute_price_change(inputs(kind="country", code="NG", name="Nigeria"))
    assert "Median of state averages" not in facts(bare)


def test_a_state_missing_last_month_is_left_out_of_the_up_down_counts() -> None:
    result = compute_price_change(
        inputs(
            kind="country",
            code="NG",
            name="Nigeria",
            states=_states({"Abia": "110", "Benue": "120"}, OCT, 100),
            states_before=_states({"Abia": "100"}, date(2024, 9, 1), 200),
        )  # fmt: skip
    )
    f = facts(result)
    assert (f["States up"]["value"], f["States down"]["value"], f["States unchanged"]["value"]) == (
        1,
        0,
        0,
    )


# --- facts, unknowns, factors, inputs ----------------------------------------------------------


def test_every_fact_points_at_evidence_and_carries_its_place_and_period() -> None:
    for result in (compute_price_change(inputs()), national()):
        for fact in result.facts:
            assert fact["evidence_ids"], fact["label"]
            assert {"label", "value", "unit", "period", "place_code", "source_label"} <= set(fact)
    changes = facts(compute_price_change(inputs()))
    assert changes["Month-on-month change"]["evidence_ids"] == [1, 2]
    assert changes["Year-on-year change"]["evidence_ids"] == [1, 3]


def test_unknowns_for_a_state_with_everything_present() -> None:
    assert compute_price_change(inputs()).unknowns == [
        "NBS averages are state-wide and may differ from prices in your LGA",
        "No independent report for this state and month",
    ]


def test_unknown_when_last_years_value_is_missing() -> None:
    result = compute_price_change(inputs(year_ago=None))
    assert "Last year's value is not available" in result.unknowns
    assert result.yoy_pct is None and result.mom_pct == D("8.0")


def test_unknowns_for_the_national_average() -> None:
    result = national()
    assert any("publishes a national average" in u for u in result.unknowns)
    assert "No independent report for Nigeria and month" in result.unknowns


def test_possible_factors_are_listed_but_never_supported_without_a_claim() -> None:
    result = compute_price_change(inputs())
    assert result.possible_factors == [
        {
            "factor": "Crude oil price",
            "code": "crude_price",
            "status": "not_checked",
            "evidence_ids": [],
        },
        {"factor": "Exchange rate", "code": "fx", "status": "not_checked", "evidence_ids": []},
    ]


def test_inputs_list_every_measurement_and_document_used() -> None:
    result = compute_price_change(inputs())
    assert result.inputs == [
        ("measurement", 1), ("measurement", 2), ("measurement", 3),
        ("evidence_document", 1), ("evidence_document", 2), ("evidence_document", 3),
    ]  # fmt: skip
    wide = national()
    kinds = {k for k, _ in wide.inputs}
    assert kinds == {"measurement", "evidence_document"}
    assert len([1 for k, _ in wide.inputs if k == "measurement"]) == 3 + 8 + 8


# --- inputs_hash -------------------------------------------------------------------------------


def test_same_inputs_give_the_same_hash_whatever_the_time() -> None:
    a = compute_price_change(inputs())
    b = compute_price_change(inputs(now=datetime(2026, 9, 30, tzinfo=UTC)))
    assert a.inputs_hash == b.inputs_hash and len(a.inputs_hash) == 64


def test_a_changed_value_changes_the_hash() -> None:
    assert (
        compute_price_change(inputs()).inputs_hash
        != compute_price_change(inputs("1080.96")).inputs_hash
    )


def test_a_new_measurement_id_with_the_same_value_changes_the_hash() -> None:
    """A restatement that repeats a value is still a different input row."""
    base = inputs()
    other = PriceInputs(**{**base.__dict__, "previous": point(99, "1000.48", date(2024, 9, 1), 2)})
    assert compute_price_change(base).inputs_hash != compute_price_change(other).inputs_hash


def test_the_hash_does_not_depend_on_the_order_of_states() -> None:
    a = national()
    # the same states, reversed
    base = inputs(kind="country", code="NG", name="Nigeria")
    reversed_inputs = PriceInputs(
        **{
            **base.__dict__,
            "states_current": tuple(reversed(_states({"Abia": "110", "Benue": "100.5"}, OCT, 100))),
        }
    )
    forward = PriceInputs(
        **{**base.__dict__, "states_current": _states({"Abia": "110", "Benue": "100.5"}, OCT, 100)}
    )
    assert (
        compute_price_change(reversed_inputs).inputs_hash
        == compute_price_change(forward).inputs_hash
    )
    assert a.inputs_hash != compute_price_change(forward).inputs_hash


def test_a_new_template_version_changes_the_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    before = compute_price_change(inputs()).inputs_hash
    monkeypatch.setattr(pc, "TEMPLATE_VERSION", "T1-3")
    assert compute_price_change(inputs()).inputs_hash != before


def test_the_item_and_place_are_part_of_the_hash() -> None:
    assert (
        compute_price_change(inputs()).inputs_hash
        != compute_price_change(inputs(code="NG-OG", name="Ogun")).inputs_hash
    )
