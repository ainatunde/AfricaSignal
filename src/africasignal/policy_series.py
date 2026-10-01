"""The T2 policy series in force: the ones in ``config/policies.yaml`` plus the active ones an
operator added in the console (spec B8.1). Code that needs "every series" asks here, not
``catalog.load_policies``, so an added series takes effect without a deploy: the model is offered
its code, the NERC adapter accepts it, and its situations are created."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.catalog import PolicySeries, load_policies
from africasignal.models import OperatorPolicySeries


def to_series(row: OperatorPolicySeries) -> PolicySeries:
    return PolicySeries(
        code=row.code,
        title=row.title,
        topic=row.topic,  # type: ignore[arg-type]
        unit=row.unit,
        primary_sources=list(row.primary_sources),
        scope=row.scope,  # type: ignore[arg-type]
        state_codes=list(row.state_codes),
        affected_groups=row.affected_groups,
        materiality_pct=float(row.materiality_pct),
    )


def all_series(session: Session | None = None) -> list[PolicySeries]:
    """File series first, then active operator-added ones in the order they were added. With no
    session only the file is read. A code in both places keeps the file's definition."""
    found = list(load_policies().series)
    if session is None:
        return found
    taken = {s.code for s in found}
    rows = session.scalars(
        select(OperatorPolicySeries)
        .where(OperatorPolicySeries.active.is_(True))
        .order_by(OperatorPolicySeries.id)
    )
    found.extend(to_series(r) for r in rows if r.code not in taken)
    return found


def series_codes(session: Session | None = None) -> set[str]:
    return {s.code for s in all_series(session)}
