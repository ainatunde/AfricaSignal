"""Applying publication policy pp-1 to real assessment versions (AS-012), on PostgreSQL."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.models import (
    AssessmentInput,
    AssessmentVersion,
    MeasurementReview,
    Place,
    Series,
    Situation,
    Source,
)
from africasignal.publish import hooks
from africasignal.publish.hooks import NotificationKind
from africasignal.publish.situations import assess_situation, ensure_situations, slug_for
from africasignal.publish.versions import (
    apply_policy,
    publication_suspended,
    release_held,
    set_publication_suspended,
    withhold_version,
)
from africasignal.storage import S3Store
from tests.integration.nbs_support import add_places, add_source, import_bytes, make_store
from tests.unit.sources.nbs_fixtures import edit

PMS_SEP, PMS_OCT = "FUEL_SEPT_2024_REPORT.xlsx", "PMS_OCT_2024_REPORT.xlsx"
GAS_SEP, GAS_OCT = "GAS_PRICE_WATCH_SEPT_2024.xlsx", "GAS_PRICE_WATCH_OCT_2024.xlsx"
D = Decimal
SEP_NOW = datetime(2024, 10, 20, 12, tzinfo=UTC)  # after the September releases
WHEN = datetime(2024, 11, 25, 12, tzinfo=UTC)  # a week after the October 2024 releases
TODAY = datetime(2026, 9, 30, 12, tzinfo=UTC)


@pytest.fixture
def store() -> Iterator[S3Store]:
    yield from make_store()


@pytest.fixture
def source(session: Session) -> Source:
    add_places(session)
    return add_source(session)


@pytest.fixture
def notified() -> Iterator[list[tuple[int, NotificationKind]]]:
    calls: list[tuple[int, NotificationKind]] = []
    hooks.clear_publication_hooks()
    hooks.register_publication_hook(
        lambda session, version_id, kind: calls.append((version_id, kind))
    )
    yield calls
    hooks.clear_publication_hooks()


def _situation(session: Session, item: str, code: str) -> Situation:
    place = session.scalars(select(Place.id).where(Place.code == code)).one()
    (found,) = ensure_situations(session, [(item, place)])
    assert found.slug == slug_for(item, code)
    return found


def _draft(session: Session, situation: Situation, now: datetime) -> AssessmentVersion:
    version = assess_situation(session, situation.id, now).version
    assert version is not None and version.status == "draft"
    return version


# --- publishing --------------------------------------------------------------------------------


def test_a_routine_version_is_published_and_becomes_current(
    session: Session, store: S3Store, source: Source, notified: list[tuple[int, NotificationKind]]
) -> None:
    import_bytes(session, store, source, GAS_OCT)
    national = _situation(session, "lpg_12_5kg", "NG")
    v = _draft(session, national, WHEN)
    assert v.severity == "medium"  # +58.7 % on the year is 2.9x the threshold
    decision = apply_policy(session, v.id, WHEN)
    assert decision is not None and decision.status == "published"
    session.refresh(national)
    assert (v.status, v.policy_version, v.published_at) == ("published", "pp-1", WHEN)
    assert national.current_version_id == v.id and v.withheld_reasons == []
    assert notified == [(v.id, "new_version")]


def test_a_new_release_supersedes_the_published_version(
    session: Session, store: S3Store, source: Source, notified: list[tuple[int, NotificationKind]]
) -> None:
    import_bytes(session, store, source, GAS_SEP)
    national = _situation(session, "lpg_12_5kg", "NG")
    v1 = _draft(session, national, SEP_NOW)
    assert v1.severity != "high"
    apply_policy(session, v1.id, SEP_NOW)
    import_bytes(session, store, source, GAS_OCT)
    v2 = _draft(session, national, WHEN)
    apply_policy(session, v2.id, WHEN)
    session.refresh(national)
    assert (v1.status, v2.status) == ("superseded", "published")
    assert national.current_version_id == v2.id and v2.supersedes_id == v1.id
    assert notified == [(v1.id, "new_version"), (v2.id, "new_version")]


def test_applying_the_policy_twice_does_nothing_the_second_time(
    session: Session, store: S3Store, source: Source, notified: list[tuple[int, NotificationKind]]
) -> None:
    import_bytes(session, store, source, GAS_OCT)
    v = _draft(session, _situation(session, "lpg_12_5kg", "NG"), WHEN)
    assert apply_policy(session, v.id, WHEN) is not None
    assert apply_policy(session, v.id, WHEN + timedelta(minutes=5)) is None
    assert len(notified) == 1 and v.published_at == WHEN


# --- R3: stale data is shown, dated, not hidden ------------------------------------------------


def test_r3_data_that_is_already_stale_is_published_as_an_insufficient_card(
    session: Session, store: S3Store, source: Source, notified: list[tuple[int, NotificationKind]]
) -> None:
    import_bytes(session, store, source, PMS_OCT)
    lagos = _situation(session, "pms_litre", "NG-LA")
    v = _draft(session, lagos, TODAY)  # October 2024 data, assessed in September 2026
    decision = apply_policy(session, v.id, TODAY)
    assert decision is not None and decision.insufficient_card and decision.reasons == ("R3",)
    session.refresh(lagos)
    assert v.status == "published" and lagos.current_version_id == v.id
    assert (v.evidence_state, v.severity, v.explanation) == ("insufficient", "none", None)
    assert v.period_label == "October 2024" and v.last_checked_at == TODAY
    assert any("more than 120 days ago" in u for u in v.unknowns)
    assert v.headline.startswith("Average petrol (PMS) price in Lagos State")
    assert notified == []  # followers are not told about an insufficient-evidence card


# --- R7: first high-severity version is held ---------------------------------------------------


def test_r7_a_first_high_severity_version_is_held_then_released_after_60_minutes(
    session: Session, store: S3Store, source: Source, notified: list[tuple[int, NotificationKind]]
) -> None:
    import_bytes(session, store, source, PMS_OCT)
    lagos = _situation(session, "pms_litre", "NG-LA")
    v = _draft(session, lagos, WHEN)
    assert v.severity == "high"  # +82.9 % on the year
    decision = apply_policy(session, v.id, WHEN)
    assert decision is not None and decision.status == "held"
    session.refresh(lagos)
    assert (v.status, v.hold_until) == ("draft", WHEN + timedelta(minutes=60))
    assert lagos.current_version_id is None and notified == []

    assert release_held(session, WHEN + timedelta(minutes=59)) == []
    assert v.status == "draft"
    assert release_held(session, WHEN + timedelta(minutes=61)) == [v.id]
    session.refresh(lagos)
    assert (v.status, v.hold_until, v.published_at) == (
        "published",
        None,
        WHEN + timedelta(minutes=61),
    )
    assert lagos.current_version_id == v.id and notified == [(v.id, "new_version")]
    assert release_held(session, WHEN + timedelta(hours=3)) == []  # nothing left to release


def test_r7_an_operator_can_withhold_during_the_hold(
    session: Session, store: S3Store, source: Source, notified: list[tuple[int, NotificationKind]]
) -> None:
    import_bytes(session, store, source, PMS_OCT)
    lagos = _situation(session, "pms_litre", "NG-LA")
    v = _draft(session, lagos, WHEN)
    apply_policy(session, v.id, WHEN)
    assert withhold_version(session, v.id, ["operator: figure looks wrong"]) is True
    assert release_held(session, WHEN + timedelta(hours=2)) == []
    session.refresh(lagos)
    assert v.status == "withheld" and v.withheld_reasons == ["operator: figure looks wrong"]
    assert lagos.current_version_id is None and notified == []
    assert withhold_version(session, v.id, ["again"]) is False  # no longer a draft


def test_r7_a_version_held_while_the_kill_switch_is_turned_on_is_not_released(
    session: Session, store: S3Store, source: Source, notified: list[tuple[int, NotificationKind]]
) -> None:
    import_bytes(session, store, source, PMS_OCT)
    v = _draft(session, _situation(session, "pms_litre", "NG-LA"), WHEN)
    apply_policy(session, v.id, WHEN)
    set_publication_suspended(session, True, WHEN + timedelta(minutes=10))
    assert release_held(session, WHEN + timedelta(hours=2)) == []
    assert (v.status, v.withheld_reasons) == ("withheld", ["R1"])
    assert notified == []


def test_r7_only_applies_to_the_first_version_of_a_situation(
    session: Session, store: S3Store, source: Source, notified: list[tuple[int, NotificationKind]]
) -> None:
    import_bytes(session, store, source, PMS_SEP)
    lagos = _situation(session, "pms_litre", "NG-LA")
    first = _draft(session, lagos, SEP_NOW)
    assert first.severity == "high" and apply_policy(session, first.id, SEP_NOW).status == "held"  # type: ignore[union-attr]
    assert release_held(session, SEP_NOW + timedelta(hours=2)) == [first.id]

    import_bytes(session, store, source, PMS_OCT)
    second = _draft(session, lagos, WHEN)
    assert second.severity == "high"
    decision = apply_policy(session, second.id, WHEN)
    assert decision is not None and decision.status == "published"  # not held a second time
    assert (first.status, second.status) == ("superseded", "published")


# --- R1, R2, R4, R8 ----------------------------------------------------------------------------


def test_r1_the_kill_switch_withholds_and_leaves_the_published_version_current(
    session: Session, store: S3Store, source: Source, notified: list[tuple[int, NotificationKind]]
) -> None:
    import_bytes(session, store, source, GAS_SEP)
    national = _situation(session, "lpg_12_5kg", "NG")
    v1 = _draft(session, national, SEP_NOW)
    apply_policy(session, v1.id, SEP_NOW)
    assert publication_suspended(session) is False
    set_publication_suspended(session, True, WHEN)
    assert publication_suspended(session) is True
    import_bytes(session, store, source, GAS_OCT)
    v2 = _draft(session, national, WHEN)
    decision = apply_policy(session, v2.id, WHEN)
    session.refresh(national)
    assert decision is not None and decision.reasons == ("R1",)
    assert (v2.status, v2.withheld_reasons) == ("withheld", ["R1"])
    assert national.current_version_id == v1.id and v1.status == "published"
    assert len(notified) == 1  # only the first publication


def test_the_kill_switch_reads_only_true_as_suspended(session: Session) -> None:
    assert publication_suspended(session) is False  # never set
    set_publication_suspended(session, True, WHEN)
    set_publication_suspended(session, False, WHEN)
    assert publication_suspended(session) is False


def test_r2_a_version_whose_evidence_was_withdrawn_is_withheld(
    session: Session, store: S3Store, source: Source, notified: list[tuple[int, NotificationKind]]
) -> None:
    doc, _ = import_bytes(session, store, source, GAS_OCT)
    v = _draft(session, _situation(session, "lpg_12_5kg", "NG"), WHEN)
    doc.status = "withdrawn"
    session.flush()
    decision = apply_policy(session, v.id, WHEN)
    assert decision is not None and decision.reasons == ("R2",)
    assert (v.status, v.withheld_reasons) == ("withheld", ["R2"]) and notified == []


def test_r4_a_value_awaiting_range_review_withholds_the_assessment(
    session: Session, store: S3Store, source: Source, notified: list[tuple[int, NotificationKind]]
) -> None:
    import_bytes(session, store, source, PMS_SEP)
    # October's Lagos value has a slipped decimal point: it goes to the review queue, not in.
    misplaced = edit(PMS_OCT, "Fuel_October 2024", {"D40": 10809.5})
    import_bytes(session, store, source, PMS_OCT, misplaced)
    assert session.scalars(select(MeasurementReview)).one().status == "pending"
    lagos = _situation(session, "pms_litre", "NG-LA")
    v = _draft(session, lagos, WHEN)  # built from what is stored: September is the latest month
    assert v.period_label == "September 2024"
    decision = apply_policy(session, v.id, WHEN)
    assert decision is not None and decision.reasons == ("R4",)
    assert v.status == "withheld" and notified == []
    # A state without a pending value is unaffected.
    ogun = _draft(session, _situation(session, "pms_litre", "NG-OG"), WHEN)
    assert apply_policy(session, ogun.id, WHEN) is not None and ogun.status != "withheld"


def test_r4_an_approved_review_no_longer_blocks(
    session: Session, store: S3Store, source: Source, notified: list[tuple[int, NotificationKind]]
) -> None:
    import_bytes(session, store, source, PMS_SEP)
    import_bytes(
        session, store, source, PMS_OCT, edit(PMS_OCT, "Fuel_October 2024", {"D40": 10809.5})
    )
    review = session.scalars(select(MeasurementReview)).one()
    review.status = "rejected"
    session.flush()
    v = _draft(session, _situation(session, "pms_litre", "NG-LA"), WHEN)
    decision = apply_policy(session, v.id, WHEN)
    assert decision is not None and decision.status != "withheld"


def test_r8_a_duplicate_of_the_published_version_is_not_published_again(
    session: Session, store: S3Store, source: Source, notified: list[tuple[int, NotificationKind]]
) -> None:
    import_bytes(session, store, source, GAS_OCT)
    national = _situation(session, "lpg_12_5kg", "NG")
    v1 = _draft(session, national, WHEN)
    apply_policy(session, v1.id, WHEN)
    # A draft with the same inputs (for example after a withheld version in between).
    v2 = AssessmentVersion(
        situation_id=national.id, version=2, template=v1.template,
        template_version=v1.template_version, policy_version="unapplied",
        inputs_hash=v1.inputs_hash, status="draft", evidence_state=v1.evidence_state,
        severity=v1.severity, headline=v1.headline, facts=v1.facts, possible_factors=[],
        unknowns=[], scope_label=v1.scope_label, period_label=v1.period_label,
        withheld_reasons=[], supersedes_id=v1.id,
    )  # fmt: skip
    session.add(v2)
    session.flush()
    session.add_all(
        AssessmentInput(assessment_version_id=v2.id, input_kind=i.input_kind, input_id=i.input_id)
        for i in session.scalars(
            select(AssessmentInput).where(AssessmentInput.assessment_version_id == v1.id)
        )
    )
    session.flush()
    decision = apply_policy(session, v2.id, WHEN)
    session.refresh(national)
    assert decision is not None and decision.status == "unchanged" and decision.reasons == ("R8",)
    assert (v2.status, v2.withheld_reasons) == ("withheld", ["R8"])
    assert national.current_version_id == v1.id and v1.status == "published"
    assert len(notified) == 1


def test_the_policy_version_is_recorded_on_withheld_versions_too(
    session: Session, store: S3Store, source: Source
) -> None:
    doc, _ = import_bytes(session, store, source, GAS_OCT)
    v = _draft(session, _situation(session, "lpg_12_5kg", "NG"), WHEN)
    doc.status = "withdrawn"
    session.flush()
    apply_policy(session, v.id, WHEN)
    assert v.policy_version == "pp-1"


def test_a_missing_version_is_ignored(session: Session) -> None:
    assert apply_policy(session, 987654, WHEN) is None


def test_r4_a_pending_value_for_the_assessed_month_itself_also_withholds(
    session: Session, store: S3Store, source: Source
) -> None:
    """A later release may report a different value for the month already assessed, and that value
    failed range validation: until it is reviewed the assessment of that month is in doubt."""
    doc, _ = import_bytes(session, store, source, PMS_OCT)
    lagos = _situation(session, "pms_litre", "NG-LA")
    series_id = session.scalars(select(Series.id).where(Series.item_code == "pms_litre")).one()
    session.add(
        MeasurementReview(
            series_id=series_id,
            place_id=lagos.place_id,
            period_start=date(2024, 10, 1),
            period_end=date(2024, 10, 31),
            value=D("9999.00"),
            vintage=date(2024, 12, 20),
            evidence_document_id=doc.id,
            reference_value=D("1030.46"),
            reason="test",
        )  # fmt: skip
    )
    session.flush()
    v = _draft(session, lagos, WHEN)
    assert v.period_label == "October 2024"
    decision = apply_policy(session, v.id, WHEN)
    assert decision is not None and decision.reasons == ("R4",)
