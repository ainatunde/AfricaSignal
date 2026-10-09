"""The weekly digest (AS-032): content, once per user per ISO week, unsubscribe."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.jobs import queue
from africasignal.jobs.handlers import JobContext
from africasignal.jobs.handlers.weekly_digest import weekly_digest as handler
from africasignal.models import AppUser, Outbox, Place, Setting
from africasignal.publish.accounts import set_digest_opt_in, set_preferences, unsubscribe
from africasignal.publish.digest import build_digest, iso_week, run_weekly_digest
from africasignal.publish.email import FakeProvider
from africasignal.publish.email_render import unsubscribe_url
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
def places(session: Session) -> dict[str, Place]:
    country = add_place(session, "NG", "Nigeria", "country")
    lagos = add_place(session, "NG-LA", "Lagos", "state", country)
    kano = add_place(session, "NG-KN", "Kano", "state", country)
    ikeja = add_place(session, "NG-LA-IKJ", "Ikeja", "lga", lagos)
    return {"NG": country, "lagos": lagos, "kano": kano, "ikeja": ikeja}


def outbox_rows(session: Session) -> list[Outbox]:
    return list(session.scalars(select(Outbox).order_by(Outbox.id)))


def test_iso_week_uses_lagos_time() -> None:
    current_week = iso_week(NOW)
    assert current_week.startswith(f"{NOW.year}-W")
    # A Sunday evening in Lagos is still the prior ISO week.
    assert iso_week(NOW - timedelta(hours=9)) != current_week
    assert iso_week(NOW.replace(hour=0, minute=30)) == current_week


def test_digest_lists_followed_changes_and_top_three_material_changes(
    session: Session, places: dict[str, Place]
) -> None:
    user = add_user(session, "a@example.com", digest=True)
    set_preferences(session, user.id, [places["ikeja"].id], [])  # an LGA brings its state

    followed = add_situation(session, "followed", places["kano"], title="Followed")
    follow(session, user, followed)
    add_version(session, followed, severity="low", published_at=NOW - timedelta(days=2))

    titles = {}
    for slug, severity, age in [
        ("high-new", "high", 1),
        ("high-old", "high", 5),
        ("medium-new", "medium", 0),
        ("medium-old", "medium", 3),
        ("low", "low", 1),  # not material
        ("too-old", "high", 8),  # outside the 7-day window
    ]:
        situation = add_situation(session, slug, places["lagos"], title=slug)
        add_version(session, situation, severity=severity, published_at=NOW - timedelta(days=age))
        titles[slug] = situation
    add_version(session, add_situation(session, "draft", places["lagos"]), status="draft")
    add_version(session, add_situation(session, "elsewhere", places["kano"]), severity="high")
    add_version(session, add_situation(session, "national", places["NG"]), severity="high")

    content = build_digest(session, user.id, NOW)

    assert [i["slug"] for i in content.followed] == ["followed"]
    assert [i["slug"] for i in content.top] == ["high-new", "high-old", "medium-new"]
    assert content.week == iso_week(NOW)


def test_a_followed_situation_is_not_repeated_in_the_top_list(
    session: Session, places: dict[str, Place]
) -> None:
    user = add_user(session, "a@example.com", digest=True)
    set_preferences(session, user.id, [places["lagos"].id], [])
    situation = add_situation(session, "both", places["lagos"])
    add_version(session, situation, severity="high")
    follow(session, user, situation)
    content = build_digest(session, user.id, NOW)
    assert [i["slug"] for i in content.followed] == ["both"] and content.top == []


def test_topics_preference_filters_the_top_list(session: Session, places: dict[str, Place]) -> None:
    user = add_user(session, "a@example.com", digest=True)
    set_preferences(session, user.id, [places["lagos"].id], ["food"])
    add_version(session, add_situation(session, "fuel", places["lagos"], "energy"), severity="high")
    add_version(session, add_situation(session, "rice", places["lagos"], "food"), severity="high")
    assert [i["slug"] for i in build_digest(session, user.id, NOW).top] == ["rice"]


def test_digest_is_queued_once_per_user_per_week_even_if_the_job_runs_twice(
    session: Session, places: dict[str, Place]
) -> None:
    user = add_user(session, "a@example.com", digest=True)
    situation = add_situation(session, "s", places["lagos"])
    follow(session, user, situation)
    add_version(session, situation)

    first = run_weekly_digest(session, NOW)
    second = run_weekly_digest(session, NOW + timedelta(hours=2))

    assert (first.queued, second.queued, second.already_queued) == (1, 0, 1)
    assert [r.dedupe_key for r in outbox_rows(session)] == [f"digest:{user.id}:{iso_week(NOW)}"]

    provider = FakeProvider()
    dispatch_pending(session, provider, NOW)
    run_weekly_digest(session, NOW + timedelta(hours=2))
    dispatch_pending(session, provider, NOW + timedelta(hours=2))
    assert len(provider.sent) == 1

    # next week is a new digest
    next_week = NOW + timedelta(days=7)
    add_version(session, situation, version=2, published_at=next_week)
    assert run_weekly_digest(session, next_week).queued == 1


def test_only_opted_in_verified_users_with_something_to_say_get_a_digest(
    session: Session, places: dict[str, Place]
) -> None:
    situation = add_situation(session, "s", places["lagos"])
    add_version(session, situation)
    wanted = add_user(session, "wanted@example.com", digest=True)
    opted_out = add_user(session, "out@example.com")
    unverified = add_user(session, "new@example.com", verified=False, digest=True)
    nothing_to_say = add_user(session, "quiet@example.com", digest=True)
    for user in (wanted, opted_out, unverified):
        follow(session, user, situation)

    run = run_weekly_digest(session, NOW)

    assert (run.queued, run.empty) == (1, 1)
    assert [r.payload["user_id"] for r in outbox_rows(session)] == [wanted.id]
    assert nothing_to_say.id not in [r.payload["user_id"] for r in outbox_rows(session)]


def test_no_digest_while_publication_is_suspended(
    session: Session, places: dict[str, Place]
) -> None:
    user = add_user(session, "a@example.com", digest=True)
    situation = add_situation(session, "s", places["lagos"])
    follow(session, user, situation)
    add_version(session, situation)
    session.add(Setting(key="publication_suspended", value=True))
    session.flush()

    run = run_weekly_digest(session, NOW)

    assert run.suspended and run.queued == 0 and outbox_rows(session) == []


def test_sent_digest_content_and_headers(session: Session, places: dict[str, Place]) -> None:
    user = add_user(session, "a@example.com", digest=True)
    situation = add_situation(session, "pms-ng-la", places["lagos"], title="Petrol, Lagos")
    follow(session, user, situation)
    add_version(session, situation, headline="Petrol rose 5% in Lagos", change_summary="Up from 4%")
    run_weekly_digest(session, NOW)
    provider = FakeProvider()
    dispatch_pending(session, provider, NOW)

    message = provider.sent[0]
    assert message.subject == f"Your AfricaSignal week: {iso_week(NOW)}"
    assert "Petrol rose 5% in Lagos" in message.text and "Up from 4%" in message.text
    assert "http://localhost:8000/s/pms-ng-la?ref=email" in message.text
    assert message.html is not None and "Petrol rose 5% in Lagos" in message.html
    assert message.headers["List-Unsubscribe"] == f"<{unsubscribe_url(user.id)}>"
    assert message.headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_unsubscribe_link_stops_the_digest(session: Session, places: dict[str, Place]) -> None:
    user = add_user(session, "a@example.com", digest=True)
    situation = add_situation(session, "s", places["lagos"])
    follow(session, user, situation)
    add_version(session, situation)
    run_weekly_digest(session, NOW)  # queued but not yet sent

    token = unsubscribe_url(user.id).split("t=", 1)[1]
    assert not unsubscribe(session, "tampered" + token)
    assert unsubscribe(session, token)
    assert unsubscribe(session, token)  # repeating is harmless
    session.refresh(user)
    assert user.digest_opt_in is False

    provider = FakeProvider()
    dispatch_pending(session, provider, NOW)
    assert provider.sent == []  # the queued digest is dropped, not sent
    assert run_weekly_digest(session, NOW + timedelta(days=7)).queued == 0


def test_opt_in_needs_a_verified_address(session: Session) -> None:
    unverified = add_user(session, "a@example.com", verified=False)
    with pytest.raises(ValueError):
        set_digest_opt_in(session, unverified.id, True, NOW)
    verified = add_user(session, "b@example.com")
    set_digest_opt_in(session, verified.id, True, NOW)
    user = session.get(AppUser, verified.id)
    assert user is not None and user.digest_opt_in and user.digest_opt_in_at == NOW


def test_the_job_handler_queues_digests(session: Session, places: dict[str, Place]) -> None:
    user = add_user(session, "a@example.com", digest=True)
    situation = add_situation(session, "s", places["lagos"])
    follow(session, user, situation)
    add_version(session, situation, published_at=datetime.now(UTC))
    queue.enqueue(session, "weekly_digest", dedupe_key="weekly_digest:test")
    job = queue.claim(session, "w")
    assert job is not None
    handler(JobContext(session=session, job=job, worker_id="w"))  # uses the real clock
    assert [r.payload["user_id"] for r in outbox_rows(session)] == [user.id]
