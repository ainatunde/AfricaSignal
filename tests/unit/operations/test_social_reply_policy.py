from __future__ import annotations

from copy import deepcopy

from africasignal.operations.social_reply_policy import (
    DEFAULT_POLICY,
    REQUIRED_EVALUATION_CASE_FAMILIES,
    evaluation_is_complete,
    policy_allows,
    validate_policy,
)


def _coverage(*, passed: bool = True) -> dict[str, dict[str, int]]:
    return {case: {"cases": 1, "passed": int(passed)} for case in REQUIRED_EVALUATION_CASE_FAMILIES}


def test_default_reply_policy_is_valid_and_bounded() -> None:
    assert validate_policy(DEFAULT_POLICY)
    assert DEFAULT_POLICY["max_replies_per_lagos_day"] == 10
    assert DEFAULT_POLICY["max_replies_per_conversation"] == 1
    assert DEFAULT_POLICY["stores_mention_text"] is False


def test_policy_allows_only_eligible_triaged_requests_within_caps() -> None:
    args = {
        "policy": DEFAULT_POLICY,
        "category": "source_request",
        "topic": "food",
        "country_code": "NG",
        "conversation_id_present": True,
        "replies_today": 9,
        "replies_in_conversation": 0,
    }
    assert policy_allows(**args)
    assert not policy_allows(**(args | {"category": "correction_report"}))
    assert not policy_allows(**(args | {"topic": "transport"}))
    assert not policy_allows(**(args | {"country_code": "GH"}))
    assert not policy_allows(**(args | {"conversation_id_present": False}))
    assert not policy_allows(**(args | {"replies_today": 10}))
    assert not policy_allows(**(args | {"replies_in_conversation": 1}))


def test_policy_validation_fails_closed_on_looser_or_malformed_bounds() -> None:
    expanded = deepcopy(DEFAULT_POLICY)
    expanded["max_replies_per_lagos_day"] = 11
    assert not validate_policy(expanded)

    no_thread_limit = deepcopy(DEFAULT_POLICY)
    no_thread_limit["max_replies_per_conversation"] = True
    assert not validate_policy(no_thread_limit)

    malformed = deepcopy(DEFAULT_POLICY)
    malformed["allowed_topics"] = [{}]
    assert not validate_policy(malformed)


def test_evaluation_requires_each_case_family_to_pass_without_critical_errors() -> None:
    coverage = _coverage()
    assert evaluation_is_complete(len(REQUIRED_EVALUATION_CASE_FAMILIES), coverage, 0)
    assert not evaluation_is_complete(len(REQUIRED_EVALUATION_CASE_FAMILIES), coverage, 1)
    failed = _coverage(passed=False)
    assert not evaluation_is_complete(len(REQUIRED_EVALUATION_CASE_FAMILIES), failed, 0)
    missing = dict(coverage)
    missing.pop(next(iter(REQUIRED_EVALUATION_CASE_FAMILIES)))
    assert not evaluation_is_complete(len(REQUIRED_EVALUATION_CASE_FAMILIES) - 1, missing, 0)
