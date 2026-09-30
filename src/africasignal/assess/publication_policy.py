"""Publication policy ``pp-1`` (spec B8.5): what happens to a new assessment version.

A pure function: ``decide`` gets everything it needs in a ``VersionDraft`` and returns a
``Decision``. Reading the database (``publish/versions.py``) and acting on the decision are kept
apart so every rule can be tested with plain values.

Rules, in the order they are applied:

=====  ========================================================  ================================
Rule   Condition                                                 Result
=====  ========================================================  ================================
R1     publication is suspended (the kill switch)                withheld
R2     any fact has no evidence, or its evidence is not active   withheld (no facts counts too)
R4     T1: a newer or equal value for the same item and place    withheld
       failed range validation and is still waiting for review
R6     a fact is more precise than the situation's scope         withheld (a bug guard)
R8     same ``inputs_hash`` as the published version             unchanged (no new version)
R5     T2: no primary document                                   published as insufficient
R3     evidence state is ``insufficient``                        published as insufficient
R7     severity ``high`` and nothing of the situation was ever   held for 60 minutes, then
       published                                                 published unless withheld
=====  ========================================================  ================================

The "withheld" rules (R1, R2, R4, R6) are all checked, so the operator sees every reason at
once. Stale data is not hidden: R3 and R5 publish a card that says the
evidence is insufficient and still shows the facts, the period and when it was last checked.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

POLICY_VERSION = "pp-1"
HOLD_MINUTES = 60

Template = Literal["T1_price_change", "T2_policy_change"]
Precision = Literal["national", "state", "lga", "city", "unknown"]
Status = Literal["published", "withheld", "held", "unchanged"]

_PRECISION_RANK: dict[str, int] = {"national": 0, "state": 1, "lga": 2, "city": 3, "unknown": 4}


@dataclass(frozen=True)
class FactDraft:
    label: str
    evidence_ids: tuple[int, ...]
    place_precision: Precision


@dataclass(frozen=True)
class VersionDraft:
    template: Template
    evidence_state: str  # reported | corroborated | disputed | insufficient
    severity: str  # none | low | medium | high
    scope_precision: Precision
    facts: tuple[FactDraft, ...]
    inputs_hash: str
    now: datetime
    # what the database says
    publication_suspended: bool = False
    active_evidence_ids: frozenset[int] = field(default_factory=frozenset)
    current_published_hash: str | None = None
    ever_published: bool = False  # any version of this situation was ever published
    range_failure_pending: bool = False  # T1: a value awaits range-validation review
    has_primary_document: bool = True  # T2


@dataclass(frozen=True)
class Decision:
    status: Status
    reasons: tuple[str, ...] = ()  # rule ids that decided or shaped the outcome
    hold_until: datetime | None = None  # R7
    insufficient_card: bool = False  # R3, R5: publish as an "insufficient evidence" card


def precision_of(place_code: str) -> Precision:
    """Precision of a place code: ``NG`` national, ``NG-LA`` state, anything longer is finer."""
    if place_code == "NG":
        return "national"
    parts = place_code.split("-")
    if len(parts) == 2 and parts[0] == "NG":
        return "state"
    if place_code.startswith("city:"):
        return "city"
    if len(parts) > 2 or place_code.startswith("hood:"):
        return "lga"
    return "unknown"


def _withholding_reasons(draft: VersionDraft) -> list[str]:
    reasons: list[str] = []
    if draft.publication_suspended:
        reasons.append("R1")
    if not draft.facts or any(
        not f.evidence_ids or not set(f.evidence_ids) <= draft.active_evidence_ids
        for f in draft.facts
    ):
        reasons.append("R2")
    if draft.template == "T1_price_change" and draft.range_failure_pending:
        reasons.append("R4")
    scope = _PRECISION_RANK[draft.scope_precision]
    if any(_PRECISION_RANK[f.place_precision] > scope for f in draft.facts):
        reasons.append("R6")
    return reasons


def decide(draft: VersionDraft) -> Decision:
    withheld = _withholding_reasons(draft)
    if withheld:
        return Decision("withheld", tuple(withheld))
    if (
        draft.current_published_hash is not None
        and draft.inputs_hash == draft.current_published_hash
    ):
        return Decision("unchanged", ("R8",))
    if draft.template == "T2_policy_change" and not draft.has_primary_document:
        return Decision("published", ("R5",), insufficient_card=True)
    if draft.evidence_state == "insufficient":
        return Decision("published", ("R3",), insufficient_card=True)
    if draft.severity == "high" and not draft.ever_published:
        return Decision("held", ("R7",), hold_until=draft.now + timedelta(minutes=HOLD_MINUTES))
    return Decision("published")
