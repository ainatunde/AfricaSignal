"""Follower notifications, correction emails and cancellation (AS-031, plan B9 step 4)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.jobs import queue
from africasignal.jobs.handlers import JobContext
from africasignal.jobs.handlers.notify_followers import notify_followers as handler
from africasignal.models import (
    AssessmentInput,
    EditorialInsightDraft,
    EvidenceDocument,
    Notification,
    Outbox,
    Place,
    Situation,
    Source,
    SourcePermission,
)
from africasignal.publish.accounts import set_insight_email_opt_in
from africasignal.publish.email import FakeProvider
from africasignal.publish.notify import (
    cancel_superseded,
    enqueue_notify_followers,
    mark_all_read,
    notify_followers,
    queue_reviewed_insight_email,
    unread_notifications,
)
from africasignal.publish.outbox import dispatch_pending
from tests.integration.email_support import (
    NOW,
    add_place,
    add_situation,
    add_user,
    add_version,
    follow,
)


@pytest.fixture
def lagos(session: Session) -> Place:
    return add_place(session, "NG-LA", "Lagos", "state")


@pytest.fixture
def situation(session: Session, lagos: Place) -> Situation:
    return add_situation(session, "pms-ng-la", lagos, title="Petrol, Lagos")


def notifications(session: Session) -> list[Notification]:
    return list(session.scalars(select(Notification).order_by(Notification.id)))


def test_one_notification_per_follower_even_if_run_twice(
    session: Session, situation: Situation
) -> None:
    a, b = add_user(session, "a@example.com"), add_user(session, "b@example.com")
    add_user(session, "not-following@example.com")
    follow(session, a, situation)
    follow(session, b, situation)
    version = add_version(session, situation)

    first = notify_followers(session, version.id, NOW)
    second = notify_followers(session, version.id, NOW)

    assert (first.created, second.created) == (2, 0)
    assert sorted(n.user_id for n in notifications(session)) == sorted([a.id, b.id])
    assert {n.kind for n in notifications(session)} == {"new_version"}
    assert notifications(session)[0].dedupe_key == f"{a.id}:{version.id}:new_version"


def test_a_new_version_does_not_email_but_a_correction_does_for_opted_in_followers(
    session: Session, situation: Situation
) -> None:
    opted_in = add_user(session, "yes@example.com", digest=True)
    opted_out = add_user(session, "no@example.com")
    unverified = add_user(session, "new@example.com", verified=False, digest=True)
    for user in (opted_in, opted_out, unverified):
        follow(session, user, situation)
    v1 = add_version(session, situation)
    notify_followers(session, v1.id, NOW)
    assert session.scalars(select(Outbox)).all() == []

    v2 = add_version(
        session,
        situation,
        version=2,
        supersedes=v1,
        change_summary="Corrected: NBS revised the July figure",
    )
    result = notify_followers(session, v2.id, NOW, kind="correction")
    assert (result.created, result.emails) == (3, 1)

    provider = FakeProvider()
    dispatch_pending(session, provider, NOW)
    assert [m.to for m in provider.sent] == ["yes@example.com"]
    assert "Corrected: NBS revised the July figure" in provider.sent[0].text
    assert "List-Unsubscribe" in provider.sent[0].headers


def test_withdrawal_kind_is_inferred_from_the_status(
    session: Session, situation: Situation
) -> None:
    user = add_user(session, "a@example.com", digest=True)
    follow(session, user, situation)
    version = add_version(session, situation, status="withdrawn")
    assert notify_followers(session, version.id, NOW).emails == 1
    assert notifications(session)[0].kind == "withdrawal"


def test_a_superseded_versions_pending_notifications_are_cancelled(
    session: Session, situation: Situation
) -> None:
    read, unread = add_user(session, "read@example.com"), add_user(session, "unread@example.com")
    follow(session, read, situation)
    follow(session, unread, situation)
    v1 = add_version(session, situation)
    notify_followers(session, v1.id, NOW)
    mark_all_read(session, read.id, NOW)

    v2 = add_version(session, situation, version=2, supersedes=v1)
    result = notify_followers(session, v2.id, NOW + timedelta(hours=1))

    assert result.cancelled == 1  # the one nobody read
    by_version = {(n.user_id, n.assessment_version_id): n for n in notifications(session)}
    assert by_version[(unread.id, v1.id)].cancelled_at is not None
    assert by_version[(read.id, v1.id)].cancelled_at is None  # already read, left alone
    assert [n.assessment_version_id for n in unread_notifications(session, unread.id)] == [v2.id]


def test_a_superseded_versions_unsent_email_is_not_sent(
    session: Session, situation: Situation
) -> None:
    user = add_user(session, "a@example.com", digest=True)
    follow(session, user, situation)
    v1 = add_version(session, situation, change_summary="Corrected: first")
    notify_followers(session, v1.id, NOW, kind="correction")
    row = session.scalars(select(Outbox)).one()
    assert row.status == "pending"

    cancel_superseded(session, v1.id, NOW)

    assert (row.status, row.last_error) == ("dead", "superseded")
    provider = FakeProvider()
    assert dispatch_pending(session, provider, NOW).sent == 0 and provider.sent == []


def test_an_email_already_sent_is_not_touched_by_cancellation(
    session: Session, situation: Situation
) -> None:
    user = add_user(session, "a@example.com", digest=True)
    follow(session, user, situation)
    v1 = add_version(session, situation)
    notify_followers(session, v1.id, NOW, kind="correction")
    dispatch_pending(session, FakeProvider(), NOW)
    cancel_superseded(session, v1.id, NOW)
    assert session.scalars(select(Outbox)).one().status == "sent"


@pytest.mark.parametrize("status", ["draft", "withheld", "superseded", "stale"])
def test_versions_that_are_not_public_notify_nobody(
    session: Session, situation: Situation, status: str
) -> None:
    follow(session, add_user(session, "a@example.com"), situation)
    version = add_version(session, situation, status=status)
    assert notify_followers(session, version.id, NOW).skipped is not None
    assert notifications(session) == []


def test_unknown_version_and_kind(session: Session, situation: Situation) -> None:
    assert notify_followers(session, 987654, NOW).skipped is not None
    version = add_version(session, situation)
    with pytest.raises(ValueError):
        notify_followers(session, version.id, NOW, kind="shout")


def test_the_job_is_deduplicated_and_its_handler_creates_notifications(
    session: Session, situation: Situation
) -> None:
    follow(session, add_user(session, "a@example.com"), situation)
    version = add_version(session, situation)
    assert enqueue_notify_followers(session, version.id) is not None
    assert enqueue_notify_followers(session, version.id) is None

    job = queue.claim(session, "test-worker")
    assert job is not None and job.kind == "notify_followers"
    handler(JobContext(session=session, job=job, worker_id="test-worker"))
    assert len(notifications(session)) == 1


def test_mark_all_read(session: Session, situation: Situation) -> None:
    user = add_user(session, "a@example.com")
    follow(session, user, situation)
    notify_followers(session, add_version(session, situation).id, NOW)
    assert len(unread_notifications(session, user.id)) == 1
    assert mark_all_read(session, user.id, NOW) == 1
    assert unread_notifications(session, user.id) == []


def _attach_permissioned_assessment_input(session: Session, version_id: int) -> EvidenceDocument:
    from africasignal.models import AssessmentVersion

    version = session.get(AssessmentVersion, version_id)
    assert version is not None
    source = Source(
        slug=f"test-source-{version_id}",
        name="Test source",
        kind="official_statistics",
        adapter="nbs",
        schedule_minutes=1440,
    )
    session.add(source)
    session.flush()
    session.add(
        SourcePermission(
            source_id=source.id,
            version=1,
            may_collect=True,
            may_store_full_text=False,
            max_quote_chars=200,
            may_republish_numbers=True,
            approved_at=NOW,
        )
    )
    document = EvidenceDocument(
        source_id=source.id,
        url="https://source.example/report",
        canonical_url="https://source.example/report",
        retrieved_at=NOW,
        content_sha256=f"{version_id:064d}",
        storage_key=f"test/{version_id}",
        mime="text/html",
        excerpt="An approved source excerpt.",
    )
    session.add(document)
    session.flush()
    session.add(
        AssessmentInput(
            assessment_version_id=version.id,
            input_kind="evidence_document",
            input_id=document.id,
        )
    )
    session.flush()
    return document


def test_reviewed_insight_email_uses_separate_consent_and_outbox(
    session: Session, situation: Situation
) -> None:
    from tests.integration.email_support import follow as add_follow

    user = add_user(session, "insight@example.com")
    add_follow(session, user, situation)
    version = add_version(session, situation)
    _attach_permissioned_assessment_input(session, version.id)
    draft = EditorialInsightDraft(
        assessment_version_id=version.id,
        status="email_queued",
        model_id="claude-test",
        prompt_version="editorial_draft_v1",
        input_sha256="a" * 64,
        evidence_claim_ids=[],
        content={
            "headline": "Reviewed insight",
            "summary": "A careful summary.",
            "reported_explanations": ["A named source reported the figure."],
        },
    )
    session.add(draft)
    session.flush()
    assert queue_reviewed_insight_email(session, draft, NOW) == 0
    assert session.scalars(select(Outbox)).all() == []

    set_insight_email_opt_in(session, user.id, True, NOW)
    assert user.insight_email_opt_in and not user.digest_opt_in
    assert queue_reviewed_insight_email(session, draft, NOW) == 1
    row = session.scalars(select(Outbox)).one()
    assert row.kind == "email_insight"
    assert row.payload["editorial_draft_id"] == draft.id
    provider = FakeProvider()
    assert dispatch_pending(session, provider, NOW).sent == 1
    assert len(provider.sent) == 1
    assert provider.sent[0].to == user.email
    assert "Reviewed insight" in provider.sent[0].subject
    assert "list=insights" in provider.sent[0].headers["List-Unsubscribe"]
    assert row.status == "sent"


def test_withdrawn_insight_email_consent_cancels_queued_rows(
    session: Session, situation: Situation
) -> None:
    from tests.integration.email_support import follow as add_follow

    user = add_user(session, "insight@example.com")
    add_follow(session, user, situation)
    set_insight_email_opt_in(session, user.id, True, NOW)
    version = add_version(session, situation)
    _attach_permissioned_assessment_input(session, version.id)
    draft = EditorialInsightDraft(
        assessment_version_id=version.id,
        status="email_queued",
        model_id="claude-test",
        prompt_version="editorial_draft_v1",
        input_sha256="b" * 64,
        evidence_claim_ids=[],
        content={"headline": "Reviewed", "summary": "Summary", "reported_explanations": []},
    )
    session.add(draft)
    session.flush()
    assert queue_reviewed_insight_email(session, draft, NOW) == 1
    set_insight_email_opt_in(session, user.id, False, NOW)
    row = session.scalars(select(Outbox)).one()
    assert not user.insight_email_opt_in
    assert (row.status, row.last_error) == ("dead", "insight email consent withdrawn")
    provider = FakeProvider()
    assert dispatch_pending(session, provider, NOW).sent == 0
    assert provider.sent == []
