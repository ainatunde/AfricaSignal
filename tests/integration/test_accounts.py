"""Follows, account deletion and data export (AS-031)."""

from __future__ import annotations

import json

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from africasignal.models import (
    AppUser,
    Event,
    Feedback,
    Follow,
    Notification,
    Outbox,
    Preference,
    UserSession,
)
from africasignal.publish.accounts import (
    delete_account,
    export_account,
    follow,
    followed_situations,
    set_preferences,
    unfollow,
)
from africasignal.publish.email import FakeProvider
from africasignal.publish.login_tokens import consume_login_token, request_login
from africasignal.publish.notify import notify_followers
from africasignal.publish.outbox import dispatch_pending
from tests.integration.email_support import (
    NOW,
    add_place,
    add_situation,
    add_user,
    add_version,
)

EMAIL = "Ada.Lovelace+news@example.com"


def occurrences(session: Session, needle: str) -> dict[str, int]:
    """Rows, per table, where any text, citext or jsonb column contains ``needle``."""
    columns = session.execute(
        text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' "
            "AND (data_type IN ('text', 'character varying', 'jsonb') OR udt_name = 'citext')"
        )
    ).all()
    found: dict[str, int] = {}
    for table, column in columns:
        n = session.execute(
            text(f'SELECT count(*) FROM "{table}" WHERE "{column}"::text ILIKE :p'),
            {"p": f"%{needle}%"},
        ).scalar_one()
        if n:
            found[f"{table}.{column}"] = n
    return found


def populate(session: Session) -> tuple[AppUser, AppUser]:
    """A user with a row in every table that can hold their data, plus a bystander."""
    lagos = add_place(session, "NG-LA", "Lagos", "state")
    situation = add_situation(session, "pms-ng-la", lagos)
    version = add_version(session, situation)

    request_login(session, EMAIL, NOW)
    provider = FakeProvider()
    dispatch_pending(session, provider, NOW)
    token = next(line for line in provider.sent[0].text.splitlines() if "token=" in line)
    assert consume_login_token(session, token.split("token=")[1], NOW) is not None
    user = session.scalars(select(AppUser).where(AppUser.email == EMAIL)).one()
    user.digest_opt_in = True

    bystander = add_user(session, "bystander@example.com", digest=True)
    for u in (user, bystander):
        follow(session, u.id, situation.id)
        set_preferences(session, u.id, [lagos.id], ["energy"])
    notify_followers(session, version.id, NOW, kind="correction")
    session.add(Event(user_id=user.id, anon_id="anon", name="follow", situation_id=situation.id))
    session.add_all(
        [
            Feedback(
                assessment_version_id=version.id,
                user_id=user.id,
                kind="error_report",
                text=f"Wrong figure, write to {EMAIL.upper()} please",
                contact_email=EMAIL,
            ),
            Feedback(  # signed out, but typed the address as a contact
                assessment_version_id=version.id,
                kind="useful_no",
                anon_id="anon",
                contact_email=EMAIL.lower(),
            ),
            Feedback(assessment_version_id=version.id, user_id=bystander.id, kind="useful_yes"),
        ]
    )
    session.flush()
    return user, bystander


def test_the_email_is_everywhere_before_deletion_and_nowhere_after(session: Session) -> None:
    user, bystander = populate(session)
    before = occurrences(session, "ada.lovelace")
    assert {"app_user.email", "feedback.contact_email", "feedback.text"} <= set(before)

    assert delete_account(session, user.id)

    assert occurrences(session, "ada.lovelace") == {}
    assert session.get(AppUser, user.id) is None
    for model in (Follow, Preference, Notification, UserSession):
        assert (
            session.scalar(select(func.count()).select_from(model).where(model.user_id == user.id))
            == 0
        )
    assert (
        session.scalar(
            select(func.count())
            .select_from(Outbox)
            .where(Outbox.payload["user_id"].as_integer() == user.id)
        )
        == 0
    )

    # feedback is kept, anonymised; the event is kept without the user
    feedback = session.scalars(select(Feedback).order_by(Feedback.id)).all()
    assert len(feedback) == 3
    assert (
        feedback[0].user_id is None
        and feedback[0].text == "Wrong figure, write to [removed] please"
    )
    assert session.scalars(select(Event)).one().user_id is None

    # nothing of the bystander was touched
    assert session.get(AppUser, bystander.id) is not None
    assert (
        session.scalar(
            select(func.count()).select_from(Follow).where(Follow.user_id == bystander.id)
        )
        == 1
    )
    assert (
        session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.user_id == bystander.id)
        )
        == 1
    )


def test_delete_unknown_user(session: Session) -> None:
    assert not delete_account(session, 987654)


def test_follow_and_unfollow(session: Session) -> None:
    lagos = add_place(session, "NG-LA", "Lagos", "state")
    user = add_user(session, "a@example.com")
    first, second = add_situation(session, "a", lagos), add_situation(session, "b", lagos)
    assert follow(session, user.id, first.id)
    assert not follow(session, user.id, first.id)
    assert follow(session, user.id, second.id)
    assert [s.slug for s in followed_situations(session, user.id)] == ["a", "b"]
    assert unfollow(session, user.id, first.id)
    assert not unfollow(session, user.id, first.id)
    assert [s.slug for s in followed_situations(session, user.id)] == ["b"]


def test_set_preferences_replaces(session: Session) -> None:
    user = add_user(session, "a@example.com")
    set_preferences(session, user.id, [1, 2], ["energy"])
    set_preferences(session, user.id, [3], [])
    pref = session.get(Preference, user.id)
    assert pref is not None and (list(pref.place_ids), list(pref.topics)) == ([3], [])


def test_export_holds_the_users_data_as_json(session: Session) -> None:
    user, bystander = populate(session)
    data = export_account(session, user.id)
    assert data is not None
    json.dumps(data)  # serialisable as is
    assert data["email"] == EMAIL.lower()
    assert data["digest_opt_in"] is True
    assert data["follows"][0]["situation"] == "pms-ng-la"
    assert data["preferences"]["topics"] == ["energy"]
    assert [n["kind"] for n in data["notifications"]] == ["correction"]
    assert [f["kind"] for f in data["feedback"]] == ["error_report"]
    assert "bystander" not in json.dumps(data)
    assert export_account(session, 987654) is None
