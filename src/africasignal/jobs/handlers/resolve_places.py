"""``resolve_places``: give each valid claim of a document the place it is about (B10, AS-021).

Payload: ``{"document_id": int}``. Runs after ``extract_claims``. A claim whose place names are
ambiguous or unknown keeps ``place_id`` empty and ``place_precision = unknown``: it is never
given a place the text does not support.
"""

from __future__ import annotations

import logging
from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.jobs.handlers import JobContext, register
from africasignal.jobs.queue import enqueue
from africasignal.models import Claim
from africasignal.places.resolve import resolve_candidates, resolve_place
from africasignal.publish.claim_assessments import request_claim_assessments

log = logging.getLogger("africasignal.resolve_places")


def enqueue_place_resolution(session: Session, document_id: int, version: str) -> int | None:
    return enqueue(
        session,
        "resolve_places",
        {"document_id": document_id},
        dedupe_key=f"resolve_places:{document_id}:{version}",
    )


def dominant_state(session: Session, claims: list[Claim]) -> int | None:
    """The one state most of a document's place names point to, used only to break ties between
    same-named places ("Ikeja" in a document about Lagos). ``None`` when no state leads clearly."""
    states: Counter[int] = Counter()
    for claim in claims:
        for name in claim.place_candidates:
            found = resolve_place(session, str(name))
            if found.status == "resolved" and found.place is not None:
                if found.precision != "national" and found.place.state_id is not None:
                    states[found.place.state_id] += 1
    top = states.most_common(2)
    if not top or (len(top) > 1 and top[0][1] == top[1][1]):
        return None
    return top[0][0]


@register("resolve_places")
def resolve_places_job(ctx: JobContext) -> None:
    session = ctx.session
    document_id = int(ctx.job.payload["document_id"])
    claims = list(
        session.scalars(
            select(Claim)
            .where(
                Claim.evidence_document_id == document_id,
                Claim.valid.is_(True),
                Claim.place_id.is_(None),
            )
            .order_by(Claim.id)
        )
    )
    context_state = dominant_state(session, claims)
    resolved = 0
    for claim in claims:
        if not claim.place_candidates:
            continue
        answer = resolve_candidates(
            session,
            [str(c) for c in claim.place_candidates],
            document_state_id=context_state,
        )
        if answer.status == "resolved" and answer.place is not None:
            claim.place_id = answer.place.id
            claim.place_precision = answer.precision
            resolved += 1
    session.flush()
    request_claim_assessments(session, document_id)  # claims with places can now corroborate
    log.info(
        "document %s: %d of %d claims placed",
        document_id,
        resolved,
        len(claims),
        extra={"job_id": ctx.job.id},
    )
