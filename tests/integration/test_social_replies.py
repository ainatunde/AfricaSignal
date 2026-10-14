"""Account-bound X mention intake and cursor safety on PostgreSQL."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal import settings_store
from africasignal.models import SocialMention, SocialMentionPoll
from africasignal.operations import social_replies
from africasignal.publish.x_api import XMention, XMentionsResult

NOW = datetime(2026, 10, 14, 10, tzinfo=UTC)
ACCOUNT_ID = "2244994945"


def test_mention_poll_retains_ids_only_and_finishes_page_cursor(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = {
        "x_replies_enabled": "yes",
        "x_account_user_id": ACCOUNT_ID,
        "x_user_access_token": "test-user-token",
    }
    monkeypatch.setattr(settings_store, "get", lambda _session, key: settings.get(key))
    monkeypatch.setattr(
        settings_store,
        "get_int",
        lambda _session, key: {"x_mentions_poll_minutes": 5, "x_mentions_daily_read_cap": 100}.get(
            key
        ),
    )
    identity_reads: list[str] = []
    monkeypatch.setattr(
        social_replies,
        "authenticated_user_id",
        lambda token: identity_reads.append(token) or ACCOUNT_ID,
    )
    page_calls: list[tuple[str | None, str | None]] = []
    pages = [
        XMentionsResult(
            mentions=(
                XMention("100", "100", ACCOUNT_ID, NOW),
                XMention("101", "100", "44", NOW),
            ),
            fetched_count=2,
            newest_id="101",
            next_token="next-page",
        ),
        XMentionsResult(
            mentions=(XMention("102", "100", "55", NOW),),
            fetched_count=1,
            newest_id="102",
            next_token=None,
        ),
        XMentionsResult(mentions=(), fetched_count=0, newest_id="102", next_token=None),
    ]

    def get_mentions(
        _token: str,
        _account_id: str,
        _max_results: int,
        since_id: str | None,
        page_token: str | None = None,
    ) -> XMentionsResult:
        page_calls.append((since_id, page_token))
        return pages.pop(0)

    monkeypatch.setattr(social_replies, "get_mentions", get_mentions)

    first = social_replies.poll_mentions(session, NOW)
    assert (first.fetched, first.new_mentions) == (2, 1)  # self-mention suppressed
    assert page_calls == [(None, None)]
    assert len(identity_reads) == 1
    mentions = list(session.scalars(select(SocialMention).order_by(SocialMention.post_id)))
    assert [mention.post_id for mention in mentions] == ["101"]
    assert not hasattr(mentions[0], "text")

    second = social_replies.poll_mentions(session, NOW + timedelta(minutes=6))
    assert (second.fetched, second.new_mentions) == (1, 1)
    assert page_calls[-1] == (None, "next-page")
    assert len(identity_reads) == 1  # token identity is rechecked only when it changes

    third = social_replies.poll_mentions(session, NOW + timedelta(minutes=12))
    assert third.fetched == 0
    assert page_calls[-1] == ("102", None)  # next scan begins after the final page watermark
    polls = list(session.scalars(select(SocialMentionPoll).order_by(SocialMentionPoll.id)))
    assert [poll.status for poll in polls] == ["succeeded", "succeeded", "succeeded"]
    assert polls[0].next_page_token == "next-page"
    assert polls[1].next_page_token is None
    assert polls[1].newest_id == "102"
