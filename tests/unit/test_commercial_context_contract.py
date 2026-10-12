from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from africasignal.operations.commercial_context_contract import ContextDecision, ContextRef


def test_context_ref_hash_is_strict_sha256() -> None:
    assert ContextRef(assessment_version_id=1, content_hash="a" * 64)
    with pytest.raises(ValidationError):
        ContextRef(assessment_version_id=1, content_hash="not-a-hash")
    with pytest.raises(ValidationError):
        ContextRef(assessment_version_id=0, content_hash="a" * 64)


def test_context_decision_is_bounded_extra_forbidden_and_timezone_aware() -> None:
    fields = {
        "topic_tags": ("energy",),
        "canonical_place_id": 1,
        "taxonomy_version": "commercial_taxonomy_v1",
        "classifier_version": "deterministic_rules_v1",
        "suitability": "eligible",
        "reason_codes": ("current_public",),
        "evidence_refs": (5,),
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
    }
    assert ContextDecision.model_validate(fields)
    with pytest.raises(ValidationError):
        ContextDecision.model_validate({**fields, "reader_id": 1})
    with pytest.raises(ValidationError):
        ContextDecision.model_validate({**fields, "evidence_refs": (-1,)})
    with pytest.raises(ValidationError):
        ContextDecision.model_validate({**fields, "expires_at": datetime.now()})
