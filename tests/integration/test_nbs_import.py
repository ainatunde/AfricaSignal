"""Importing real NBS workbooks into measurements (AS-010), against PostgreSQL.

The revision, range and conflict cases use copies of a real file with one cell edited, because the
real September and October 2024 files hold no restatement (see the parser tests).
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from africasignal.catalog import load_items
from africasignal.models import (
    EvidenceDocument,
    Measurement,
    MeasurementReview,
    Place,
    ReportingOrigin,
    Series,
    Source,
)
from africasignal.sources.nbs import RANGE_HIGH, RANGE_LOW, _in_range, import_workbook
from africasignal.sources.nbs_workbook import NbsParseError
from africasignal.storage import S3Store
from tests.integration.nbs_support import (
    STATES,
    add_places,
    add_source,
    current_values,
    import_bytes,
    make_store,
    release,
    release_date,
)
from tests.unit.sources.nbs_fixtures import edit

D = Decimal
PMS_SEP, PMS_OCT = "FUEL_SEPT_2024_REPORT.xlsx", "PMS_OCT_2024_REPORT.xlsx"
PMS_SHEET = "Fuel_October 2024"
LAGOS_ROW = 40  # Lagos in the October petrol sheet: B year ago, C previous month, D reference


@pytest.fixture
def store() -> Iterator[S3Store]:
    yield from make_store()


@pytest.fixture
def places(session: Session) -> dict[str, int]:
    return add_places(session)


@pytest.fixture
def source(session: Session, places: dict[str, int]) -> Source:
    return add_source(session)


def _count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def _rows(session: Session, item: str, place: str | None = None) -> list[Measurement]:
    query = (
        select(Measurement)
        .join(Series, Series.id == Measurement.series_id)
        .join(Place, Place.id == Measurement.place_id)
        .where(Series.item_code == item)
        .order_by(Measurement.period_start, Measurement.vintage)
    )
    if place:
        query = query.where(Place.code == place)
    return list(session.scalars(query))


# One state table has 37 states + the national row = 38 places, each with three months.
STATE_ITEM_ROWS = 38 * 3
# file -> the items it fills and how many measurements each gets on a first import
EXPECTED = {
    "FUEL_SEPT_2024_REPORT.xlsx": {"pms_litre": STATE_ITEM_ROWS},
    "PMS_OCT_2024_REPORT.xlsx": {"pms_litre": STATE_ITEM_ROWS},
    "DIESEL_SEPT_2024_REPORT.xlsx": {"ago_litre": STATE_ITEM_ROWS},
    "DIESEL_OCT_2024_REPORT.xlsx": {"ago_litre": STATE_ITEM_ROWS},
    "HOUSEHOLD_KEROSENE_SEPT_2024.xlsx": {"dpk_litre": STATE_ITEM_ROWS},
    "HOUSEHOLD_KEROSENE_OCT_2024.xlsx": {"dpk_litre": STATE_ITEM_ROWS},
    "GAS_PRICE_WATCH_SEPT_2024.xlsx": {"lpg_5kg": STATE_ITEM_ROWS, "lpg_12_5kg": STATE_ITEM_ROWS},
    "GAS_PRICE_WATCH_OCT_2024.xlsx": {"lpg_5kg": STATE_ITEM_ROWS, "lpg_12_5kg": STATE_ITEM_ROWS},
    # national only: four items x three months
    "selected_food_sept_2024.xlsx": {
        "rice_local_1kg": 3, "garri_white_1kg": 3, "beans_brown_1kg": 3, "maize_white_1kg": 3,
    },
    "selected_food_oct_2024.xlsx": {
        "rice_local_1kg": 3, "garri_white_1kg": 3, "beans_brown_1kg": 3, "maize_white_1kg": 3,
    },
}  # fmt: skip


@pytest.mark.parametrize("file", EXPECTED)
def test_importing_a_real_file_creates_measurements_for_every_state_and_item_in_it(
    session: Session, store: S3Store, source: Source, file: str
) -> None:
    _, result = import_bytes(session, store, source, file)
    assert result.unresolved_places == [] and result.queued_for_review == 0
    assert result.rejected == 0 and result.revised == 0 and result.unchanged == 0
    assert result.inserted == sum(EXPECTED[file].values())
    for item, rows in EXPECTED[file].items():
        assert len(_rows(session, item)) == rows, item
    if any(k.startswith(("pms", "ago", "dpk", "lpg")) for k in EXPECTED[file]):
        item = next(iter(EXPECTED[file]))
        month = date(2024, 9 if "SEPT" in file else 10, 1)
        reference = [m for m in _rows(session, item) if m.period_start == month]
        assert len(reference) == 38  # 37 states and the country
        assert {m.place_id for m in reference} == {
            p for (p,) in session.execute(select(Place.id).where(Place.kind != "lga"))
        }


def test_untracked_blocks_and_items_are_not_imported(
    session: Session, store: S3Store, source: Source
) -> None:
    import_bytes(session, store, source, "HOUSEHOLD_KEROSENE_OCT_2024.xlsx")
    import_bytes(session, store, source, "selected_food_oct_2024.xlsx")
    codes = set(session.scalars(select(Series.item_code)))
    assert codes == {
        "dpk_litre",
        "rice_local_1kg",
        "garri_white_1kg",
        "beans_brown_1kg",
        "maize_white_1kg",
    }


def test_series_carry_the_unit_topic_and_source_from_items_yaml(
    session: Session, store: S3Store, source: Source
) -> None:
    for file in ("GAS_PRICE_WATCH_OCT_2024.xlsx", "selected_food_oct_2024.xlsx"):
        import_bytes(session, store, source, file)
    catalog = load_items()
    for series in session.scalars(select(Series)):
        item = catalog.item(series.item_code)
        assert (series.unit, series.topic, series.currency) == (item.unit, item.topic, "NGN")
        assert series.source_id == source.id and series.frequency == "monthly"
    units = {s.item_code: s.unit for s in session.scalars(select(Series))}
    assert units["lpg_5kg"] == "NGN/5kg" and units["lpg_12_5kg"] == "NGN/12.5kg"


def test_values_match_what_nbs_states_in_its_summary_text(
    session: Session, store: S3Store, source: Source
) -> None:
    import_bytes(session, store, source, PMS_OCT)
    import_bytes(session, store, source, "GAS_PRICE_WATCH_OCT_2024.xlsx")
    values = current_values(session)
    oct24, sep24, oct23 = date(2024, 10, 1), date(2024, 9, 1), date(2023, 10, 1)
    # NBS: petrol averaged N1184.83 in October 2024, N1030.46 in September, N630.63 a year earlier
    assert values[("pms_litre", "NG", oct24)] == D("1184.83")
    assert values[("pms_litre", "NG", sep24)] == D("1030.46")
    assert values[("pms_litre", "NG", oct23)] == D("630.63")
    # NBS: a 5kg cooking gas refill averaged N6,915.69 (N6,699.63 in September)
    assert values[("lpg_5kg", "NG", oct24)] == D("6915.69")
    assert values[("lpg_5kg", "NG", sep24)] == D("6699.63")
    # the state rows land on the right places, including "Abuja" = the FCT
    assert values[("pms_litre", "NG-LA", oct24)] == D("1080.95")
    assert values[("pms_litre", "NG-FC", oct24)] == D("1124.61")


def test_the_mislabelled_kebbi_row_lands_on_kebbi_not_taraba(
    session: Session, store: S3Store, source: Source
) -> None:
    _, result = import_bytes(session, store, source, "GAS_PRICE_WATCH_OCT_2024.xlsx")
    values = current_values(session)
    oct24 = date(2024, 10, 1)
    assert values[("lpg_12_5kg", "NG-KE", oct24)] == D("16397.50")
    assert values[("lpg_12_5kg", "NG-TA", oct24)] == D("16992.19")
    assert any("'Taraba'" in n and "'Kebbi'" in n for n in result.notes)


def test_reimporting_the_same_file_creates_nothing_new(
    session: Session, store: S3Store, source: Source
) -> None:
    doc, first = import_bytes(session, store, source, PMS_OCT)
    before = _count(session, Measurement)
    doc_again, second = import_bytes(session, store, source, PMS_OCT)
    assert doc_again.id == doc.id  # the same bytes at the same URL are the same evidence
    assert _count(session, Measurement) == before == first.inserted
    assert (second.inserted, second.revised, second.historic) == (0, 0, 0)
    assert second.unchanged == first.inserted
    assert _count(session, EvidenceDocument) == 1 and _count(session, ReportingOrigin) == 1


def test_october_adds_only_new_months_because_it_repeats_septembers_values(
    session: Session, store: S3Store, source: Source
) -> None:
    _, sep = import_bytes(session, store, source, PMS_SEP)
    _, oct_ = import_bytes(session, store, source, PMS_OCT)
    assert sep.inserted == 114
    # October repeats September's 38 values and brings 38 for October and 38 for October 2023
    assert (oct_.inserted, oct_.unchanged, oct_.revised) == (76, 38, 0)
    assert len(_rows(session, "pms_litre")) == 190
    assert not [m for m in _rows(session, "pms_litre") if m.superseded_by_id]


def test_import_order_does_not_change_the_current_values(
    session: Session, store: S3Store, source: Source
) -> None:
    import_bytes(session, store, source, PMS_SEP)
    import_bytes(session, store, source, PMS_OCT)
    forwards = current_values(session)
    for m in session.scalars(select(Measurement)):
        session.delete(m)
    session.flush()
    import_bytes(session, store, source, PMS_OCT)
    import_bytes(session, store, source, PMS_SEP)
    assert current_values(session) == forwards


# --- revisions ---------------------------------------------------------------------------------


def _restated_october(previous: str = "1010.48") -> bytes:
    """The real October petrol file with Lagos' September value (the previous-month column)
    changed from 1000.48: what a restatement in a later release would look like."""
    return edit(PMS_OCT, PMS_SHEET, {f"C{LAGOS_ROW}": float(previous)})


def test_a_restated_month_creates_a_new_vintage_and_supersedes_the_old_row(
    session: Session, store: S3Store, source: Source, places: dict[str, int]
) -> None:
    import_bytes(session, store, source, PMS_SEP)  # released 2024-10-17: Lagos September 1000.48
    _, result = import_bytes(session, store, source, PMS_OCT, _restated_october())

    lagos_sep = [
        m for m in _rows(session, "pms_litre", "NG-LA") if m.period_start == date(2024, 9, 1)
    ]
    old, new = lagos_sep
    assert (old.value, old.vintage) == (D("1000.48"), date(2024, 10, 17))
    assert (new.value, new.vintage) == (D("1010.48"), date(2024, 11, 19))
    assert old.superseded_by_id == new.id and new.superseded_by_id is None
    assert new.evidence_document_id != old.evidence_document_id
    assert result.revised == 1
    assert ("pms_litre", places["NG-LA"]) in result.touched
    assert any("revised to 1010.48" in n for n in result.notes)
    assert current_values(session)[("pms_litre", "NG-LA", date(2024, 9, 1))] == D("1010.48")


def test_a_revision_imported_before_the_older_release_still_ends_up_current(
    session: Session, store: S3Store, source: Source
) -> None:
    import_bytes(session, store, source, PMS_OCT, _restated_october())
    _, result = import_bytes(session, store, source, PMS_SEP)  # older release, older value
    assert result.historic == 1 and result.revised == 0
    old, new = [
        m for m in _rows(session, "pms_litre", "NG-LA") if m.period_start == date(2024, 9, 1)
    ]
    assert (old.value, new.value) == (D("1000.48"), D("1010.48"))
    assert old.superseded_by_id == new.id and new.superseded_by_id is None
    assert current_values(session)[("pms_litre", "NG-LA", date(2024, 9, 1))] == D("1010.48")


def test_a_different_value_in_the_same_release_is_reported_and_not_stored(
    session: Session, store: S3Store, source: Source
) -> None:
    import_bytes(session, store, source, PMS_OCT)
    _, result = import_bytes(session, store, source, PMS_OCT, _restated_october())
    assert result.revised == 0 and result.inserted == 0
    assert any("already holds a different value" in n for n in result.notes)
    assert current_values(session)[("pms_litre", "NG-LA", date(2024, 9, 1))] == D("1000.48")


def test_a_later_vintage_with_an_unchanged_value_is_not_a_revision(
    session: Session, store: S3Store, source: Source
) -> None:
    import_bytes(session, store, source, PMS_OCT)
    _, result = import_bytes(session, store, source, PMS_OCT, vintage=date(2024, 12, 1))
    assert (result.inserted, result.revised, result.historic) == (0, 0, 0)
    assert result.unchanged == 114


# --- validation --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "baseline", "ok"),
    [
        ("200", "1000", True),  # exactly 0.2x
        ("199.99", "1000", False),
        ("5000", "1000", True),  # exactly 5x
        ("5000.01", "1000", False),
        ("1000", None, True),  # nothing to compare with
        ("1000", "0", True),
    ],
)
def test_range_is_inclusive_of_0_2x_and_5x(value: str, baseline: str | None, ok: bool) -> None:
    assert (RANGE_LOW, RANGE_HIGH) == (D("0.2"), D("5"))
    assert _in_range(D(value), D(baseline) if baseline is not None else None) is ok


def test_a_value_far_from_the_previous_months_median_goes_to_the_review_queue(
    session: Session, store: S3Store, source: Source
) -> None:
    misplaced = edit(PMS_OCT, PMS_SHEET, {f"D{LAGOS_ROW}": 10809.5})  # 1080.95 with a slipped dot
    doc, result = import_bytes(session, store, source, PMS_OCT, misplaced)
    assert result.queued_for_review == 1
    assert not [
        m for m in _rows(session, "pms_litre", "NG-LA") if m.period_start == date(2024, 10, 1)
    ]
    (review,) = session.scalars(select(MeasurementReview))
    assert review.value == D("10809.50") and review.status == "pending"
    assert review.evidence_document_id == doc.id and review.vintage == date(2024, 11, 19)
    assert review.reference_value == D("1025.00")  # median of the 37 September state values
    assert "outside 0.2x to 5x" in review.reason
    assert any("queued for review" in n for n in result.notes)
    # The other 113 values were stored; September and October 2023 for Lagos are fine.
    assert result.inserted == 113


def test_a_queued_value_is_not_queued_twice_on_reimport(
    session: Session, store: S3Store, source: Source
) -> None:
    misplaced = edit(PMS_OCT, PMS_SHEET, {f"D{LAGOS_ROW}": 10809.5})
    import_bytes(session, store, source, PMS_OCT, misplaced)
    _, again = import_bytes(session, store, source, PMS_OCT, misplaced)
    assert again.queued_for_review == 0
    assert _count(session, MeasurementReview) == 1


def test_too_small_values_are_queued_too(session: Session, store: S3Store, source: Source) -> None:
    tiny = edit(PMS_OCT, PMS_SHEET, {f"D{LAGOS_ROW}": 108.09})
    _, result = import_bytes(session, store, source, PMS_OCT, tiny)
    assert result.queued_for_review == 1


def test_food_items_are_checked_against_their_own_previous_month(
    session: Session, store: S3Store, source: Source
) -> None:
    rice_row = 35  # "Rice local sold loose": B year ago, C previous, D reference
    bad = edit("selected_food_oct_2024.xlsx", "Selected Food oct 2024", {f"D{rice_row}": 19446.4})
    _, result = import_bytes(session, store, source, "selected_food_oct_2024.xlsx", bad)
    assert result.queued_for_review == 1
    (review,) = session.scalars(select(MeasurementReview))
    assert review.reference_value == D("1914.77")


@pytest.mark.parametrize("bad", [0, -5])
def test_values_that_are_not_positive_are_rejected_not_stored(
    session: Session, store: S3Store, source: Source, bad: int
) -> None:
    edited = edit(PMS_OCT, PMS_SHEET, {f"D{LAGOS_ROW}": bad})
    _, result = import_bytes(session, store, source, PMS_OCT, edited)
    assert result.rejected == 1 and result.queued_for_review == 0
    assert any("is not positive" in n for n in result.notes)
    assert _count(session, MeasurementReview) == 0


# --- names and structure -----------------------------------------------------------------------


def test_a_state_name_that_matches_no_place_is_skipped_and_reported(
    session: Session, store: S3Store, source: Source
) -> None:
    edited = edit(PMS_OCT, PMS_SHEET, {f"A{LAGOS_ROW}": "Atlantis"})
    _, result = import_bytes(session, store, source, PMS_OCT, edited)
    assert result.unresolved_places == ["Atlantis"]
    assert result.inserted == 114 - 3  # Lagos' three months are not stored
    assert any("Atlantis" in n for n in result.notes)


def test_both_spellings_of_nasarawa_resolve_to_the_same_state(
    session: Session, store: S3Store, source: Source, places: dict[str, int]
) -> None:
    import_bytes(session, store, source, "DIESEL_OCT_2024_REPORT.xlsx")  # "Nassarawa"
    import_bytes(session, store, source, "GAS_PRICE_WATCH_OCT_2024.xlsx")  # "Nasarawa"
    values = current_values(session)
    assert ("ago_litre", "NG-NA", date(2024, 10, 1)) in values
    assert ("lpg_5kg", "NG-NA", date(2024, 10, 1)) in values


def test_the_evidence_document_gets_an_official_dataset_origin(
    session: Session, store: S3Store, source: Source
) -> None:
    doc, _ = import_bytes(session, store, source, PMS_OCT)
    origin = session.get(ReportingOrigin, doc.origin_id)
    assert origin is not None and origin.kind == "official_dataset"
    assert origin.label == release(PMS_OCT)["listing_title"]


def test_a_release_for_another_month_than_the_file_holds_is_refused(
    session: Session, store: S3Store, source: Source
) -> None:
    doc, _ = import_bytes(session, store, source, PMS_OCT)
    with pytest.raises(NbsParseError, match="October 2024 but the release is for September 2024"):
        import_workbook(
            session, store, source, doc, load_items().publication("pms"),
            vintage=release_date(PMS_OCT), expected_month=date(2024, 9, 1),
        )  # fmt: skip


def test_a_file_where_no_tracked_item_can_be_found_fails_loudly(
    session: Session, store: S3Store, source: Source
) -> None:
    """If NBS renames every food row, the import must fail (so the source degrades) instead of
    quietly storing nothing."""
    renamed = {f"A{r}": f"renamed {r}" for r in (4, 20, 26, 35)}
    edited = edit("selected_food_oct_2024.xlsx", "Selected Food oct 2024", renamed)
    with pytest.raises(NbsParseError, match="none of the 4 tracked food items"):
        import_bytes(session, store, source, "selected_food_oct_2024.xlsx", edited)


def test_without_the_country_place_the_import_refuses(
    session: Session, store: S3Store, source: Source
) -> None:
    session.execute(Place.__table__.update().where(Place.code == "NG").values(code=None))
    with pytest.raises(NbsParseError, match="country place NG is not loaded"):
        import_bytes(session, store, source, PMS_OCT)


def test_all_37_places_of_the_fixture_have_a_code(places: dict[str, int]) -> None:
    assert len(places) == len(STATES) + 1 == 38
    assert datetime.date(2024, 10, 1)  # keeps the datetime import honest for the edits above
