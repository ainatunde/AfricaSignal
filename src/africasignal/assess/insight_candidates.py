"""Deterministic, private insight selection across current eligible assessments."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from africasignal import audit
from africasignal.models import (
    AssessmentVersion,
    EvidenceDocument,
    InsightCandidate,
    InsightCandidateAssessment,
    Situation,
    Source,
)
from africasignal.publish.eligibility import (
    PublicationDecision,
    publication_eligibility_many,
)

PROMPT_VERSION = "insight_selection_v1"
MIN_CHANGE_GAP_PP = Decimal("5")
MAX_VERSIONS = 150
MAX_CANDIDATES_PER_RUN = 100
MAX_CANDIDATES_TO_REFRESH = 200


@dataclass(frozen=True)
class CandidateSpec:
    candidate_type: str
    version_ids: tuple[int, ...]
    roles: tuple[str, ...]
    reason_codes: tuple[str, ...]
    packet: dict[str, Any]
    dedupe_key: str


def _number(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        return None
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _parse_month(label: str) -> int | None:
    try:
        month = datetime.strptime(label, "%B %Y")
    except ValueError:
        return None
    return month.year * 12 + month.month


def _version_facts(version: AssessmentVersion) -> list[dict[str, Any]]:
    return [fact for fact in version.facts if isinstance(fact, dict)]


def _facts_are_grounded(version: AssessmentVersion, decision: PublicationDecision | None) -> bool:
    if decision is None or not decision.eligible:
        return False
    allowed_document_ids = {dependency.evidence_document_id for dependency in decision.dependencies}
    facts = _version_facts(version)
    if not facts:
        return False
    for fact in facts:
        evidence_ids = fact.get("evidence_ids")
        if not isinstance(evidence_ids, list) or not evidence_ids:
            return False
        if any(
            isinstance(document_id, bool)
            or not isinstance(document_id, int)
            or document_id < 1
            or document_id not in allowed_document_ids
            for document_id in evidence_ids
        ):
            return False
    return True


def _input_hash(version: AssessmentVersion) -> str:
    return f"{version.situation_id}:{version.inputs_hash}"


def _dedupe_key(candidate_type: str, versions: list[AssessmentVersion]) -> str:
    inputs = ":".join(sorted(_input_hash(version) for version in versions))
    raw = f"{PROMPT_VERSION}:{candidate_type}:{inputs}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _evidence_packet(
    versions: list[AssessmentVersion],
    situations: dict[int, Situation],
    decisions: dict[int, PublicationDecision],
) -> list[dict[str, Any]]:
    packet: list[dict[str, Any]] = []
    for version in versions:
        situation = situations[version.situation_id]
        packet.append(
            {
                "assessment_version_id": version.id,
                "situation_id": situation.id,
                "situation_title": situation.title,
                "topic": situation.topic,
                "item_code": situation.item_code,
                "place_id": situation.place_id,
                "assessment_template": version.template,
                "assessment_version": version.version,
                "assessment_input_hash": version.inputs_hash,
                "headline": version.headline,
                "change_summary": version.change_summary,
                "evidence_state": version.evidence_state,
                "severity": version.severity,
                "scope": version.scope_label,
                "period": version.period_label,
                "facts": _version_facts(version),
                "explanation_assertions": version.explanation_assertions or [],
                "unknowns": version.unknowns,
                "source_coverage": [],
                "freshness": {
                    "last_checked_at": (
                        version.last_checked_at.isoformat() if version.last_checked_at else None
                    ),
                    "valid_until": version.valid_until.isoformat() if version.valid_until else None,
                },
                "eligibility": decisions[version.id].snapshot(),
            }
        )
    return packet


def _spec(
    candidate_type: str,
    versions: list[AssessmentVersion],
    packet_versions: list[dict[str, Any]],
    reason_codes: tuple[str, ...],
    brief: dict[str, Any],
    sources: list[dict[str, Any]],
    roles: tuple[str, ...] | None = None,
) -> CandidateSpec:
    version_roles = roles or tuple(
        "current" if len(versions) == 1 else f"series_{index + 1}"
        for index, _version in enumerate(versions)
    )
    return CandidateSpec(
        candidate_type=candidate_type,
        version_ids=tuple(version.id for version in versions),
        roles=version_roles,
        reason_codes=reason_codes,
        packet={
            "selection_version": PROMPT_VERSION,
            "candidate_type": candidate_type,
            "brief": brief,
            "assessments": packet_versions,
            "source_coverage": sources,
            "uncertainty": sorted(
                {
                    unknown
                    for version in packet_versions
                    for unknown in version.get("unknowns", [])
                    if isinstance(unknown, str)
                }
            ),
        },
        dedupe_key=_dedupe_key(candidate_type, versions),
    )


def _specs_for_current(
    session: Session,
    versions: list[AssessmentVersion],
    historical_versions: dict[int, AssessmentVersion],
    situations: dict[int, Situation],
    decisions: dict[int, PublicationDecision],
) -> list[CandidateSpec]:
    specs: list[CandidateSpec] = []
    active = [
        version for version in versions if _facts_are_grounded(version, decisions.get(version.id))
    ]
    histories = [
        historical_versions[version.supersedes_id]
        for version in active
        if version.supersedes_id in historical_versions
        and historical_versions[version.supersedes_id].status == "superseded"
        and _facts_are_grounded(
            historical_versions[version.supersedes_id], decisions.get(version.supersedes_id)
        )
    ]
    coverage_by_version = _source_coverage_by_version(session, [*active, *histories], decisions)
    packet_by_version: dict[int, dict[str, Any]] = {}
    for version in [*active, *histories]:
        packet_by_version[version.id] = _evidence_packet([version], situations, decisions)[0]
        packet_by_version[version.id]["source_coverage"] = coverage_by_version.get(version.id, [])

    def packet_for(rows: list[AssessmentVersion]) -> list[dict[str, Any]]:
        return [packet_by_version[row.id] for row in rows]

    for version in active:
        rows = [version]
        packet_versions = packet_for(rows)
        source_coverage = coverage_by_version.get(version.id, [])
        if (
            version.evidence_state != "disputed"
            and version.severity in {"medium", "high"}
            and version.change_summary
        ):
            brief = {
                "what_changed": version.change_summary,
                "baseline": version.period_label,
                "affected_scope": version.scope_label,
                "assessment_headline": version.headline,
                "source_reported_explanations": version.explanation_assertions or [],
                "unknowns": version.unknowns,
                "evidence_links": sorted(
                    {
                        int(document_id)
                        for fact in _version_facts(version)
                        for document_id in fact.get("evidence_ids", [])
                        if isinstance(document_id, int)
                    }
                ),
                "freshness": version.last_checked_at.isoformat()
                if version.last_checked_at
                else None,
            }
            specs.append(
                _spec(
                    "material_change",
                    rows,
                    packet_versions,
                    ("severity_material", "current_assessment", "eligible_evidence"),
                    brief,
                    source_coverage,
                )
            )
        if version.evidence_state == "disputed":
            disagreement = [
                fact
                for fact in _version_facts(version)
                if "disagree" in str(fact.get("label", "")).casefold()
            ]
            brief = {
                "what_disagrees": disagreement,
                "measurement_or_policy_position": version.headline,
                "scope": version.scope_label,
                "period": version.period_label,
                "unknowns": version.unknowns,
                "interpretation_limit": (
                    "The disagreement is recorded; the packet does not resolve it or infer "
                    "why it occurred."
                ),
            }
            specs.append(
                _spec(
                    "evidence_disagreement",
                    rows,
                    packet_versions,
                    ("assessment_disputed", "current_assessment", "eligible_evidence"),
                    brief,
                    source_coverage,
                )
            )

    comparables: list[tuple[AssessmentVersion, Situation, dict[str, Any], Decimal]] = []
    for version in active:
        situation = situations[version.situation_id]
        if (
            version.template != "T1_price_change"
            or version.evidence_state not in {"reported", "corroborated"}
            or situation.item_code is None
        ):
            continue
        change_fact = next(
            (
                fact
                for fact in _version_facts(version)
                if fact.get("label") == "Month-on-month change" and fact.get("unit") == "%"
            ),
            None,
        )
        change_value = _number(change_fact.get("value")) if change_fact else None
        if change_fact is not None and change_value is not None:
            comparables.append((version, situation, change_fact, change_value))

    for index, (left, left_situation, left_fact, left_value) in enumerate(comparables):
        for right, right_situation, right_fact, right_value in comparables[index + 1 :]:
            if (
                left_situation.id == right_situation.id
                or left_situation.topic != right_situation.topic
                or left_situation.place_id != right_situation.place_id
                or not left_fact.get("place_code")
                or left_fact.get("place_code") != right_fact.get("place_code")
                or left.period_label != right.period_label
                or left_fact.get("period") != left.period_label
                or right_fact.get("period") != right.period_label
                or left_fact.get("period") != right_fact.get("period")
                or left_fact.get("unit") != right_fact.get("unit")
                or left_situation.item_code == right_situation.item_code
            ):
                continue
            gap = abs(left_value - right_value)
            if gap < MIN_CHANGE_GAP_PP:
                continue
            rows = [left, right]
            packet_versions = packet_for(rows)
            brief = {
                "comparison": [
                    {
                        "item": left_situation.title,
                        "value": str(left_value),
                        "unit": "%",
                        "fact_label": left_fact["label"],
                    },
                    {
                        "item": right_situation.title,
                        "value": str(right_value),
                        "unit": "%",
                        "fact_label": right_fact["label"],
                    },
                ],
                "comparison_rule": "Same topic, exact place, month-on-month measure, "
                "and reference period.",
                "difference_percentage_points": str(gap),
                "significance_reason": f"difference_at_least_{MIN_CHANGE_GAP_PP}_percentage_points",
                "interpretation_limit": (
                    "This compares measured price changes; it does not explain their causes "
                    "or imply one item caused another."
                ),
            }
            specs.append(
                _spec(
                    "multi_series_comparison",
                    rows,
                    packet_versions,
                    (
                        "same_topic",
                        "same_place",
                        "same_reference_period",
                        "same_measure_unit",
                        "difference_material",
                    ),
                    brief,
                    _merge_source_coverage(rows, coverage_by_version),
                )
            )
    for current in active:
        if current.template != "T1_price_change" or current.supersedes_id is None:
            continue
        prior = historical_versions.get(current.supersedes_id)
        situation = situations[current.situation_id]
        if (
            prior is None
            or prior.status != "superseded"
            or prior.situation_id != current.situation_id
            or not _facts_are_grounded(prior, decisions.get(prior.id))
        ):
            continue
        current_change = next(
            (
                fact
                for fact in _version_facts(current)
                if fact.get("label") == "Month-on-month change" and fact.get("unit") == "%"
            ),
            None,
        )
        prior_change = next(
            (
                fact
                for fact in _version_facts(prior)
                if fact.get("label") == "Month-on-month change" and fact.get("unit") == "%"
            ),
            None,
        )
        current_value = _number(current_change.get("value")) if current_change else None
        prior_value = _number(prior_change.get("value")) if prior_change else None
        current_period = _parse_month(current.period_label)
        prior_period = _parse_month(prior.period_label)
        if (
            current_change is None
            or prior_change is None
            or current_value is None
            or prior_value is None
            or current_period is None
            or prior_period is None
            or current_period - prior_period != 1
            or current_value == 0
            or prior_value == 0
            or (current_value > 0) != (prior_value > 0)
            or min(abs(current_value), abs(prior_value)) < MIN_CHANGE_GAP_PP
        ):
            continue
        rows = [prior, current]
        packet_versions = packet_for(rows)
        brief = {
            "trend": [
                {
                    "period": prior.period_label,
                    "value": str(prior_value),
                    "unit": "%",
                },
                {
                    "period": current.period_label,
                    "value": str(current_value),
                    "unit": "%",
                },
            ],
            "scope": current.scope_label,
            "direction": "increases" if current_value > 0 else "decreases",
            "significance_reason": "consecutive_same_direction_changes_at_least_5_percent",
            "interpretation_limit": (
                "Two consecutive monthly changes support a continuing pattern over these periods; "
                "they do not establish a cause or predict the next period."
            ),
        }
        specs.append(
            _spec(
                "persistent_trend",
                rows,
                packet_versions,
                (
                    "same_situation",
                    "consecutive_monthly_periods",
                    "same_direction",
                    "both_changes_material",
                ),
                brief,
                _merge_source_coverage(rows, coverage_by_version),
                roles=("prior", "current"),
            )
        )
    priority = {
        "material_change": 0,
        "evidence_disagreement": 1,
        "persistent_trend": 2,
        "multi_series_comparison": 3,
    }
    return sorted(specs, key=lambda spec: (priority[spec.candidate_type], spec.version_ids))[
        :MAX_CANDIDATES_PER_RUN
    ]


def _source_coverage_by_version(
    session: Session,
    versions: list[AssessmentVersion],
    decisions: dict[int, PublicationDecision],
) -> dict[int, list[dict[str, Any]]]:
    document_ids = {
        dependency.evidence_document_id
        for version in versions
        for dependency in decisions[version.id].dependencies
    }
    documents = (
        {
            document.id: (document, source)
            for document, source in session.execute(
                select(EvidenceDocument, Source)
                .join(Source, Source.id == EvidenceDocument.source_id)
                .where(EvidenceDocument.id.in_(document_ids))
            )
        }
        if document_ids
        else {}
    )
    result: dict[int, list[dict[str, Any]]] = {}
    for version in versions:
        grouped: dict[int, dict[str, Any]] = {}
        for dependency in decisions[version.id].dependencies:
            document_source = documents.get(dependency.evidence_document_id)
            if document_source is None:
                continue
            document, source = document_source
            item = grouped.setdefault(
                source.id,
                {
                    "source_id": source.id,
                    "source": source.name,
                    "permission_version": dependency.permission_version,
                    "document_ids": [],
                    "document_links": [],
                    "published_dates": [],
                    "retrieved_dates": [],
                },
            )
            item["document_ids"].append(document.id)
            item["document_links"].append(
                {
                    "document_id": document.id,
                    "url": document.url,
                    "title": document.title,
                    "published_at": document.published_at.isoformat()
                    if document.published_at
                    else None,
                }
            )
            if document.published_at:
                item["published_dates"].append(document.published_at.date().isoformat())
            item["retrieved_dates"].append(document.retrieved_at.date().isoformat())
        for item in grouped.values():
            item["document_ids"] = sorted(item["document_ids"])
            item["published_dates"] = sorted(item["published_dates"])
            item["retrieved_dates"] = sorted(item["retrieved_dates"])
        result[version.id] = sorted(grouped.values(), key=lambda item: int(item["source_id"]))
    return result


def _merge_source_coverage(
    versions: list[AssessmentVersion], coverage: dict[int, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    merged: dict[int, dict[str, Any]] = {}
    for version in versions:
        for item in coverage.get(version.id, []):
            row = merged.setdefault(
                int(item["source_id"]),
                {
                    "source_id": item["source_id"],
                    "source": item["source"],
                    "permission_versions": [],
                    "document_ids": [],
                    "document_links": [],
                    "published_dates": [],
                    "retrieved_dates": [],
                },
            )
            row["permission_versions"].append(item["permission_version"])
            for key in ("document_ids", "published_dates", "retrieved_dates", "document_links"):
                row[key].extend(item[key])
    for row in merged.values():
        for key in ("permission_versions", "document_ids", "published_dates", "retrieved_dates"):
            row[key] = sorted(set(row[key]))
        row["document_links"] = sorted(
            {link["document_id"]: link for link in row["document_links"]}.values(),
            key=lambda link: int(link["document_id"]),
        )
    return sorted(merged.values(), key=lambda item: int(item["source_id"]))


def mark_stale_candidates(session: Session, now: datetime) -> int:
    candidates = list(
        session.scalars(
            select(InsightCandidate)
            .where(InsightCandidate.status.in_(("pending_review", "selected")))
            .order_by(InsightCandidate.created_at)
            .limit(MAX_CANDIDATES_TO_REFRESH)
            .with_for_update(skip_locked=True)
        )
    )
    if not candidates:
        return 0
    relations = session.execute(
        select(
            InsightCandidateAssessment.candidate_id,
            InsightCandidateAssessment.role,
            AssessmentVersion,
        )
        .select_from(InsightCandidateAssessment)
        .join(
            AssessmentVersion,
            AssessmentVersion.id == InsightCandidateAssessment.assessment_version_id,
        )
        .where(InsightCandidateAssessment.candidate_id.in_([row.id for row in candidates]))
    ).all()
    versions_by_candidate: dict[int, list[tuple[str, AssessmentVersion]]] = {
        row.id: [] for row in candidates
    }
    for candidate_id, role, version in relations:
        versions_by_candidate[candidate_id].append((role, version))
    all_versions = [version for rows in versions_by_candidate.values() for _role, version in rows]
    decisions = publication_eligibility_many(
        session, all_versions, channel="website", now=now, require_current=False
    )
    situations = {
        situation.id: situation
        for situation in session.scalars(
            select(Situation).where(
                Situation.id.in_({version.situation_id for version in all_versions})
            )
        )
    }
    stale_count = 0
    for candidate in candidates:
        member_rows = versions_by_candidate[candidate.id]
        current_versions = {
            version.situation_id: version for role, version in member_rows if role != "prior"
        }
        stale = not member_rows or any(
            not decisions.get(version.id)
            or not decisions[version.id].eligible
            or (version.valid_until is not None and version.valid_until <= now)
            or (
                role == "prior"
                and (
                    version.status != "superseded"
                    or current_versions.get(version.situation_id) is None
                    or current_versions[version.situation_id].supersedes_id != version.id
                )
            )
            or (
                role != "prior"
                and (
                    version.status != "published"
                    or situations.get(version.situation_id) is None
                    or situations[version.situation_id].current_version_id != version.id
                )
            )
            for role, version in member_rows
        )
        if stale:
            before = candidate.status
            candidate.status = "stale"
            candidate.stale_reason = "Assessment eligibility or current evidence changed."
            candidate.reviewed_at = now
            session.flush()
            audit.record_system(
                session,
                "insight_candidate.stale",
                "insight_candidate",
                candidate.id,
                before={"status": before},
                after={"status": "stale", "reason": candidate.stale_reason},
            )
            stale_count += 1
    return stale_count


def refresh_and_generate(session: Session, now: datetime | None = None) -> dict[str, int]:
    """Refresh existing packets and store new eligible candidates; caller commits."""
    now = now or datetime.now(UTC)
    stale = mark_stale_candidates(session, now)
    versions = list(
        session.scalars(
            select(AssessmentVersion)
            .join(Situation, Situation.id == AssessmentVersion.situation_id)
            .where(
                AssessmentVersion.status == "published",
                or_(
                    AssessmentVersion.severity.in_(("medium", "high")),
                    AssessmentVersion.evidence_state == "disputed",
                ),
                Situation.status == "active",
                Situation.current_version_id == AssessmentVersion.id,
                (AssessmentVersion.valid_until.is_(None)) | (AssessmentVersion.valid_until > now),
            )
            .order_by(AssessmentVersion.published_at.desc(), AssessmentVersion.id.desc())
            .limit(MAX_VERSIONS)
        )
    )
    historical_ids = {
        version.supersedes_id for version in versions if version.supersedes_id is not None
    }
    historical_versions = (
        {
            version.id: version
            for version in session.scalars(
                select(AssessmentVersion).where(AssessmentVersion.id.in_(historical_ids))
            )
        }
        if historical_ids
        else {}
    )
    all_versions = [*versions, *historical_versions.values()]
    situations = {
        situation.id: situation
        for situation in session.scalars(
            select(Situation).where(
                Situation.id.in_({version.situation_id for version in all_versions})
            )
        )
    }
    decisions = publication_eligibility_many(session, versions, channel="website", now=now)
    if historical_versions:
        decisions.update(
            publication_eligibility_many(
                session,
                list(historical_versions.values()),
                channel="website",
                now=now,
                require_current=False,
            )
        )
    specs = _specs_for_current(session, versions, historical_versions, situations, decisions)
    inserted = 0
    for spec in specs:
        candidate_id = session.execute(
            pg_insert(InsightCandidate)
            .values(
                candidate_type=spec.candidate_type,
                dedupe_key=spec.dedupe_key,
                status="pending_review",
                reason_codes=list(spec.reason_codes),
                packet=spec.packet,
                created_at=now,
            )
            .on_conflict_do_nothing(index_elements=[InsightCandidate.dedupe_key])
            .returning(InsightCandidate.id)
        ).scalar_one_or_none()
        if candidate_id is None:
            continue
        session.add_all(
            InsightCandidateAssessment(
                candidate_id=candidate_id,
                assessment_version_id=version_id,
                role=spec.roles[ordinal],
                ordinal=ordinal,
            )
            for ordinal, version_id in enumerate(spec.version_ids)
        )
        audit.record_system(
            session,
            "insight_candidate.created",
            "insight_candidate",
            candidate_id,
            after={
                "candidate_type": spec.candidate_type,
                "assessment_version_ids": list(spec.version_ids),
                "reason_codes": list(spec.reason_codes),
                "selection_version": PROMPT_VERSION,
            },
        )
        inserted += 1
    return {"created": inserted, "marked_stale": stale}


def recent_candidates(session: Session, limit: int = 50) -> list[InsightCandidate]:
    return list(
        session.scalars(
            select(InsightCandidate)
            .order_by(InsightCandidate.created_at.desc(), InsightCandidate.id.desc())
            .limit(limit)
        )
    )
