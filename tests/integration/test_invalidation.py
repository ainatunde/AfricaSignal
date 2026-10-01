"""Invalidation, corrections, withdrawals and expiry (AS-013), on PostgreSQL. One test per trigger."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from africasignal.jobs.handlers import JobContext
from africasignal.jobs.handlers.assess_situation import assess_situation as assess_handler
from africasignal.jobs.handlers.expire_assessments import expire_assessments as expire_handler
from africasignal.jobs.handlers.invalidate import invalidate as invalidate_handler
from africasignal.jobs.queue import ClaimedJob
from africasignal.models import (
    AppUser,
    AssessmentInput,
    AssessmentVersion,
    Notification,
    Outbox,
    Place,
    Situation,
    Source,
)
from africasignal.publish import hooks
from africasignal.publish.expiry import expire_assessments
from africasignal.publish.hooks import NotificationKind
from africasignal.publish.invalidation import (
    invalidate,
    plan_corrections,
    withdraw_situation,
)
from africasignal.publish.situations import (
    assess_situation,
    ensure_situations,
    request_assessments,
)
from africasignal.publish.versions import apply_policy, release_held
from africasignal.storage import S3Store
from tests.integration.nbs_support import add_places, add_source, import_bytes, make_store
from tests.unit.sources.nbs_fixtures import edit

GAS_OCT = "GAS_PRICE_WATCH_OCT_2024.xlsx"
PMS_OCT = "PMS_OCT_2024_REPORT.xlsx"
WHEN = datetime(2024, 11, 25, 12, tzinfo=UTC)
RESTATED = date(2024, 12, 20)
ITEM = "lpg_12_5kg"  # national: medium severity in October 2024, so it is published at once


class _Frozen(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
        return WHEN


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The job handlers read the clock; the data is from 2024, so keep "now" a week after it."""
    from africasignal.jobs.handlers import assess_situation as assess_module
    from africasignal.jobs.handlers import expire_assessments as expire_module
    from africasignal.jobs.handlers import invalidate as invalidate_module

    for module in (assess_module, expire_module, invalidate_module):
        monkeypatch.setattr(module, "datetime", _Frozen)


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
    hooks.register_publication_hook(lambda session, vid, kind: calls.append((vid, kind)))
    yield calls
    hooks.clear_publication_hooks()


def _published(
    session: Session, item: str = ITEM, code: str = "NG", now: datetime = WHEN
) -> tuple[Situation, AssessmentVersion]:
    place = session.scalars(select(Place.id).where(Place.code == code)).one()
    (situation,) = ensure_situations(session, [(item, place)])
    version = assess_situation(session, situation.id, now).version
    assert version is not None
    decision = apply_policy(session, version.id, now)
    assert decision is not None and decision.status == "published", decision
    return situation, version


def _run_assess_jobs(session: Session, now: datetime | None = None) -> None:
    """Run the queued assess_situation jobs with the real handler, oldest first."""
    rows = session.execute(
        text(
            "SELECT id, payload FROM job WHERE kind = 'assess_situation' AND status = 'queued' ORDER BY id"
        )
    ).all()
    for job_id, payload in rows:
        assess_handler(
            JobContext(session, ClaimedJob(job_id, "assess_situation", payload, 1, 5), "w")
        )
        session.execute(text("UPDATE job SET status = 'done' WHERE id = :i"), {"i": job_id})


def _payloads(session: Session) -> list[dict[str, Any]]:
    return [
        r[0]
        for r in session.execute(
            text("SELECT payload FROM job WHERE kind = 'assess_situation' ORDER BY id")
        ).all()
    ]


def _restated_october() -> bytes:
    """The real October cooking gas file with the national 12.5kg September value changed from
    16313.43 to 16400 (column K of the "Average" row): a restatement."""
    return edit(GAS_OCT, "GAS OCT 2024", {"K46": 16400.0})


def _delivery_rows(
    session: Session, version: AssessmentVersion
) -> tuple[Notification, Notification, Outbox]:
    user = AppUser(email="reader@example.com")
    session.add(user)
    session.flush()
    unread = Notification(
        user_id=user.id, assessment_version_id=version.id, kind="new_version", dedupe_key="n1"
    )
    read = Notification(
        user_id=user.id, assessment_version_id=version.id, kind="new_version", dedupe_key="n2",
        read_at=WHEN,
    )  # fmt: skip
    email = Outbox(
        kind="email_correction", payload={"assessment_version_id": version.id}, dedupe_key="o1"
    )
    session.add_all([unread, read, email])
    session.flush()
    return unread, read, email


# --- trigger 1: a measurement is superseded ----------------------------------------------------


def test_a_restated_figure_produces_a_corrected_version_and_cancels_pending_delivery(
    session: Session, store: S3Store, source: Source, notified: list[tuple[int, NotificationKind]]
) -> None:
    import_bytes(session, store, source, GAS_OCT)
    situation, v1 = _published(session)
    unread, read, email = _delivery_rows(session, v1)
    notified.clear()

    doc, result = import_bytes(
        session, store, source, GAS_OCT, _restated_october(), vintage=RESTATED
    )
    assert len(result.superseded) == 1
    queued = request_assessments(session, result.touched, doc.id, result.superseded)
    assert len(queued) == 1  # only the national situation had a published version using it
    (payload,) = [p for p in _payloads(session) if p.get("correction")]
    assert payload["correction"] == (
        "Corrected: NBS revised the September 2024 figure from ₦16,313.43 to ₦16,400.00"
    )
    _run_assess_jobs(session)

    versions = list(
        session.scalars(
            select(AssessmentVersion)
            .where(AssessmentVersion.situation_id == situation.id)
            .order_by(AssessmentVersion.version)
        )
    )
    assert [v.status for v in versions] == ["superseded", "published"]
    v2 = versions[1]
    assert v2.change_summary == payload["correction"] and v2.supersedes_id == v1.id
    session.refresh(situation)
    assert situation.current_version_id == v2.id
    assert notified == [(v2.id, "correction")]
    # the old version's unread notification and pending email are cancelled; a read one is kept
    session.refresh(unread), session.refresh(read), session.refresh(email)
    assert unread.cancelled_at is not None and read.cancelled_at is None
    assert (email.status, email.last_error) == ("dead", "superseded")


def test_a_restatement_nobody_published_yet_queues_a_plain_reassessment(
    session: Session, store: S3Store, source: Source
) -> None:
    import_bytes(session, store, source, GAS_OCT)
    place = session.scalars(select(Place.id).where(Place.code == "NG")).one()
    ensure_situations(session, [(ITEM, place)])  # a situation, but no version was ever published
    doc, result = import_bytes(
        session, store, source, GAS_OCT, _restated_october(), vintage=RESTATED
    )
    request_assessments(session, result.touched, doc.id, result.superseded)
    assert all("correction" not in p for p in _payloads(session))


def test_an_unchanged_value_is_not_a_correction(
    session: Session, store: S3Store, source: Source
) -> None:
    import_bytes(session, store, source, GAS_OCT)
    _published(session)
    doc, result = import_bytes(session, store, source, GAS_OCT, vintage=RESTATED)
    assert result.superseded == []
    assert plan_corrections(session, "measurement", []) == {}


def test_several_restated_figures_are_summarised(
    session: Session, store: S3Store, source: Source
) -> None:
    import_bytes(session, store, source, GAS_OCT)
    situation, _ = _published(session)
    restated = edit(GAS_OCT, "GAS OCT 2024", {"K46": 16400.0, "L46": 16900.0})  # Sep and Oct
    doc, result = import_bytes(session, store, source, GAS_OCT, restated, vintage=RESTATED)
    plan = plan_corrections(session, "measurement", result.superseded)
    assert plan[situation.id].startswith("Corrected: NBS revised 1 figure") is False
    assert plan[situation.id] == (
        "Corrected: NBS revised 2 figures used in this assessment, "
        "for example the October 2024 figure from ₦16,734.55 to ₦16,900.00"
    )


# --- trigger 2: an evidence document is withdrawn ----------------------------------------------


def test_a_withdrawn_document_with_no_replacement_produces_a_withdrawn_version(
    session: Session, store: S3Store, source: Source, notified: list[tuple[int, NotificationKind]]
) -> None:
    doc, _ = import_bytes(session, store, source, GAS_OCT)
    situation, v1 = _published(session)
    unread, _, email = _delivery_rows(session, v1)
    notified.clear()

    doc.status = "withdrawn"
    session.flush()
    queued = invalidate(session, "evidence_document", [doc.id], WHEN)
    assert queued == [situation.id]
    _run_assess_jobs(session)

    withdrawn = session.scalars(
        select(AssessmentVersion).where(AssessmentVersion.status == "withdrawn")
    ).one()
    session.refresh(v1), session.refresh(situation)
    assert withdrawn.version == 2 and withdrawn.supersedes_id == v1.id
    assert withdrawn.headline == (
        "Withdrawn: the evidence behind this assessment was withdrawn and nothing replaces it"
    )
    assert (withdrawn.facts, withdrawn.severity, withdrawn.evidence_state) == (
        [],
        "none",
        "insufficient",
    )
    assert withdrawn.scope_label == v1.scope_label and withdrawn.period_label == v1.period_label
    assert v1.status == "superseded" and situation.current_version_id == withdrawn.id
    assert notified == [(withdrawn.id, "withdrawal")]
    session.refresh(unread), session.refresh(email)
    assert unread.cancelled_at is not None and email.status == "dead"


def test_a_withdrawn_document_with_a_replacement_produces_a_correction_not_a_withdrawal(
    session: Session, store: S3Store, source: Source, notified: list[tuple[int, NotificationKind]]
) -> None:
    doc, _ = import_bytes(session, store, source, GAS_OCT)
    situation, v1 = _published(session)
    # The same figures arrive from a second source (its own series), so evidence remains.
    other = Source(
        slug="nbs-microdata", name="NBS microdata", kind="official_statistics", adapter="nbs",
        owner="National Bureau of Statistics", schedule_minutes=1440,
    )  # fmt: skip
    session.add(other)
    session.flush()
    from africasignal.models import SourcePermission

    session.add(
        SourcePermission(
            source_id=other.id,
            version=1,
            may_collect=True,
            may_store_full_text=True,
            may_republish_numbers=True,
            approved_at=WHEN,
        )  # fmt: skip
    )
    session.flush()
    import_bytes(session, store, other, GAS_OCT, vintage=RESTATED)
    notified.clear()

    doc.status = "withdrawn"
    session.flush()
    invalidate(session, "evidence_document", [doc.id], WHEN)
    _run_assess_jobs(session)

    v2 = session.scalars(
        select(AssessmentVersion).where(
            AssessmentVersion.situation_id == situation.id, AssessmentVersion.version == 2
        )
    ).one()
    assert v2.status == "published" and v2.headline == v1.headline  # same figures, other source
    assert v2.change_summary == "Corrected: a source document behind this assessment was withdrawn"
    assert notified == [(v2.id, "correction")]
    assert not session.scalars(
        select(AssessmentVersion).where(AssessmentVersion.status == "withdrawn")
    ).all()


def test_withdrawing_a_situation_twice_does_nothing_the_second_time(
    session: Session, store: S3Store, source: Source
) -> None:
    import_bytes(session, store, source, GAS_OCT)
    situation, _ = _published(session)
    assert withdraw_situation(session, situation, WHEN) is not None
    assert withdraw_situation(session, situation, WHEN) is None


def test_a_situation_with_nothing_published_has_nothing_to_withdraw(
    session: Session, store: S3Store, source: Source
) -> None:
    import_bytes(session, store, source, GAS_OCT)
    place = session.scalars(select(Place.id).where(Place.code == "NG")).one()
    (situation,) = ensure_situations(session, [(ITEM, place)])
    assert withdraw_situation(session, situation, WHEN) is None


def test_a_draft_held_under_r7_that_used_a_withdrawn_document_is_withheld(
    session: Session, store: S3Store, source: Source
) -> None:
    doc, _ = import_bytes(session, store, source, PMS_OCT)
    place = session.scalars(select(Place.id).where(Place.code == "NG-LA")).one()
    (lagos,) = ensure_situations(session, [("pms_litre", place)])
    v = assess_situation(session, lagos.id, WHEN).version
    assert v is not None
    decision = apply_policy(session, v.id, WHEN)
    assert decision is not None and decision.status == "held"
    doc.status = "withdrawn"
    session.flush()
    assert invalidate(session, "evidence_document", [doc.id], WHEN) == []  # nothing published
    assert (v.status, v.withheld_reasons, v.hold_until) == (
        "withheld",
        ["invalidated:evidence_document"],
        None,
    )
    assert release_held(session, WHEN + timedelta(hours=2)) == []


# --- trigger 3: a claim is marked invalid ------------------------------------------------------


def test_an_invalid_claim_used_by_the_current_version_queues_a_correction(
    session: Session, store: S3Store, source: Source
) -> None:
    import_bytes(session, store, source, GAS_OCT)
    situation, v1 = _published(session)
    session.add(AssessmentInput(assessment_version_id=v1.id, input_kind="claim", input_id=777))
    session.flush()
    plan = plan_corrections(session, "claim", [777])
    assert plan == {
        situation.id: "Corrected: a report behind this assessment was found to be invalid"
    }
    assert invalidate(session, "claim", [777], WHEN) == [situation.id]
    assert invalidate(session, "claim", [778], WHEN) == []  # no version used claim 778


def test_the_same_invalidation_is_not_queued_twice(
    session: Session, store: S3Store, source: Source
) -> None:
    import_bytes(session, store, source, GAS_OCT)
    situation, v1 = _published(session)
    session.add(AssessmentInput(assessment_version_id=v1.id, input_kind="claim", input_id=777))
    session.flush()
    assert invalidate(session, "claim", [777], WHEN) == [situation.id]
    assert invalidate(session, "claim", [777], WHEN) == []


def test_unknown_input_kinds_are_refused(session: Session) -> None:
    with pytest.raises(ValueError, match="unknown input kind"):
        invalidate(session, "weather", [1], WHEN)


def test_the_invalidate_job_handler_reads_its_payload(
    session: Session, store: S3Store, source: Source
) -> None:
    import_bytes(session, store, source, GAS_OCT)
    _, v1 = _published(session)
    session.add(AssessmentInput(assessment_version_id=v1.id, input_kind="claim", input_id=5))
    session.flush()
    invalidate_handler(
        JobContext(session, ClaimedJob(1, "invalidate", {"kind": "claim", "ids": [5]}, 1, 5), "w")
    )
    assert any(p.get("correction") for p in _payloads(session))


# --- expiry ------------------------------------------------------------------------------------


def test_versions_past_valid_until_become_stale_and_are_reassessed(
    session: Session, store: S3Store, source: Source
) -> None:
    import_bytes(session, store, source, GAS_OCT)
    situation, v1 = _published(session)
    assert v1.valid_until == datetime(2025, 1, 14, 23, 59, 59, tzinfo=UTC)
    assert expire_assessments(session, v1.valid_until) == []  # not yet: the limit is exclusive
    just_after = v1.valid_until + timedelta(seconds=1)
    assert expire_assessments(session, just_after) == [situation.id]
    assert v1.status == "stale"
    session.refresh(situation)
    assert situation.current_version_id == v1.id  # still the current version, shown as out of date
    assert expire_assessments(session, just_after + timedelta(hours=1)) == []  # already stale
    assert {"situation_id": situation.id} in _payloads(session)


def test_a_reassessment_with_the_same_inputs_leaves_the_version_stale(
    session: Session, store: S3Store, source: Source
) -> None:
    import_bytes(session, store, source, GAS_OCT)
    situation, v1 = _published(session)
    later = v1.valid_until + timedelta(days=1)  # type: ignore[operator]
    expire_assessments(session, later)
    _run_assess_jobs(session)
    assert v1.status == "stale" and v1.last_checked_at is not None
    assert session.scalars(
        select(AssessmentVersion).where(AssessmentVersion.situation_id == situation.id)
    ).all() == [v1]


def test_new_data_replaces_a_stale_version(
    session: Session, store: S3Store, source: Source
) -> None:
    import_bytes(session, store, source, GAS_OCT)
    situation, v1 = _published(session)
    expire_assessments(session, v1.valid_until + timedelta(days=1))  # type: ignore[operator]
    restated = edit(GAS_OCT, "GAS OCT 2024", {"L46": 17000.0})
    import_bytes(session, store, source, GAS_OCT, restated, vintage=RESTATED)
    _run_assess_jobs(session)
    v2 = session.scalars(
        select(AssessmentVersion).where(
            AssessmentVersion.situation_id == situation.id, AssessmentVersion.version == 2
        )
    ).one_or_none()
    assert v2 is not None
    session.refresh(v1)
    assert v1.status in ("superseded", "stale")


def test_expiry_leaves_unpublished_and_unexpired_versions_alone(
    session: Session, store: S3Store, source: Source
) -> None:
    import_bytes(session, store, source, GAS_OCT)
    situation, v1 = _published(session)
    assert expire_assessments(session, WHEN + timedelta(days=30)) == []
    assert v1.status == "published"


def test_the_expiry_job_handler_runs_the_expiry(
    session: Session, store: S3Store, source: Source, monkeypatch: pytest.MonkeyPatch
) -> None:
    import_bytes(session, store, source, GAS_OCT)
    situation, v1 = _published(session)
    v1.valid_until = datetime(2020, 1, 1, tzinfo=UTC)  # long before the frozen clock
    session.flush()
    expire_handler(JobContext(session, ClaimedJob(1, "expire_assessments", {}, 1, 5), "w"))
    assert v1.status == "stale"
