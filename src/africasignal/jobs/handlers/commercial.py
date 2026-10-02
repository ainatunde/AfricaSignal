"""Fenced, bounded deterministic commercial context refresh jobs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.config import get_settings
from africasignal.jobs.handlers import JobContext, register
from africasignal.jobs.policy import WorkloadSchedule
from africasignal.llm.errors import WorkloadUnavailable
from africasignal.models import (
    AssessmentVersion,
    CommercialControl,
    ContentContext,
    EvidenceDocument,
    Setting,
    Situation,
    Source,
    WorkloadControl,
)
from africasignal.operations.commercial_context import (
    CLASSIFIER_VERSION,
    TAXONOMY_VERSION,
    classify_content,
    content_ref_for,
    store_context_decision,
)


class RebuildPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assessment_version_id: int | None = Field(default=None, ge=1)
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    slot: int | None = Field(default=None, ge=0)
    after_assessment_version_id: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_shape(self) -> RebuildPayload:
        targeted = self.assessment_version_id is not None
        if targeted != (self.content_hash is not None):
            raise ValueError("targeted rebuild requires an assessment version and content hash")
        if targeted and (self.slot is not None or self.after_assessment_version_id):
            raise ValueError("targeted rebuild cannot include scan fields")
        if not targeted and self.slot is None:
            raise ValueError("scheduled rebuild requires a slot")
        return self


def _admit(session: Session, now: datetime) -> None:
    if get_settings().commercial_deny:
        raise WorkloadUnavailable(now + timedelta(hours=1), "commercial deployment deny is active")
    workload = session.scalar(
        select(WorkloadControl)
        .where(WorkloadControl.name == "commercial")
        .with_for_update(read=True)
    )
    if workload is None or not workload.enabled:
        raise WorkloadUnavailable(now + timedelta(hours=1), "commercial workload is off")
    schedule = WorkloadSchedule.model_validate(workload.schedule)
    if not schedule.allows(now):
        raise WorkloadUnavailable(schedule.next_open(now), "commercial processing window is closed")
    control = session.scalar(
        select(CommercialControl)
        .where(CommercialControl.singleton_id == 1)
        .with_for_update(read=True)
    )
    if control is None or not control.global_enabled:
        raise WorkloadUnavailable(now + timedelta(hours=1), "global commercial switch is off")


def _latest_context(session: Session, assessment_version_id: int) -> ContentContext | None:
    return session.scalar(
        select(ContentContext)
        .where(ContentContext.assessment_version_id == assessment_version_id)
        .order_by(ContentContext.created_at.desc(), ContentContext.id.desc())
        .limit(1)
    )


def _fresh(session: Session, assessment_version_id: int, content_hash: str, now: datetime) -> bool:
    row = _latest_context(session, assessment_version_id)
    return bool(
        row is not None
        and row.content_hash == content_hash
        and row.taxonomy_version == TAXONOMY_VERSION
        and row.classifier_version == CLASSIFIER_VERSION
        and row.invalidated_at is None
        and row.expires_at > now
    )


def _rebuild_one(
    session: Session,
    assessment_version_id: int,
    *,
    now: datetime,
    expected_hash: str | None = None,
) -> bool:
    initial_ref = content_ref_for(session, assessment_version_id)
    if initial_ref is None or (expected_hash and initial_ref.content_hash != expected_hash):
        return False
    if _fresh(session, assessment_version_id, initial_ref.content_hash, now):
        return False

    situation = session.scalar(
        select(Situation)
        .where(Situation.current_version_id == assessment_version_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if situation is None or situation.status != "active":
        return False
    current_ref = content_ref_for(session, assessment_version_id)
    if current_ref is None or current_ref.content_hash != initial_ref.content_hash:
        return False
    if expected_hash and current_ref.content_hash != expected_hash:
        return False
    if _fresh(session, assessment_version_id, current_ref.content_hash, now):
        return False

    # Bind locks to the evidence and sources for this exact version, in stable order, before
    # checking rights. This serializes refresh with evidence expiry and source permission changes.
    from africasignal.web.queries import evidence_ids as version_evidence_ids

    version = session.scalar(
        select(AssessmentVersion)
        .where(AssessmentVersion.id == assessment_version_id)
        .execution_options(populate_existing=True)
    )
    if version is None:
        return False
    referenced_ids = tuple(version_evidence_ids(version))
    if not referenced_ids:
        return False
    documents = session.scalars(
        select(EvidenceDocument)
        .where(EvidenceDocument.id.in_(referenced_ids))
        .order_by(EvidenceDocument.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    source_ids = sorted({document.source_id for document in documents})
    if source_ids:
        session.scalars(
            select(Source)
            .where(Source.id.in_(source_ids))
            .order_by(Source.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all()

    final_ref = content_ref_for(session, assessment_version_id)
    if final_ref is None or final_ref.content_hash != current_ref.content_hash:
        return False
    decision = classify_content(session, content_ref=final_ref, now=now)
    store_context_decision(
        session,
        input_ref=final_ref,
        decision=decision,
        now=now,
    )
    return True


def _scan_batch(session: Session, *, now: datetime) -> int:
    setting = session.get(Setting, "commercial_context_scan_cursor")
    cursor = setting.value if setting is not None else 0
    if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
        cursor = 0
    workload = session.get(WorkloadControl, "commercial")
    schedule = WorkloadSchedule.model_validate(workload.schedule) if workload else None
    batch_size = min(10, schedule.max_items_per_run if schedule else 1)

    query = (
        select(AssessmentVersion.id)
        .join(Situation, Situation.current_version_id == AssessmentVersion.id)
        .where(
            AssessmentVersion.id > cursor,
            AssessmentVersion.status == "published",
            Situation.status == "active",
        )
        .order_by(AssessmentVersion.id)
        .limit(batch_size)
    )
    ids = list(session.scalars(query))
    if not ids and cursor:
        ids = list(
            session.scalars(
                select(AssessmentVersion.id)
                .join(Situation, Situation.current_version_id == AssessmentVersion.id)
                .where(
                    AssessmentVersion.status == "published",
                    Situation.status == "active",
                )
                .order_by(AssessmentVersion.id)
                .limit(batch_size)
            )
        )
    if setting is None:
        setting = Setting(key="commercial_context_scan_cursor", value=ids[-1] if ids else 0)
        session.add(setting)
    else:
        setting.value = ids[-1] if ids else 0
    if ids:
        for version_id in ids:
            _rebuild_one(session, version_id, now=now)
    return len(ids)


@register("commercial_context_rebuild")
def rebuild_commercial_context(ctx: JobContext) -> None:
    now = datetime.now(UTC)
    _admit(ctx.session, now)
    payload = RebuildPayload.model_validate(ctx.job.payload)
    if payload.assessment_version_id is not None:
        _rebuild_one(
            ctx.session,
            payload.assessment_version_id,
            now=now,
            expected_hash=payload.content_hash,
        )
        return
    assert payload.slot is not None
    _scan_batch(ctx.session, now=now)
