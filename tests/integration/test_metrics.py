"""The demand-test numbers (AS-034, plan D1) against a hand count, and event retention."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from africasignal import metrics, operators
from africasignal.models import (
    AuditLog,
    Event,
    Feedback,
    Follow,
    LlmCall,
    Operator,
    Place,
    Situation,
)
from tests.integration.email_support import add_place, add_situation, add_user, add_version

LAGOS = ZoneInfo("Africa/Lagos")
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=LAGOS)  # Wednesday of ISO week 2026-W42


def at(month: int, day: int, hour: int = 10) -> datetime:
    return datetime(2026, month, day, hour, 0, tzinfo=LAGOS)


def view(session: Session, anon: str, when: datetime, **kw: object) -> None:
    metrics.record_event(session, "page_view", anon_id=anon, now=when, **kw)  # type: ignore[arg-type]


@pytest.fixture
def situation(session: Session) -> Situation:
    state = add_place(session, "NG-LA", "Lagos", "state")
    situation = add_situation(session, "price-pms", state)
    add_version(session, situation)
    return situation


@pytest.fixture
def operator(session: Session) -> Operator:
    return operators.create_operator(session, "ops@example.org", "correct horse battery", "admin")[
        0
    ]


@pytest.mark.parametrize("channel", ("facebook", "instagram", "telegram", "youtube", "tiktok"))
def test_social_channel_referrals_are_retained(channel: str) -> None:
    assert metrics.clean_ref(channel) == channel


def test_iso_weeks_are_counted_in_lagos_time() -> None:
    # 23:30 UTC on Sunday 11 October is 00:30 Monday 12 October in Lagos: the next week.
    assert metrics.iso_week_label(datetime(2026, 10, 11, 23, 30, tzinfo=UTC)) == "2026-W42"
    assert metrics.iso_week_label(datetime(2026, 10, 11, 22, 30, tzinfo=UTC)) == "2026-W41"
    assert metrics.week_start(NOW).isoformat() == "2026-10-12"


def test_the_report_matches_a_hand_count(
    session: Session, situation: Situation, operator: Operator
) -> None:
    # W40 = 28 Sep to 4 Oct, W41 = 5 to 11 Oct, W42 = 12 to 18 Oct (today is Wed 14 Oct, noon).
    view(session, "visitor-a-aaaaaaaaaaaaaaaa", at(9, 29))  # A: first seen W40
    view(session, "visitor-a-aaaaaaaaaaaaaaaa", at(10, 2))  # ...and back on another day
    view(session, "visitor-b-bbbbbbbbbbbbbbbb", at(9, 30, 9))  # B: first seen W40, never back
    view(session, "visitor-c-cccccccccccccccc", at(10, 6))  # C: first seen W41
    view(session, "visitor-c-cccccccccccccccc", at(10, 6, 15))  # (the same day is not a return)
    view(session, "visitor-c-cccccccccccccccc", at(10, 13))  # C again in W42
    view(session, "visitor-d-dddddddddddddddd", at(10, 7))  # D: first seen W41
    view(
        session, "visitor-e-eeeeeeeeeeeeeeee", at(10, 13), ref="wa"
    )  # E: new in W42, from WhatsApp
    metrics.record_event(
        session, "situation_view", anon_id="visitor-e-eeeeeeeeeeeeeeee", now=at(10, 13)
    )
    metrics.record_event(
        session, "share_click", anon_id="visitor-e-eeeeeeeeeeeeeeee", now=at(10, 13)
    )
    session.flush()

    other = add_situation(session, "price-lpg", session.scalars(select(Place)).one())
    u1, u2, u3 = (add_user(session, f"u{i}@example.com") for i in (1, 2, 3))
    for user, target, when in (
        (u1, situation, at(10, 2)),
        (u2, situation, at(10, 8)),
        (u2, other, at(10, 9)),  # a second follow is not a second user
    ):
        follow = Follow(user_id=user.id, situation_id=target.id)
        session.add(follow)
        session.flush()
        follow.created_at = when
    assert u3.id  # a signed-in user with no follow is not counted
    session.flush()

    version_id = situation.current_version_id
    assert version_id is not None
    for kind, when in (
        ("useful_yes", at(10, 6)),
        ("useful_yes", at(10, 7)),
        ("useful_yes", at(10, 8)),
        ("useful_no", at(10, 9)),
        ("error_report", at(10, 9)),  # not a vote
    ):
        row = Feedback(assessment_version_id=version_id, kind=kind, anon_id="v")
        session.add(row)
        session.flush()
        row.created_at = when
    for cost, when in ((Decimal("2.00000"), at(10, 6)), (Decimal("1.00000"), at(10, 9))):
        session.add(
            LlmCall(
                purpose="extract",
                model_id="m",
                prompt_version="v1",
                input_tokens=1,
                output_tokens=1,
                cost_usd=cost,
                ts=when,
            )
        )
    metrics.record_infra_cost(session, operator, Decimal("52.00"))  # 52 a month: 12 a week
    metrics.record_whatsapp_followers(session, operator, "2026-W41", 400)
    session.flush()

    report = metrics.d1_report(session, NOW, weeks=4)
    w39, w40, w41, w42 = report.weeks
    assert [w.week for w in report.weeks] == ["2026-W39", "2026-W40", "2026-W41", "2026-W42"]

    assert (w39.visitors, w39.page_views) == (0, 0)
    assert (w40.visitors, w40.page_views, w40.new_visitors) == (2, 3, 2)
    assert (w40.return_measured, w40.return_returned, w40.return_rate) == (2, 1, 50.0)
    assert (w41.visitors, w41.page_views, w41.new_visitors) == (2, 3, 2)
    assert (w41.return_measured, w41.return_rate) == (0, None)  # their 14 days are not over
    assert (w42.visitors, w42.page_views, w42.new_visitors) == (2, 2, 1)
    assert w42.situation_views == 1 and w42.share_clicks == 1
    assert w42.visits_by_ref == {"wa": 1}

    assert [w.users_with_follow for w in report.weeks] == [0, 1, 2, 2]
    assert (w41.useful_yes, w41.useful_no, w41.useful_rate) == (3, 1, 75.0)
    assert w42.useful_rate is None and w40.useful_rate is None
    assert (w41.llm_cost_usd, w41.infra_cost_usd) == (Decimal("3"), Decimal("12"))
    assert w41.cost_per_wau == pytest.approx(7.5)  # (3 + 12) / 2 visitors
    assert w39.cost_per_wau is None  # no visitors
    assert [w.wa_followers for w in report.weeks] == [None, None, 400, None]

    summary = {row["key"]: row for row in report.summary()}  # the latest complete week is W41
    assert (
        summary["wa_followers"]["value"] == 400 and summary["wa_followers"]["verdict"] == "between"
    )
    assert summary["return_rate"]["verdict"] == "unknown"
    assert summary["followers_with_follow"]["value"] == 2
    assert summary["followers_with_follow"]["verdict"] == "rethink"
    assert (
        summary["useful_rate"]["value"] == 75.0 and summary["useful_rate"]["verdict"] == "continue"
    )
    assert summary["cost_per_wau"]["verdict"] == "rethink"


def test_a_visitor_who_returns_after_14_days_does_not_count_as_returned(session: Session) -> None:
    view(session, "visitor-a-aaaaaaaaaaaaaaaa", at(9, 14))
    view(session, "visitor-a-aaaaaaaaaaaaaaaa", at(9, 29))  # 15 days later
    view(session, "visitor-b-bbbbbbbbbbbbbbbb", at(9, 14))
    view(session, "visitor-b-bbbbbbbbbbbbbbbb", at(9, 28, 9))  # 13 days and 23 hours later
    session.flush()
    report = metrics.d1_report(session, NOW, weeks=6)
    cohort = next(w for w in report.weeks if w.week == "2026-W38")
    assert (cohort.new_visitors, cohort.return_measured, cohort.return_returned) == (2, 2, 1)


def test_verdicts_follow_the_plan_thresholds() -> None:
    wa = metrics.TARGETS["wa_followers"]
    assert [metrics.verdict(wa, v) for v in (1000, 999, 300, 299, None)] == [
        "continue",
        "between",
        "between",
        "rethink",
        "unknown",
    ]
    cost = metrics.TARGETS["cost_per_wau"]
    assert [metrics.verdict(cost, v) for v in (0.05, 0.051, 0.25, 0.26)] == [
        "continue",
        "between",
        "between",
        "rethink",
    ]


def test_manual_numbers_are_validated_and_audited(session: Session, operator: Operator) -> None:
    for week, count in (("2026-40", 5), ("2026-W54", 5), ("2026-W40", -1)):
        with pytest.raises(ValueError):
            metrics.record_whatsapp_followers(session, operator, week, count)
    metrics.record_whatsapp_followers(session, operator, "2026-W40", 300)
    metrics.record_whatsapp_followers(session, operator, "2026-W40", 350)  # a correction
    metrics.record_whatsapp_followers(session, operator, "2026-W41", 400)
    assert metrics.whatsapp_followers(session) == {"2026-W40": 350, "2026-W41": 400}
    with pytest.raises(ValueError):
        metrics.record_infra_cost(session, operator, Decimal("-1"))
    metrics.record_infra_cost(session, operator, Decimal("24.50"))
    assert metrics.infra_monthly_usd(session) == Decimal("24.50")
    actions = session.scalars(select(AuditLog.action).where(AuditLog.action.like("metrics.%")))
    assert sorted(actions) == ["metrics.infra_cost"] + ["metrics.whatsapp_followers"] * 3


def test_events_are_kept_for_13_months(session: Session) -> None:
    now = datetime(2026, 10, 14, tzinfo=UTC)
    metrics.record_event(
        session, "page_view", anon_id="visitor-a-aaaaaaaaaaaaaaaa", now=now - timedelta(days=30)
    )
    metrics.record_event(
        session, "page_view", anon_id="visitor-a-aaaaaaaaaaaaaaaa", now=now - timedelta(days=380)
    )
    metrics.record_event(
        session, "page_view", anon_id="visitor-a-aaaaaaaaaaaaaaaa", now=now - timedelta(days=420)
    )
    session.flush()
    assert metrics.prune_events(session, now) == 1
    assert session.scalar(select(func.count()).select_from(Event)) == 2


def test_event_inputs_are_checked(session: Session) -> None:
    with pytest.raises(ValueError):
        metrics.record_event(session, "made_up")
    metrics.record_event(session, "page_view", anon_id="../../etc/passwd", ref="wa")
    row = session.scalars(select(Event)).one()
    assert row.anon_id is None and row.ref == "wa"  # a cookie that is not ours is dropped
    assert metrics.looks_like_a_bot(None) and metrics.looks_like_a_bot("Googlebot/2.1")
    assert not metrics.looks_like_a_bot("Mozilla/5.0 (Linux; Android 13) Chrome/120")
    assert session.scalar(select(func.count()).select_from(Place)) == 0
