"""Policy situations page (spec B8.1, B11.5): the T2 series in force and a form to add one.

``config/policies.yaml`` stays the base list and is read-only here. A series an operator adds is
stored in ``operator_policy_series`` and, from that moment, is offered to claim extraction, accepted
by the NERC tariff reader and given its situations. Retiring a series stops all three for new work;
situations and versions already published stay (withdraw them on the Assessments page). Affected
groups are typed by the operator and never inferred by the model (B8.3). Every change is audited in
the same transaction."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal import audit
from africasignal.assess.corroboration import OFFICIAL_SOURCE_KINDS
from africasignal.catalog import PolicySeries, load_policies
from africasignal.models import (
    AssessmentVersion,
    Operator,
    OperatorPolicySeries,
    Place,
    Situation,
    Source,
)
from africasignal.policy_series import to_series
from africasignal.publish.policy_situations import ensure_policy_situations, policy_slug
from africasignal.textclean import one_line

_CODE = re.compile(r"^[a-z0-9_]{2,60}(:[a-z0-9][a-z0-9-]{0,59})?$")
_STATE = re.compile(r"^NG-[A-Z]{2}$")
MAX_TITLE = 120
MAX_UNIT = 30
MAX_GROUPS = 200
TOPICS = ("energy", "food")


class PolicyError(ValueError):
    """A refusal the operator should see."""


@dataclass(frozen=True)
class SituationRow:
    situation: Situation
    version: AssessmentVersion | None


@dataclass
class SeriesRow:
    series: PolicySeries
    from_file: bool
    row_id: int | None  # the console row, None for a series that comes from the file
    active: bool
    situations: list[SituationRow] = field(default_factory=list)


@dataclass(frozen=True)
class NewSeries:
    code: str
    title: str
    topic: str
    unit: str
    primary_sources: list[str]
    scope: str
    state_codes: str  # comma or space separated, as typed
    affected_groups: str
    materiality_pct: str = "5"


def _situations(session: Session, code: str) -> list[SituationRow]:
    rows = session.execute(
        select(Situation, AssessmentVersion)
        .outerjoin(AssessmentVersion, AssessmentVersion.id == Situation.current_version_id)
        .where(Situation.kind == "policy", Situation.policy_series == code)
        .order_by(Situation.slug)
    )
    return [SituationRow(s, v) for s, v in rows]


def listing(session: Session) -> list[SeriesRow]:
    """File series first, then console ones, each with its situations."""
    result = [
        SeriesRow(s, True, None, True, _situations(session, s.code)) for s in load_policies().series
    ]
    file_codes = {r.series.code for r in result}
    for row in session.scalars(select(OperatorPolicySeries).order_by(OperatorPolicySeries.id)):
        if row.code in file_codes:  # the file wins; a clash cannot be created here
            continue
        result.append(
            SeriesRow(to_series(row), False, row.id, row.active, _situations(session, row.code))
        )
    return result


def source_choices(session: Session) -> list[Source]:
    """Sources that can state a policy: regulators, government bodies, statistics offices and
    companies. News outlets and aggregators only report on policy, they never set a rate."""
    return list(
        session.scalars(
            select(Source).where(Source.kind.in_(OFFICIAL_SOURCE_KINDS)).order_by(Source.name)
        )
    )


def _parse_states(session: Session, typed: str) -> list[str]:
    codes = list(dict.fromkeys(c.upper() for c in re.split(r"[\s,;]+", one_line(typed)) if c))
    if not codes:
        raise PolicyError("give at least one state code, for example NG-LA")
    bad = [c for c in codes if not _STATE.match(c)]
    if bad:
        raise PolicyError(f"not state codes (they look like NG-LA): {', '.join(bad)}")
    known = set(
        session.scalars(select(Place.code).where(Place.kind == "state", Place.code.in_(codes)))
    )
    unknown = [c for c in codes if c not in known]
    if unknown:
        raise PolicyError(f"no such state in the places list: {', '.join(unknown)}")
    return codes


def _validate(session: Session, new: NewSeries) -> tuple[PolicySeries, str]:
    code = one_line(new.code)
    if not _CODE.match(code):
        raise PolicyError(
            "the code is lower case letters, digits and underscores, optionally followed by "
            "a colon and a short name, for example electricity_tariff_band_a:abuja-electricity"
        )
    taken = {s.code for s in load_policies().series}
    taken |= set(session.scalars(select(OperatorPolicySeries.code)))
    if code in taken:
        raise PolicyError("a policy series with that code already exists")
    title, unit, groups = one_line(new.title), one_line(new.unit), one_line(new.affected_groups)
    if not 3 <= len(title) <= MAX_TITLE:
        raise PolicyError(f"give a title of 3 to {MAX_TITLE} characters")
    if not 1 <= len(unit) <= MAX_UNIT:
        raise PolicyError(f"give the unit the rate is stated in (up to {MAX_UNIT} characters)")
    if not 3 <= len(groups) <= MAX_GROUPS:
        raise PolicyError(
            f"say who is affected, in 3 to {MAX_GROUPS} characters. The model never guesses this"
        )
    if new.topic not in TOPICS:
        raise PolicyError("choose a topic")
    if new.scope not in ("national", "states"):
        raise PolicyError("choose whether the policy is national or for named states")
    slugs = list(dict.fromkeys(one_line(s) for s in new.primary_sources if one_line(s)))
    if not slugs:
        raise PolicyError("choose at least one source that publishes this policy")
    found = {
        slug: kind
        for slug, kind in session.execute(
            select(Source.slug, Source.kind).where(Source.slug.in_(slugs))
        )
    }
    if set(found) != set(slugs):
        raise PolicyError("one of the chosen sources does not exist")
    if any(kind not in OFFICIAL_SOURCE_KINDS for kind in found.values()):
        raise PolicyError(
            "a policy's primary source must be a regulator, government body, statistics office "
            "or company; news outlets only report on it"
        )
    try:
        pct = Decimal(one_line(new.materiality_pct) or "5")
    except InvalidOperation as exc:
        raise PolicyError("the materiality threshold must be a number") from exc
    if not Decimal("0.1") <= pct <= Decimal("100"):
        raise PolicyError("the materiality threshold must be between 0.1 and 100 percent")
    states: list[str] = []
    if new.scope == "states":
        states = _parse_states(session, new.state_codes)
    elif new.state_codes.strip():
        raise PolicyError("a national series has no states: clear the state field")
    series = PolicySeries(
        code=code,
        title=title,
        topic=new.topic,  # type: ignore[arg-type]
        unit=unit,
        primary_sources=slugs,
        scope=new.scope,  # type: ignore[arg-type]
        state_codes=states,
        affected_groups=groups,
        materiality_pct=float(pct),
    )
    return series, str(pct)


def _check_slugs_free(session: Session, series: PolicySeries) -> None:
    """Two codes can make the same slug once punctuation is flattened; refuse before creating."""
    codes = ["NG"] if series.scope == "national" else series.state_codes
    slugs = [policy_slug(series.code, c) for c in codes]
    clash = session.scalars(select(Situation.slug).where(Situation.slug.in_(slugs))).first()
    if clash:
        raise PolicyError(f"a situation called {clash} already exists; choose another code")


def add_series(
    session: Session, operator: Operator, new: NewSeries
) -> tuple[OperatorPolicySeries, list[Situation]]:
    """Store the series and create its situations. The list is empty when the places they would
    cover are not loaded yet; they are created the next time a claim for the series arrives."""
    series, pct = _validate(session, new)
    _check_slugs_free(session, series)
    row = OperatorPolicySeries(
        code=series.code,
        title=series.title,
        topic=series.topic,
        unit=series.unit,
        primary_sources=series.primary_sources,
        scope=series.scope,
        state_codes=series.state_codes,
        affected_groups=series.affected_groups,
        materiality_pct=Decimal(pct),
        active=True,
        created_by_operator_id=operator.id,
    )
    session.add(row)
    session.flush()
    created = ensure_policy_situations(session, {series.code})
    audit.record(
        session,
        operator,
        "policy_series.add",
        "policy_series",
        row.id,
        after={
            "code": series.code,
            "title": series.title,
            "topic": series.topic,
            "unit": series.unit,
            "primary_sources": series.primary_sources,
            "scope": series.scope,
            "state_codes": series.state_codes,
            "affected_groups": series.affected_groups,
            "materiality_pct": pct,
            "situations": [s.slug for s in created],
        },
    )
    return row, created


def set_active(
    session: Session, operator: Operator, row_id: int, active: bool
) -> OperatorPolicySeries:
    row = session.scalars(
        select(OperatorPolicySeries).where(OperatorPolicySeries.id == row_id).with_for_update()
    ).first()
    if row is None:
        raise PolicyError("no such policy series (series in policies.yaml cannot be changed here)")
    if row.active == active:
        raise PolicyError("it is already " + ("active" if active else "retired"))
    row.active = active
    session.flush()
    if active:
        ensure_policy_situations(session, {row.code})
    audit.record(
        session,
        operator,
        "policy_series.activate" if active else "policy_series.retire",
        "policy_series",
        row.id,
        before={"active": not active},
        after={"active": active, "code": row.code},
    )
    return row
