"""Invalidation, corrections and withdrawals (spec B9, AS-013).

A correction starts when something an assessment used stops being valid:

* a measurement is superseded by a restated value (NBS revised a figure);
* an evidence document is withdrawn (operator action or source takedown);
* a claim is marked invalid by an operator.

``plan_corrections`` finds the situations whose *current* version used the invalid input, through
``assessment_input``, and the sentence that explains the correction. ``invalidate`` (the
``invalidate`` job) withholds affected drafts and queues an ``assess_situation`` job per
situation carrying that sentence. That job stores a new version with the sentence as its
``change_summary`` and publishes it as a correction (the old version becomes ``superseded`` and
pending notifications for it are cancelled). When nothing valid is left to assess, a ``withdrawn``
version replaces the current one. Sending the notices is delivery's job (``publish.hooks``).
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.assess.invalidation import (
    WITHDRAWAL_NO_EVIDENCE,
    Revision,
    correction_for_invalid_claim,
    correction_for_revisions,
    correction_for_withdrawn_document,
    withdrawal_headline,
)
from africasignal.assess.publication_policy import POLICY_VERSION
from africasignal.jobs import queue
from africasignal.models import (
    AssessmentInput,
    AssessmentVersion,
    EvidenceDocument,
    Measurement,
    Situation,
    Source,
)
from africasignal.publish.hooks import notify_published
from africasignal.publish.situations import source_short_name
from africasignal.publish.versions import cancel_pending_delivery

log = logging.getLogger("africasignal.publish.invalidation")

KINDS = ("measurement", "claim", "evidence_document")


def _versions_using(
    session: Session, kind: str, ids: Iterable[int]
) -> list[tuple[AssessmentVersion, int]]:
    """(version, input id) for every stored version that used one of the inputs."""
    if kind not in KINDS:
        raise ValueError(f"unknown input kind {kind!r}")
    rows = session.execute(
        select(AssessmentVersion, AssessmentInput.input_id)
        .join(AssessmentInput, AssessmentInput.assessment_version_id == AssessmentVersion.id)
        .where(AssessmentInput.input_kind == kind, AssessmentInput.input_id.in_(list(ids)))
    ).all()
    return [(r[0], r[1]) for r in rows]


def _revisions(session: Session, old_ids: set[int]) -> list[Revision]:
    revisions = []
    for old in session.scalars(select(Measurement).where(Measurement.id.in_(old_ids))):
        if old.superseded_by_id is None:
            continue  # not restated (yet): nothing to say
        new = session.get(Measurement, old.superseded_by_id)
        if new is not None and new.value != old.value:
            revisions.append(Revision(old.period_start, Decimal(old.value), Decimal(new.value)))
    return revisions


def _source_short(session: Session, old_ids: set[int]) -> str:
    row = session.execute(
        select(Source)
        .join(EvidenceDocument, EvidenceDocument.source_id == Source.id)
        .join(Measurement, Measurement.evidence_document_id == EvidenceDocument.id)
        .where(Measurement.id.in_(old_ids))
        .limit(1)
    ).scalar_one_or_none()
    return source_short_name(row) if row is not None else "the source"


def plan_corrections(session: Session, kind: str, ids: Iterable[int]) -> dict[int, str]:
    """Situation id -> the correction sentence, for situations whose current version used one of
    the inputs. Situations that only have older versions or drafts using them are not included."""
    ids = set(ids)
    by_situation: dict[int, set[int]] = defaultdict(set)
    for version, input_id in _versions_using(session, kind, ids):
        situation = session.get(Situation, version.situation_id)
        if situation is not None and situation.current_version_id == version.id:
            by_situation[situation.id].add(input_id)

    plan: dict[int, str] = {}
    for situation_id, used in by_situation.items():
        if kind == "measurement":
            revisions = _revisions(session, used)
            if not revisions:
                continue
            plan[situation_id] = correction_for_revisions(_source_short(session, used), revisions)
        elif kind == "evidence_document":
            plan[situation_id] = correction_for_withdrawn_document()
        else:
            plan[situation_id] = correction_for_invalid_claim()
    return plan


def withhold_affected_drafts(session: Session, kind: str, ids: Iterable[int]) -> list[int]:
    """Drafts (including versions on an R7 hold) built on invalid inputs must not publish."""
    withheld: list[int] = []
    for version, _ in _versions_using(session, kind, set(ids)):
        if version.status == "draft":
            version.status = "withheld"
            version.withheld_reasons = [f"invalidated:{kind}"]
            version.hold_until = None
            withheld.append(version.id)
    session.flush()
    return sorted(set(withheld))


def queue_correction(
    session: Session, situation_id: int, correction: str, dedupe_key: str
) -> int | None:
    """Queue the re-assessment that carries a correction sentence."""
    return queue.enqueue(
        session,
        "assess_situation",
        {"situation_id": situation_id, "correction": correction},
        dedupe_key=dedupe_key,
    )


def invalidate(session: Session, kind: str, ids: Iterable[int], now: datetime) -> list[int]:
    """The ``invalidate`` job: act on inputs that are no longer valid. Returns the situation ids
    a correction was queued for."""
    ids = set(ids)
    withhold_affected_drafts(session, kind, ids)
    digest = hashlib.sha256(f"{kind}:{sorted(ids)}".encode()).hexdigest()[:16]
    queued: list[int] = []
    for situation_id, correction in sorted(plan_corrections(session, kind, ids).items()):
        key = f"assess_situation:{situation_id}:invalidate:{digest}"
        if queue_correction(session, situation_id, correction, key) is not None:
            queued.append(situation_id)
    log.info("invalidate %s %s: %d situations to correct", kind, sorted(ids), len(queued))
    return queued


def withdraw_situation(
    session: Session, situation: Situation, now: datetime, reason: str = WITHDRAWAL_NO_EVIDENCE
) -> AssessmentVersion | None:
    """Replace the current version with a ``withdrawn`` one. Returns None when there is no
    published version to withdraw, or it is already withdrawn."""
    locked = session.scalar(
        select(Situation)
        .where(Situation.id == situation.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None:
        return None
    situation = locked
    previous = (
        session.get(AssessmentVersion, situation.current_version_id)
        if situation.current_version_id is not None
        else None
    )
    if previous is None or previous.status == "withdrawn":
        return None
    latest = session.scalars(
        select(AssessmentVersion)
        .where(AssessmentVersion.situation_id == situation.id)
        .order_by(AssessmentVersion.version.desc())
        .limit(1)
    ).one()
    version = AssessmentVersion(
        situation_id=situation.id,
        version=latest.version + 1,
        template=previous.template,
        template_version=previous.template_version,
        policy_version=POLICY_VERSION,
        inputs_hash=hashlib.sha256(f"withdrawn:{previous.inputs_hash}".encode()).hexdigest(),
        status="withdrawn",
        evidence_state="insufficient",
        severity="none",
        headline=withdrawal_headline(reason),
        facts=[],
        possible_factors=[],
        unknowns=[reason[0].upper() + reason[1:]],
        scope_label=previous.scope_label,
        period_label=previous.period_label,
        last_checked_at=now,
        valid_until=None,
        change_summary=reason[0].upper() + reason[1:],
        withheld_reasons=[],
        published_at=now,
        supersedes_id=previous.id,
    )
    session.add(version)
    session.flush()
    previous.status = "superseded"
    situation.current_version_id = version.id
    session.flush()
    cancel_pending_delivery(session, previous.id, now)
    notify_published(session, version.id, "withdrawal")
    log.info("withdrew %s: %s", situation.slug, reason)
    return version
