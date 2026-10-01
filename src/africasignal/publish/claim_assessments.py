"""Re-assessing situations when a document's claims arrive (spec B8.2, B8.3, AS-027).

Claims are what corroborate, dispute or explain an official figure, so a document that yields
valid claims may change the assessment of a situation that already has one:

* T1: a claim about an item at a place affects the price situation for that item at the place, or
  at the state the place is in;
* T2: a claim for a policy series affects that series' situations, which are created here on the
  first claim so a series only has situations once there is something to assess.

``request_claim_assessments`` queues one ``assess_situation`` job per affected situation. The job's
dedupe key names the newest claim of the document, so claims that arrive later (place resolution,
a second extraction) queue a new assessment instead of being swallowed by the first.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.jobs import queue
from africasignal.models import Claim, Place, Situation
from africasignal.publish.policy_situations import ensure_policy_situations


def _state_and_self(session: Session, place_id: int) -> set[int]:
    """The place and the places above it that have a situation scope (a state)."""
    ids = {place_id}
    place = session.get(Place, place_id)
    while place is not None and place.parent_id is not None:
        place = session.get(Place, place.parent_id)
        if place is not None and place.kind == "state":
            ids.add(place.id)
    return ids


def request_claim_assessments(session: Session, document_id: int) -> list[int]:
    """Queue assessments for the situations a document's valid claims touch. Returns job ids."""
    claims = list(
        session.scalars(
            select(Claim).where(Claim.evidence_document_id == document_id, Claim.valid.is_(True))
        )
    )
    if not claims:
        return []
    newest = max(c.id for c in claims)

    situation_ids: set[int] = set()
    pairs = {
        (c.item_code, pid)
        for c in claims
        if c.item_code and c.place_id is not None and c.claim_type in ("price_statement", "other")
        for pid in _state_and_self(session, c.place_id)
    }
    for item_code, place_id in pairs:
        situation_ids |= set(
            session.scalars(
                select(Situation.id).where(
                    Situation.kind == "price_series",
                    Situation.item_code == item_code,
                    Situation.place_id == place_id,
                )
            )
        )
    series = {
        c.policy_series for c in claims if c.policy_series and c.claim_type == "policy_statement"
    }
    if series:
        situation_ids |= {s.id for s in ensure_policy_situations(session, series)}

    job_ids: list[int] = []
    for situation_id in sorted(situation_ids):
        job_id = queue.enqueue(
            session,
            "assess_situation",
            {"situation_id": situation_id},
            dedupe_key=f"assess_situation:{situation_id}:claims:{document_id}:{newest}",
        )
        if job_id is not None:
            job_ids.append(job_id)
    return job_ids
