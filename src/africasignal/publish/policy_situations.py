"""T2 policy situations and their assessment versions (spec B8.1, B8.3, AS-027).

``ensure_policy_situations`` creates the situations ``config/policies.yaml`` lists: one per policy
series and scope place (the country, or each state of a ``states`` scope).
``assess_policy_situation`` loads the valid ``policy_statement`` claims for the series, runs the
pure computation in ``assess.policy_change`` and stores a new draft version unless the inputs are
unchanged. The publication policy (``publish.versions.apply_policy``) then decides what readers
see, exactly as for T1.

The place of a claim is not used: a policy series is about one regulator's rate, and who is
affected comes from ``config/policies.yaml``, never from the claim.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.assess.corroboration import is_official
from africasignal.assess.policy_change import (
    TEMPLATE_VERSION,
    PolicyAssessment,
    PolicyInputs,
    compute_policy_change,
)
from africasignal.assess.price_change import place_phrase
from africasignal.catalog import PolicySeries, load_policies
from africasignal.models import (
    AssessmentInput,
    AssessmentVersion,
    Claim,
    Place,
    Situation,
)
from africasignal.publish.situations import (
    POLICY_UNAPPLIED,
    AssessmentOutcome,
    load_claim_points,
    mark_official_copies,
)

log = logging.getLogger("africasignal.publish.policy_situations")


def policy_slug(series_code: str, place_code: str) -> str:
    """``policy-electricity_tariff_band_a-ikeja-electric-ng-la``."""
    return "policy-" + re.sub(r"[^a-z0-9_]+", "-", f"{series_code}:{place_code}".lower()).strip("-")


def _scope_codes(series: PolicySeries) -> list[str]:
    return ["NG"] if series.scope == "national" else list(series.state_codes)


def ensure_policy_situations(
    session: Session, series_codes: set[str] | None = None
) -> list[Situation]:
    """The T2 situations for the configured series (all of them, or just ``series_codes``),
    created when missing. A scope place that is not in the database yet is skipped."""
    found: list[Situation] = []
    for series in load_policies().series:
        if series_codes is not None and series.code not in series_codes:
            continue
        codes = _scope_codes(series)
        for code in codes:
            place = session.scalars(select(Place).where(Place.code == code)).one_or_none()
            if place is None or place.kind not in ("country", "state"):
                continue
            slug = policy_slug(series.code, code)
            situation = session.scalars(
                select(Situation).where(Situation.slug == slug)
            ).one_or_none()
            if situation is None:
                title = series.title
                if len(codes) > 1:
                    title += f" in {place_phrase(place.kind, code, place.name)}"
                situation = Situation(
                    slug=slug,
                    kind="policy",
                    topic=series.topic,
                    title=title,
                    policy_series=series.code,
                    place_id=place.id,
                )
                session.add(situation)
                session.flush()
            found.append(situation)
    return found


def load_policy_inputs(
    session: Session, situation: Situation, now: datetime
) -> PolicyInputs | None:
    """Everything the T2 computation needs, or None when the series or place is unknown."""
    series = next((s for s in load_policies().series if s.code == situation.policy_series), None)
    place = session.get(Place, situation.place_id)
    if series is None or place is None or place.code is None:
        return None
    if place.kind not in ("country", "state"):
        return None
    claims = load_claim_points(
        session,
        now,
        Claim.policy_series == series.code,
        Claim.claim_type == "policy_statement",
    )
    claims = mark_official_copies(
        session, claims, {c.evidence_document_id for c in claims if is_official(c)}
    )
    return PolicyInputs(
        series_code=series.code,
        title=series.title,
        unit=series.unit,
        affected_groups=series.affected_groups,
        materiality_pct=Decimal(str(series.materiality_pct)),
        place_code=place.code,
        place_name=place.name,
        place_kind="country" if place.kind == "country" else "state",
        claims=tuple(claims),
        now=now,
    )


def _change_summary(previous: AssessmentVersion | None, new: PolicyAssessment) -> str | None:
    if previous is None:
        return None
    if previous.period_label != new.period_label:
        return (
            f"Updated: the rate now in force is {new.period_label} "
            f"(previously {previous.period_label})"
        )
    return "Re-assessed after the inputs changed"


def assess_policy_situation(
    session: Session, situation: Situation, now: datetime, *, correction: str | None = None
) -> AssessmentOutcome:
    """Compute the situation's T2 assessment and store a new version if the inputs changed.

    Mirrors ``publish.situations.assess_situation``: the same inputs give the same ``inputs_hash``
    and then only the latest version's ``last_checked_at`` moves. ``correction`` becomes the new
    version's ``change_summary`` (spec B9). The caller commits.
    """
    inputs = load_policy_inputs(session, situation, now)
    if inputs is None:
        return AssessmentOutcome("skipped", reason="policy series or place is not configured")
    computed = compute_policy_change(inputs)
    if computed is None:
        return AssessmentOutcome("skipped", reason="no usable policy claims")

    latest = session.scalars(
        select(AssessmentVersion)
        .where(AssessmentVersion.situation_id == situation.id)
        .order_by(AssessmentVersion.version.desc())
        .limit(1)
    ).first()
    if latest is not None and latest.inputs_hash == computed.inputs_hash:
        latest.last_checked_at = now
        return AssessmentOutcome("unchanged", latest)

    version = AssessmentVersion(
        situation_id=situation.id,
        version=1 if latest is None else latest.version + 1,
        template="T2_policy_change",
        template_version=TEMPLATE_VERSION,
        policy_version=POLICY_UNAPPLIED,
        inputs_hash=computed.inputs_hash,
        status="draft",
        evidence_state=computed.evidence_state,
        severity=computed.severity,
        headline=computed.headline,
        facts=computed.facts,
        possible_factors=computed.possible_factors,
        unknowns=computed.unknowns,
        scope_label=computed.scope_label,
        period_label=computed.period_label,
        last_checked_at=now,
        valid_until=computed.valid_until,
        change_summary=correction or _change_summary(latest, computed),
        withheld_reasons=[],
        supersedes_id=latest.id if latest else None,
    )
    session.add(version)
    session.flush()
    session.add_all(
        AssessmentInput(assessment_version_id=version.id, input_kind=kind, input_id=id_)
        for kind, id_ in computed.inputs
    )
    session.flush()
    log.info(
        "assessment %s v%d: %s",
        situation.slug,
        version.version,
        computed.headline,
        extra={"evidence_state": computed.evidence_state, "severity": computed.severity},
    )
    return AssessmentOutcome("created", version)
