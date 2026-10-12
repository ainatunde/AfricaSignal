"""Assessments page (spec B11.5, B8.5): newest versions, the R7 hold queue, and the three things
an operator may do: withhold a held draft, release a held draft early, withdraw a published
situation. The publication policy still decides: releasing early re-runs it, so an operator cannot
publish what a rule would withhold. Every action needs a reason and is audited."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from africasignal import audit
from africasignal.models import AssessmentVersion, Operator, Situation
from africasignal.operations.paging import Page, page_of
from africasignal.publish import versions
from africasignal.publish.invalidation import withdraw_situation
from africasignal.textclean import one_line

MIN_REASON = 8
MAX_REASON = 200
# What the explanation check also refuses in text readers see: links, @ signs and markup.
_NOT_PUBLIC = re.compile(r"https?:|www\.|@|[<>`*]|\[[^\]]*\]\(", re.IGNORECASE)
STATUS_FILTERS = ("all", "published", "draft", "withheld", "stale", "withdrawn", "superseded")


class AssessmentError(ValueError):
    """A refusal the operator should see."""


@dataclass(frozen=True)
class VersionRow:
    situation: Situation
    version: AssessmentVersion
    is_current: bool


def clean_reason(reason: str) -> str:
    """One line, trimmed, of a sensible length."""
    text = one_line(reason)
    if len(text) < MIN_REASON:
        raise AssessmentError(f"give a reason of at least {MIN_REASON} characters")
    if len(text) > MAX_REASON:
        raise AssessmentError(f"keep the reason under {MAX_REASON} characters")
    return text


def clean_public_reason(reason: str) -> str:
    """A reason readers will see: ``clean_reason`` plus the rule that public text carries no
    links, @ signs or markup (the same rule as the explanation check)."""
    text = clean_reason(reason)
    if _NOT_PUBLIC.search(text):
        raise AssessmentError(
            "readers will see this reason, so write it as plain text: no links, @ signs or markup"
        )
    return text


VERSIONS_PER_PAGE = 50
HELD_PER_PAGE = 20


def recent_versions(
    session: Session, *, status: str = "all", page: str | None = None
) -> tuple[list[VersionRow], Page]:
    """One page of versions, newest first, and where that page sits in the whole list."""
    where = []
    if status in STATUS_FILTERS and status != "all":
        where.append(AssessmentVersion.status == status)
    total = session.scalar(
        select(func.count())
        .select_from(AssessmentVersion)
        .join(Situation, AssessmentVersion.situation_id == Situation.id)
        .where(*where)
    )
    where_page = page_of(page, total or 0, VERSIONS_PER_PAGE)
    rows = session.execute(
        select(Situation, AssessmentVersion)
        .join(AssessmentVersion, AssessmentVersion.situation_id == Situation.id)
        .where(*where)
        .order_by(AssessmentVersion.id.desc())
        .limit(where_page.size)
        .offset(where_page.offset)
    )
    return (
        [
            VersionRow(situation, version, situation.current_version_id == version.id)
            for situation, version in rows
        ],
        where_page,
    )


def held_queue(session: Session, page: str | None = None) -> tuple[list[VersionRow], Page]:
    """One page of the first high-severity versions waiting out the R7 hold, soonest release
    first, and where that page sits in the whole queue."""
    waiting = (
        AssessmentVersion.status == "draft",
        AssessmentVersion.hold_until.is_not(None),
    )
    total = session.scalar(select(func.count()).select_from(AssessmentVersion).where(*waiting))
    held_page = page_of(page, total or 0, HELD_PER_PAGE)
    rows = session.execute(
        select(Situation, AssessmentVersion)
        .join(AssessmentVersion, AssessmentVersion.situation_id == Situation.id)
        .where(*waiting)
        .order_by(AssessmentVersion.hold_until, AssessmentVersion.id)
        .limit(held_page.size)
        .offset(held_page.offset)
    )
    return [VersionRow(s, v, False) for s, v in rows], held_page


def _held_version(session: Session, version_id: int) -> tuple[Situation, AssessmentVersion]:
    version = session.scalars(
        select(AssessmentVersion).where(AssessmentVersion.id == version_id).with_for_update()
    ).first()
    if version is None:
        raise AssessmentError("no such version")
    if version.status != "draft" or version.hold_until is None:
        raise AssessmentError("that version is not waiting in the review hold any more")
    situation = session.get(Situation, version.situation_id)
    assert situation is not None
    return situation, version


def withhold(
    session: Session, operator: Operator, version_id: int, reason: str
) -> AssessmentVersion:
    reason = clean_reason(reason)
    situation, version = _held_version(session, version_id)
    before = {"status": version.status, "hold_until": version.hold_until.isoformat()}  # type: ignore[union-attr]
    versions.withhold_version(session, version.id, [f"operator: {reason}"])
    audit.record(
        session,
        operator,
        "assessment.withhold",
        "assessment_version",
        version.id,
        before={"situation": situation.slug, "version": version.version, **before},
        after={"status": "withheld", "reason": reason},
    )
    return version


@dataclass(frozen=True)
class ReleaseResult:
    version: AssessmentVersion
    published: bool
    withheld_by: list[str]


def release_early(
    session: Session,
    operator: Operator,
    version_id: int,
    reason: str,
    now: datetime | None = None,
) -> ReleaseResult:
    """Publish a held version now instead of at the end of the hold. The policy is applied again
    (the kill switch, range checks and every other rule); if a rule withholds it, so it is, and
    the result says which rules. Either way the outcome and the audit row are kept."""
    now = now or datetime.now(UTC)
    reason = clean_reason(reason)
    situation, version = _held_version(session, version_id)
    if versions.has_newer_version(session, version):
        raise AssessmentError(
            "a newer version of this situation exists, so publishing this one would replace it "
            "with older figures; withhold this one and judge the newer version instead"
        )
    held_until = version.hold_until.isoformat()  # type: ignore[union-attr]
    outcome = versions.release_version(session, version, now)
    audit.record(
        session,
        operator,
        "assessment.release_early",
        "assessment_version",
        version.id,
        before={
            "situation": situation.slug,
            "version": version.version,
            "status": "draft",
            "hold_until": held_until,
        },
        after={"status": version.status, "reason": reason, "withheld_by": version.withheld_reasons},
    )
    return ReleaseResult(
        version, outcome == "published", [str(r) for r in version.withheld_reasons]
    )


def withdraw(
    session: Session,
    operator: Operator,
    situation_id: int,
    reason: str,
    now: datetime | None = None,
) -> AssessmentVersion:
    """Replace the current version with a withdrawn one. The reason is shown to readers as
    "Withdrawn: <reason>", so it is written for them."""
    now = now or datetime.now(UTC)
    reason = clean_public_reason(reason)
    situation = session.scalars(
        select(Situation).where(Situation.id == situation_id).with_for_update()
    ).first()
    if situation is None:
        raise AssessmentError("no such situation")
    previous_id = situation.current_version_id
    new = withdraw_situation(session, situation, now, reason[0].lower() + reason[1:])
    if new is None:
        raise AssessmentError("there is no published version to withdraw")
    from africasignal.operations.commercial_invalidation import invalidate_situation_contexts

    invalidate_situation_contexts(session, situation.id, reason="content_withdrawn", now=now)
    audit.record(
        session,
        operator,
        "assessment.withdraw",
        "situation",
        situation.id,
        before={"slug": situation.slug, "current_version_id": previous_id},
        after={"current_version_id": new.id, "status": "withdrawn", "reason": reason},
    )
    return new
