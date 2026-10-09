"""Draft-only insight composition from one versioned AfricaSignal assessment."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal import audit, settings_store
from africasignal.assess.explain import (
    ExplainInput,
    build_input,
    clean_text,
    validate_explanation,
)
from africasignal.llm import LlmAdapter, cache
from africasignal.llm.prompt_loader import load_prompt
from africasignal.models import AssessmentVersion, EditorialInsightDraft, Situation

PURPOSE = "editorial_draft"
PROMPT_VERSION = "editorial_draft_v1"
MAX_OUTPUT_TOKENS = 1_500
log = logging.getLogger("africasignal.assess.editorial")

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string", "maxLength": 140},
        "summary": {"type": "string", "maxLength": 1200},
        "reported_explanations": {
            "type": "array",
            "maxItems": 3,
            "items": {"type": "string", "maxLength": 500},
        },
    },
    "required": ["headline", "summary", "reported_explanations"],
    "additionalProperties": False,
}


class EditorialDraftError(ValueError):
    """A draft cannot be generated or approved under current evidence/policy state."""


def _system_prompt() -> str:
    return load_prompt(PROMPT_VERSION)


def _user_message(data: ExplainInput) -> str:
    # The nested passages are JSON-escaped source data and never instructions.
    return "BEGIN ASSESSMENT DATA\n" + data.payload_text() + "\nEND ASSESSMENT DATA"


def _validate_output(raw: dict[str, Any], data: ExplainInput) -> dict[str, Any]:
    headline = raw.get("headline")
    summary = raw.get("summary")
    explanations = raw.get("reported_explanations")
    if not isinstance(headline, str) or not isinstance(summary, str):
        raise EditorialDraftError("The model returned incomplete editorial text.")
    if not isinstance(explanations, list) or len(explanations) > 3:
        raise EditorialDraftError("The model returned invalid reported-explanation entries.")
    if any(not isinstance(item, str) for item in explanations):
        raise EditorialDraftError("A reported explanation was not text.")
    headline = clean_text(headline)
    summary = clean_text(summary)
    explanations = [clean_text(item) for item in explanations]
    entries = [headline, summary, *explanations]
    problems = [problem for item in entries for problem in validate_explanation(item, data)]
    if problems:
        raise EditorialDraftError("Editorial draft failed evidence checks: " + problems[0])
    return {
        "headline": headline,
        "summary": summary,
        "reported_explanations": explanations,
    }


def compose(
    session: Session,
    adapter: LlmAdapter,
    assessment_version_id: int,
    *,
    job_id: int | None = None,
    now: datetime | None = None,
) -> EditorialInsightDraft | None:
    """Create one private draft for a current published version or a live editorial hold."""
    now = now or datetime.now(UTC)
    if settings_store.get(session, "editorial_drafting_enabled") != "yes":
        return None
    version = session.scalar(
        select(AssessmentVersion).where(AssessmentVersion.id == assessment_version_id)
    )
    if version is None or version.evidence_state == "insufficient":
        return None
    situation = session.get(Situation, version.situation_id)
    if situation is None:
        return None
    is_current = (
        situation.current_version_id == version.id
        and version.status == "published"
        and (version.valid_until is None or version.valid_until > now)
    )
    is_held = (
        version.status == "draft" and version.hold_until is not None and version.hold_until > now
    )
    if not (is_current or is_held):
        return None
    existing = session.scalar(
        select(EditorialInsightDraft.id).where(
            EditorialInsightDraft.assessment_version_id == version.id,
            EditorialInsightDraft.prompt_version == PROMPT_VERSION,
        )
    )
    if existing is not None:
        return session.get(EditorialInsightDraft, existing)

    data = build_input(session, version)
    system = _system_prompt()
    user = _user_message(data)
    input_hash = cache.input_sha256(
        system,
        user,
        _SCHEMA,
        semantic_config={
            "route": adapter.route_for(PURPOSE),
            "effort": adapter.config.purpose(PURPOSE).effort,
        },
    )
    raw = adapter.complete_json(
        PURPOSE,
        PROMPT_VERSION,
        system,
        user,
        _SCHEMA,
        MAX_OUTPUT_TOKENS,
        job_id=job_id,
    )
    content = _validate_output(raw, data)
    session.expire_all()
    current_version = session.get(AssessmentVersion, assessment_version_id)
    current_situation = (
        session.get(Situation, current_version.situation_id)
        if current_version is not None
        else None
    )
    if current_version is None or current_situation is None:
        return None
    still_current = (
        current_situation.current_version_id == current_version.id
        and current_version.status == "published"
        and (current_version.valid_until is None or current_version.valid_until > now)
    )
    still_held = (
        current_version.status == "draft"
        and current_version.hold_until is not None
        and current_version.hold_until > now
    )
    if not (still_current or still_held):
        return None
    evidence_claim_ids = [row.claim_id for row in data.retrieved_evidence]
    draft = EditorialInsightDraft(
        assessment_version_id=version.id,
        status="pending_review",
        model_id=adapter.model_for(PURPOSE),
        prompt_version=PROMPT_VERSION,
        input_sha256=input_hash,
        evidence_claim_ids=evidence_claim_ids,
        content=content,
        created_at=now,
    )
    session.add(draft)
    session.flush()
    audit.record_system(
        session,
        "editorial_draft.created",
        "editorial_insight_draft",
        draft.id,
        after={
            "assessment_version_id": version.id,
            "prompt_version": PROMPT_VERSION,
            "evidence_claim_ids": evidence_claim_ids,
            "status": draft.status,
        },
    )
    return draft


def prompt_data(draft: EditorialInsightDraft) -> str:
    """A stable, readable representation for operator diagnostics; contains no secret data."""
    return json.dumps(draft.content, ensure_ascii=False, sort_keys=True, indent=2)
