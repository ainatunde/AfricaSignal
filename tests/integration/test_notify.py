"""Follower notifications, correction emails and cancellation (AS-031, plan B9 step 4)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.jobs import queue
from africasignal.jobs.handlers import JobContext
from africasignal.jobs.handlers.notify_followers import notify_followers as handler
from africasignal.models import Notification, Outbox, Place, Situation
from africasignal.publish.email import FakeProvider
from africasignal.publish.notify import (
    cancel_superseded,
    enqueue_notify_followers,
    mark_all_read,
    notify_followers,
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
