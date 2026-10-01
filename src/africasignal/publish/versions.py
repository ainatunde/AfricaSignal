"""Applying the publication policy to stored assessment versions (spec B8.5, AS-012).

``assess_situation`` stores new versions as drafts. ``apply_policy`` reads what the policy needs
from the database, asks ``decide`` and then publishes, withholds or holds the version:

* published: status ``published``, becomes the situation's current version, and the version it
  replaces becomes ``superseded``; followers are told through ``publish.hooks`` unless the card
  says the evidence is insufficient;
* withheld: status ``withheld`` with the rule ids in ``withheld_reasons``; the previously
  published version, if any, stays current;
* held (R7): stays ``draft`` with ``hold_until``; ``release_held`` publishes it after the hold
  unless an operator withheld it in the meantime or a withholding rule now applies.
"""

from __future__ import annotations

import logging
from datetime import date, datetime

from sqlalchemy import func, select, text, update
from sqlalchemy.orm import Session

from africasignal.assess.corroboration import OFFICIAL_SOURCE_KINDS
from africasignal.assess.publication_policy import (
    POLICY_VERSION,
    Decision,
    FactDraft,
    Precision,
    VersionDraft,
    decide,
    precision_of,
)
from africasignal.models import (
    AssessmentInput,
    AssessmentVersion,
    Claim,
    EvidenceDocument,
    Measurement,
    MeasurementReview,
    Notification,
    Place,
    Series,
    Setting,
    Situation,
    Source,
)
from africasignal.publish.hooks import NotificationKind, notify_published

log = logging.getLogger("africasignal.publish.versions")

SUSPENDED_KEY = "publication_suspended"
EVER_PUBLISHED = ("published", "superseded", "stale", "withdrawn")
_SCOPE_PRECISION: dict[str, Precision] = {"country": "national", "state": "state"}


def publication_suspended(session: Session) -> bool:
    """The kill switch: ``setting.publication_suspended`` (R1). Anything but true means running."""
    value = session.scalar(select(Setting.value).where(Setting.key == SUSPENDED_KEY))
    return value is True


def set_publication_suspended(session: Session, suspended: bool, now: datetime) -> None:
    """Operators flip the kill switch; the caller records the audit entry."""
    setting = session.get(Setting, SUSPENDED_KEY)
    if setting is None:
        session.add(Setting(key=SUSPENDED_KEY, value=suspended, updated_at=now))
    else:
        setting.value = suspended
        setting.updated_at = now
    session.flush()


def _assessed_period(
    session: Session, version: AssessmentVersion
) -> tuple[str | None, date | None]:
    """The item code and latest period among the measurements the version used."""
    row = session.execute(
        select(Series.item_code, func.max(Measurement.period_start))
        .join(Measurement, Measurement.series_id == Series.id)
        .join(AssessmentInput, AssessmentInput.input_id == Measurement.id)
        .where(
            AssessmentInput.assessment_version_id == version.id,
            AssessmentInput.input_kind == "measurement",
        )
        .group_by(Series.item_code)
    ).first()
    return (row[0], row[1]) if row else (None, None)


def range_review_pending(
    session: Session, version: AssessmentVersion, situation: Situation
) -> bool:
    """Rule R4: a value for the situation's place and item, in the assessed month or later, is
    waiting in the range-check queue."""
    if version.template != "T1_price_change":
        return False
    item_code, period = _assessed_period(session, version)
    if item_code is None or period is None:
        return False
    return (
        session.scalar(
            select(func.count())
            .select_from(MeasurementReview)
            .join(Series, Series.id == MeasurementReview.series_id)
            .where(
                Series.item_code == item_code,
                MeasurementReview.place_id == situation.place_id,
                MeasurementReview.status == "pending",
                MeasurementReview.period_start >= period,
            )
        )
        or 0
    ) > 0


def build_draft(session: Session, version: AssessmentVersion, now: datetime) -> VersionDraft:
    """Everything the policy needs to judge ``version``."""
    situation = session.get(Situation, version.situation_id)
    assert situation is not None
    place = session.get(Place, situation.place_id)
    assert place is not None

    evidence_ids = set(
        session.scalars(
            select(AssessmentInput.input_id).where(
                AssessmentInput.assessment_version_id == version.id,
                AssessmentInput.input_kind == "evidence_document",
            )
        )
    )
    active = frozenset(
        session.scalars(
            select(EvidenceDocument.id).where(
                EvidenceDocument.id.in_(evidence_ids), EvidenceDocument.status == "active"
            )
        )
    )

    current_hash: str | None = None
    if situation.current_version_id is not None:
        current_hash = session.scalar(
            select(AssessmentVersion.inputs_hash).where(
                AssessmentVersion.id == situation.current_version_id
            )
        )
    ever_published = (
        session.scalar(
            select(func.count())
            .select_from(AssessmentVersion)
            .where(
                AssessmentVersion.situation_id == situation.id,
                AssessmentVersion.id != version.id,
                AssessmentVersion.status.in_(EVER_PUBLISHED),
            )
        )
        or 0
    ) > 0

    range_pending = range_review_pending(session, version, situation)

    has_primary = True  # only T2 can lack one (R5)
    if version.template == "T2_policy_change":
        has_primary = (
            session.scalar(
                select(func.count())
                .select_from(AssessmentInput)
                .join(Claim, Claim.id == AssessmentInput.input_id)
                .join(EvidenceDocument, EvidenceDocument.id == Claim.evidence_document_id)
                .join(Source, Source.id == EvidenceDocument.source_id)
                .where(
                    AssessmentInput.assessment_version_id == version.id,
                    AssessmentInput.input_kind == "claim",
                    Source.kind.in_(OFFICIAL_SOURCE_KINDS),
                )
            )
            or 0
        ) > 0

    return VersionDraft(
        template=version.template,  # type: ignore[arg-type]
        evidence_state=version.evidence_state,
        severity=version.severity,
        scope_precision=_SCOPE_PRECISION.get(place.kind, "unknown"),
        facts=tuple(
            FactDraft(
                label=str(f.get("label", "")),
                evidence_ids=tuple(int(i) for i in f.get("evidence_ids", [])),
                place_precision=precision_of(str(f.get("place_code", ""))),
            )
            for f in version.facts
        ),
        inputs_hash=version.inputs_hash,
        now=now,
        publication_suspended=publication_suspended(session),
        active_evidence_ids=active,
        current_published_hash=current_hash,
        ever_published=ever_published,
        range_failure_pending=range_pending,
        has_primary_document=has_primary,
    )


def cancel_pending_delivery(session: Session, version_id: int, now: datetime) -> None:
    """A corrected or withdrawn version must not reach people after the fact: cancel its unread
    ``new_version`` notifications and its pending emails. Email rows are found by
    ``payload.assessment_version_id``, the key delivery writes for emails about a version."""
    session.execute(
        update(Notification)
        .where(
            Notification.assessment_version_id == version_id,
            Notification.kind == "new_version",
            Notification.read_at.is_(None),
            Notification.cancelled_at.is_(None),
        )
        .values(cancelled_at=now)
    )
    session.execute(
        text(
            "UPDATE outbox SET status = 'dead', last_error = 'superseded' "
            "WHERE status = 'pending' AND payload->>'assessment_version_id' = :v"
        ),
        {"v": str(version_id)},
    )


def _publish(
    session: Session,
    situation: Situation,
    version: AssessmentVersion,
    now: datetime,
    kind: NotificationKind = "new_version",
) -> None:
    previous = (
        session.get(AssessmentVersion, situation.current_version_id)
        if situation.current_version_id is not None
        else None
    )
    version.status = "published"
    version.published_at = now
    version.hold_until = None
    version.withheld_reasons = []
    version.policy_version = POLICY_VERSION
    if previous is not None and previous.id != version.id and previous.status != "withdrawn":
        previous.status = "superseded"
    situation.current_version_id = version.id
    session.flush()
    if previous is not None and previous.id != version.id and kind != "new_version":
        cancel_pending_delivery(session, previous.id, now)
    if version.evidence_state != "insufficient":
        notify_published(session, version.id, kind)


def apply_policy(
    session: Session, version_id: int, now: datetime, kind: NotificationKind = "new_version"
) -> Decision | None:
    """Decide and act on a draft version. Returns None when it is not a draft any more. ``kind``
    is "correction" when the version replaces one that used invalid inputs."""
    version = session.get(AssessmentVersion, version_id)
    if version is None or version.status != "draft":
        return None
    situation = session.get(Situation, version.situation_id)
    assert situation is not None
    decision = decide(build_draft(session, version, now))

    version.policy_version = POLICY_VERSION
    match decision.status:
        case "published":
            _publish(session, situation, version, now, kind)
        case "held":
            version.hold_until = decision.hold_until
        case _:  # withheld, or unchanged: a draft identical to what is already published
            version.status = "withheld"
            version.withheld_reasons = list(decision.reasons)
            version.hold_until = None
    session.flush()
    log.info(
        "policy %s for %s v%d: %s %s",
        POLICY_VERSION,
        situation.slug,
        version.version,
        decision.status,
        list(decision.reasons),
    )
    return decision


def release_held(session: Session, now: datetime) -> list[int]:
    """Publish held versions whose hold has ended, unless a withholding rule applies now.

    The hold (R7) only applies to a first high-severity version, so on release the version is
    judged as if something had been published already."""
    due = session.scalars(
        select(AssessmentVersion)
        .where(
            AssessmentVersion.status == "draft",
            AssessmentVersion.hold_until.is_not(None),
            AssessmentVersion.hold_until <= now,
        )
        .order_by(AssessmentVersion.hold_until)
    ).all()
    released: list[int] = []
    for version in due:
        if release_version(session, version, now) == "published":
            released.append(version.id)
    session.flush()
    return released


def release_version(session: Session, version: AssessmentVersion, now: datetime) -> str:
    """Judge a held draft as if something had been published already and act on it: ``published``,
    or ``withheld`` when a withholding rule applies now. Used when the hold ends and when an
    operator releases a version early; the rules apply either way, so an operator cannot publish
    what the policy would withhold."""
    situation = session.get(Situation, version.situation_id)
    assert situation is not None
    draft = build_draft(session, version, now)
    decision = decide(VersionDraft(**{**draft.__dict__, "ever_published": True}))
    if decision.status == "published":
        _publish(session, situation, version, now)
        return "published"
    version.status = "withheld"
    version.withheld_reasons = list(decision.reasons)
    version.hold_until = None
    session.flush()
    return "withheld"


def withhold_version(session: Session, version_id: int, reasons: list[str]) -> bool:
    """An operator withholds a version that is still a draft (for example during an R7 hold).
    Returns False when it is no longer a draft. The caller records the audit entry."""
    version = session.get(AssessmentVersion, version_id)
    if version is None or version.status != "draft":
        return False
    version.status = "withheld"
    version.withheld_reasons = reasons
    version.hold_until = None
    session.flush()
    return True
