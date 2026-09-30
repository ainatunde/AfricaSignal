"""What readers are told when an assessment is corrected or withdrawn (spec B9). Pure text.

The wording is built in code from the changed values, like every other sentence about a number.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

InvalidationKind = str  # "measurement" | "claim" | "evidence_document"


@dataclass(frozen=True)
class Revision:
    """A restated figure: the month it is for, and the old and new value."""

    period: date
    old: Decimal
    new: Decimal


def _naira(value: Decimal) -> str:
    return f"₦{value:,.2f}"


def correction_for_revisions(source_short: str, revisions: Sequence[Revision]) -> str:
    """ "Corrected: NBS revised the July figure from ₦X to ₦Y". With several figures, the newest
    month's change is given as the example."""
    if not revisions:
        raise ValueError("a correction needs at least one revision")
    ordered = sorted(revisions, key=lambda r: (r.period, r.old, r.new))
    if len(ordered) == 1:
        r = ordered[0]
        return (
            f"Corrected: {source_short} revised the {r.period:%B %Y} figure "
            f"from {_naira(r.old)} to {_naira(r.new)}"
        )
    latest = ordered[-1]
    return (
        f"Corrected: {source_short} revised {len(ordered)} figures used in this assessment, "
        f"for example the {latest.period:%B %Y} figure from {_naira(latest.old)} "
        f"to {_naira(latest.new)}"
    )


def correction_for_withdrawn_document() -> str:
    return "Corrected: a source document behind this assessment was withdrawn"


def correction_for_invalid_claim() -> str:
    return "Corrected: a report behind this assessment was found to be invalid"


def withdrawal_headline(reason: str) -> str:
    return f"Withdrawn: {reason}"


WITHDRAWAL_NO_EVIDENCE = "the evidence behind this assessment was withdrawn and nothing replaces it"
