from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from africasignal.models import Setting
from africasignal.research.policy import ResearchPolicy
from africasignal.research.qualification import (
    RELEASE_GATES,
    Evaluation,
    accepted_promotion,
    accepted_release,
)


def test_disabled_default_and_no_external_authority():
    assert ResearchPolicy().mode == "off"
    with pytest.raises(ValidationError):
        ResearchPolicy(external_send_enabled=True)
    with pytest.raises(ValidationError):
        ResearchPolicy(application_enabled=True)
    with pytest.raises(ValidationError):
        ResearchPolicy(mode="research")


def evaluation(provenance="synthetic"):
    return Evaluation(
        provenance=provenance,
        source_sha="a" * 40,
        case_manifest_sha="b" * 64,
        policy_revision=1,
        source_ids=[1],
        model="recorded-test",
        reviewer="reviewer",
        observations=[
            dict(
                case_id=str(i),
                family=str(i % 12),
                actionable=True,
                unsafe=i < 20,
                detected=True,
                correct=True,
                abstained=i < 20,
            )
            for i in range(60)
        ],
    )


def test_synthetic_success_is_never_release_acceptance():
    score = evaluation().score()
    assert score["regression_passed"] is True
    assert score["release_eligible"] is False
    with pytest.raises(ValidationError):
        Evaluation.model_validate({**evaluation().model_dump(), "case_manifest_sha": "missing"})


def test_unsafe_and_missing_cases_cannot_be_hidden_from_denominator():
    report = evaluation("editorial_recorded")
    report.observations[0].abstained = False
    assert report.score()["release_eligible"] is False
    report = evaluation()
    report.observations[1].case_id = "0"
    assert report.score()["complete"] is False
    report = evaluation()
    report.observations[30].critical_errors = 1
    assert report.score()["regression_passed"] is False


def test_canary_and_promotion_gates_are_separate():
    gates = {name: {"passed": True, "evidence": f"check:{name}"} for name in RELEASE_GATES}
    gates["seven_day_canary"] = {"passed": False, "evidence": "canary:pending"}
    row = Setting(
        key="research.qualification.release-1",
        value={
            "evaluation": evaluation("editorial_recorded").model_dump(mode="json"),
            "gates": gates,
        },
    )
    session = Mock()
    session.get.return_value = row
    assert accepted_release(session, "release-1")
    assert not accepted_promotion(session, "release-1")
    row.value["gates"]["seven_day_canary"] = {"passed": True, "evidence": "canary:week-1"}
    assert accepted_promotion(session, "release-1")
