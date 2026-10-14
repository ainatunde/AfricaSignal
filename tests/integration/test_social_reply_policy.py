"""PostgreSQL state transitions for the inactive, evidence-gated X reply policy."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal import settings_store
from africasignal.models import Operator, SocialReplyPolicy
from africasignal.operations import social_replies, social_reply_policy

NOW = datetime(2026, 10, 14, 10, tzinfo=UTC)


def _operator(session: Session, name: str) -> Operator:
    row = Operator(
        email=f"{name}@example.test",
        password_hash="test-only",
        totp_secret_enc="test-only",
        role="admin",
    )
    session.add(row)
    session.flush()
    return row


def _coverage() -> dict[str, dict[str, int]]:
    return {
        family: {"cases": 1, "passed": 1}
        for family in social_reply_policy.REQUIRED_EVALUATION_CASE_FAMILIES
    }


def test_seeded_policy_stays_draft_until_evaluation_editorial_and_staging_gates(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy = session.scalar(select(SocialReplyPolicy).where(SocialReplyPolicy.revision == 1))
    assert policy is not None
    assert policy.status == "draft"
    assert social_replies.active_reply_policy(session, "2244994945") is None

    evaluator = _operator(session, "policy-evaluator")
    editor = _operator(session, "policy-editor")
    staging_operator = _operator(session, "policy-staging")
    with pytest.raises(ValueError, match="different editor"):
        social_reply_policy.approve(
            session, evaluator, 1, "Evaluator cannot approve own policy results", now=NOW
        )
    failed = social_reply_policy.record_evaluation(
        session,
        evaluator,
        1,
        _coverage(),
        1,
        "reports/x-replies-v1.json",
        now=NOW,
    )
    assert failed.status == "draft"
    assert failed.evaluation_critical_errors == 1

    social_reply_policy.record_evaluation(
        session,
        evaluator,
        1,
        _coverage(),
        0,
        "reports/x-replies-v1.json",
        now=NOW,
    )
    with pytest.raises(ValueError, match="different editor"):
        social_reply_policy.approve(
            session, evaluator, 1, "Independent editorial approval required", now=NOW
        )
    social_reply_policy.approve(
        session, editor, 1, "Conversation cases reviewed by editorial", now=NOW
    )
    settings = {
        "x_account_user_id": "2244994945",
        "x_user_access_token": "stage-token",
        "x_replies_enabled": "yes",
        "x_publishing_enabled": "yes",
    }
    monkeypatch.setattr(settings_store, "get", lambda _session, key: settings.get(key))
    monkeypatch.setattr(
        social_reply_policy, "get_settings", lambda: SimpleNamespace(env="development")
    )
    monkeypatch.setattr(social_reply_policy, "publication_suspended", lambda _session: False)
    with pytest.raises(ValueError, match="qualified staging"):
        social_reply_policy.activate(
            session,
            staging_operator,
            1,
            "reports/staging-x-check.json",
            "Provider and staging drills passed",
            now=NOW,
        )

    monkeypatch.setattr(social_reply_policy, "get_settings", lambda: SimpleNamespace(env="staging"))
    social_reply_policy.activate(
        session,
        staging_operator,
        1,
        "reports/staging-x-check.json",
        "Provider and staging drills passed",
        now=NOW,
    )
    active = social_replies.active_reply_policy(session, "2244994945")
    assert active is not None
    assert active.status == "active"
    assert active.revision == 1
    retired = social_reply_policy.retire(
        session, staging_operator, 1, "Staging policy exercise complete", now=NOW
    )
    assert retired.status == "retired"
    assert social_replies.active_reply_policy(session, "2244994945") is None
    next_draft = social_reply_policy.revise(
        session,
        staging_operator,
        1,
        "Revise after the recorded staging exercise",
        social_reply_policy.DEFAULT_POLICY,
        now=NOW,
    )
    assert next_draft.status == "draft"
    assert next_draft.revision == 2
