"""One explainable permission and publication gate for every public channel.

Collection permission controls future polling. It does not by itself revoke reuse of material
already collected; the current approved reuse permission and retention limit do. Every public
surface rechecks those current rights at read, approval, or dispatch time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from africasignal.models import (
    AssessmentInput,
    AssessmentVersion,
    Claim,
    EvidenceDocument,
    Measurement,
    Setting,
    Situation,
    Source,
    SourcePermission,
)

PublicationChannel = Literal["website", "email", "social", "reply"]


@dataclass(frozen=True)
class PublicationDependency:
    evidence_document_id: int
    source_id: int
    permission_version: int


@dataclass(frozen=True)
class PublicationDecision:
    eligible: bool
    channel: PublicationChannel
    rule_ids: tuple[str, ...]
    dependencies: tuple[PublicationDependency, ...] = ()

    def snapshot(self) -> dict[str, object]:
        """JSON-safe decision record stored with an assessment or channel authorization."""
        return {
            "eligible": self.eligible,
            "channel": self.channel,
            "rule_ids": list(self.rule_ids),
            "dependencies": [
                {
                    "evidence_document_id": dependency.evidence_document_id,
                    "source_id": dependency.source_id,
                    "permission_version": dependency.permission_version,
                }
                for dependency in self.dependencies
            ],
        }


def publication_eligibility(
    session: Session,
    version: AssessmentVersion,
    *,
    channel: PublicationChannel,
    now: datetime | None = None,
    allow_draft: bool = False,
    allow_withdrawal_notice: bool = False,
    require_current: bool = True,
) -> PublicationDecision:
    """Evaluate one version using current state, evidence, retention, and approved reuse rights.

    ``allow_draft`` is only for the publication-policy transition immediately before publishing.
    ``allow_withdrawal_notice`` permits a content-free email notice for a current withdrawn
    version; it never permits the withdrawn figures to be rendered or posted.
    """
    return publication_eligibility_many(
        session,
        [version],
        channel=channel,
        now=now,
        allow_draft=allow_draft,
        allow_withdrawal_notice=allow_withdrawal_notice,
        require_current=require_current,
    )[version.id]


def publication_eligibility_many(
    session: Session,
    versions: list[AssessmentVersion],
    *,
    channel: PublicationChannel,
    now: datetime | None = None,
    allow_draft: bool = False,
    allow_withdrawal_notice: bool = False,
    require_current: bool = True,
) -> dict[int, PublicationDecision]:
    """Evaluate a page or batch with bounded queries rather than one rights query per card."""
    if not versions:
        return {}
    now = now or datetime.now(UTC)
    version_ids = {version.id for version in versions}
    situation_ids = {version.situation_id for version in versions}
    situations = {
        situation.id: situation
        for situation in session.scalars(select(Situation).where(Situation.id.in_(situation_ids)))
    }
    inputs_by_version: dict[int, list[tuple[str, int]]] = {
        version_id: [] for version_id in version_ids
    }
    for version_id, kind, input_id in session.execute(
        select(
            AssessmentInput.assessment_version_id,
            AssessmentInput.input_kind,
            AssessmentInput.input_id,
        ).where(AssessmentInput.assessment_version_id.in_(version_ids))
    ):
        inputs_by_version[version_id].append((kind, input_id))

    claim_ids = {
        input_id
        for values in inputs_by_version.values()
        for kind, input_id in values
        if kind == "claim"
    }
    measurement_ids = {
        input_id
        for values in inputs_by_version.values()
        for kind, input_id in values
        if kind == "measurement"
    }
    document_ids_by_version: dict[int, set[int]] = {version_id: set() for version_id in version_ids}
    evidence_errors: dict[int, list[str]] = {version_id: [] for version_id in version_ids}
    for version_id, values in inputs_by_version.items():
        if not values:
            evidence_errors[version_id].append("evidence_missing")
        for kind, input_id in values:
            if kind == "evidence_document":
                document_ids_by_version[version_id].add(input_id)
            elif kind not in {"claim", "measurement"}:
                evidence_errors[version_id].append("assessment_input_missing")

    claims = (
        {
            claim_id: (document_id, valid)
            for claim_id, document_id, valid in session.execute(
                select(Claim.id, Claim.evidence_document_id, Claim.valid).where(
                    Claim.id.in_(claim_ids)
                )
            )
        }
        if claim_ids
        else {}
    )
    measurements = (
        {
            measurement_id: (document_id, superseded_by_id is not None)
            for measurement_id, document_id, superseded_by_id in session.execute(
                select(
                    Measurement.id,
                    Measurement.evidence_document_id,
                    Measurement.superseded_by_id,
                ).where(Measurement.id.in_(measurement_ids))
            )
        }
        if measurement_ids
        else {}
    )
    for version_id, values in inputs_by_version.items():
        for kind, input_id in values:
            if kind == "claim":
                claim = claims.get(input_id)
                if claim is None:
                    evidence_errors[version_id].append("assessment_input_missing")
                else:
                    document_ids_by_version[version_id].add(claim[0])
                    if not claim[1]:
                        evidence_errors[version_id].append("claim_invalid")
            elif kind == "measurement":
                measurement = measurements.get(input_id)
                if measurement is None:
                    evidence_errors[version_id].append("assessment_input_missing")
                else:
                    document_id, superseded = measurement
                    document_ids_by_version[version_id].add(document_id)
                    if superseded:
                        evidence_errors[version_id].append("measurement_superseded")

    all_document_ids = set().union(*document_ids_by_version.values())
    document_rules: dict[int, list[str]] = {}
    document_dependencies: dict[int, PublicationDependency] = {}
    if all_document_ids:
        latest = (
            select(
                SourcePermission.source_id.label("source_id"),
                func.max(SourcePermission.version).label("version"),
            )
            .where(SourcePermission.approved_at.is_not(None))
            .group_by(SourcePermission.source_id)
            .subquery()
        )
        rows = session.execute(
            select(EvidenceDocument, Source, SourcePermission)
            .join(Source, Source.id == EvidenceDocument.source_id)
            .outerjoin(latest, latest.c.source_id == Source.id)
            .outerjoin(
                SourcePermission,
                and_(
                    SourcePermission.source_id == latest.c.source_id,
                    SourcePermission.version == latest.c.version,
                ),
            )
            .where(EvidenceDocument.id.in_(all_document_ids))
        ).all()
        found_documents = {document.id for document, _, _ in rows}
        for version_id, ids in document_ids_by_version.items():
            if ids - found_documents:
                evidence_errors[version_id].append("evidence_missing")
        for document, source, permission in rows:
            document_rule_ids: list[str] = []
            if document.status != "active":
                document_rule_ids.append("evidence_not_active")
            if document.retention_until is not None and document.retention_until <= now:
                document_rule_ids.append("evidence_retention_expired")
            if permission is None:
                document_rule_ids.append("source_permission_missing")
            else:
                document_dependencies[document.id] = PublicationDependency(
                    document.id, source.id, permission.version
                )
                if not permission.may_republish_numbers:
                    document_rule_ids.append("numeric_republication_not_permitted")
                if permission.retention_days is not None and (
                    document.retrieved_at + timedelta(days=permission.retention_days) <= now
                ):
                    document_rule_ids.append("evidence_retention_expired")
            document_rules[document.id] = document_rule_ids

    suspended = (channel != "website" or allow_draft) and session.scalar(
        select(Setting.value).where(Setting.key == "publication_suspended")
    ) is True
    result: dict[int, PublicationDecision] = {}
    for version in versions:
        situation = situations.get(version.situation_id)
        rules: list[str] = []
        if situation is None or situation.status == "closed":
            rules.append("situation_unavailable")
        elif require_current and situation.current_version_id != version.id:
            rules.append("version_not_current")
        if (
            allow_withdrawal_notice
            and channel == "email"
            and version.status == "withdrawn"
            and situation is not None
            and (not require_current or situation.current_version_id == version.id)
            and situation.status != "closed"
        ):
            result[version.id] = PublicationDecision(True, channel, ("withdrawal_notice_only",))
            continue

        allowed_states = (
            {"published", "stale", "withdrawn"} if channel == "website" else {"published"}
        )
        if channel == "website" and not require_current:
            allowed_states.add("superseded")
        if allow_draft:
            allowed_states.add("draft")
        if version.status not in allowed_states:
            rules.append("assessment_state_not_publishable")
        if version.status == "withdrawn" and channel != "website":
            rules.append("withdrawal_content_forbidden")
        if (
            version.status == "published"
            and version.valid_until is not None
            and version.valid_until <= now
            and channel != "website"
        ):
            rules.append("assessment_expired")
        if suspended:
            rules.append("publication_suspended")

        dependencies: set[PublicationDependency] = set()
        if version.status == "withdrawn" and channel == "website":
            acceptance_rule = "withdrawal_page_only"
        else:
            ids = document_ids_by_version[version.id]
            rules.extend(evidence_errors[version.id])
            if not ids and not evidence_errors[version.id]:
                rules.append("evidence_missing")
            for document_id in ids:
                rules.extend(document_rules.get(document_id, ["evidence_missing"]))
                dependency = document_dependencies.get(document_id)
                if dependency is not None:
                    dependencies.add(dependency)
            acceptance_rule = "evidence_and_reuse_rights_current"

        eligible = not rules
        unique_rules = tuple(dict.fromkeys(rules))
        if not unique_rules:
            unique_rules = (acceptance_rule,)
        result[version.id] = PublicationDecision(
            eligible,
            channel,
            unique_rules,
            tuple(sorted(dependencies, key=lambda dep: dep.evidence_document_id)),
        )
    return result
