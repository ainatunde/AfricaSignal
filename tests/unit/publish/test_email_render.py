from __future__ import annotations

import pytest

from africasignal.publish import tokens
from africasignal.publish.accounts import unsubscribe_token
from africasignal.publish.email_render import (
    render_correction,
    render_digest,
    render_login,
    unsubscribe_url,
)

ITEM = {
    "slug": "pms-ng-la",
    "title": "Petrol, Lagos",
    "headline": "Petrol rose 5% & more",
    "scope_label": "Lagos State (state average, NBS)",
    "change_summary": "Corrected: NBS revised the July figure",
}


def test_tokens_are_purpose_bound_and_tamper_proof() -> None:
    token = tokens.sign("unsubscribe", "42")
    assert tokens.verify("unsubscribe", token) == "42"
    assert tokens.verify("login", token) is None
    assert tokens.verify("unsubscribe", token.replace("42", "43", 1)) is None
    assert tokens.verify("unsubscribe", "garbage") is None
    assert tokens.verify("unsubscribe", "") is None


def test_login_email_has_the_link_in_both_parts() -> None:
    message = render_login("a@b.co", {"link": "https://x/signin/verify?token=abc&y=1"}, "login:1")
    assert "https://x/signin/verify?token=abc&y=1" in message.text
    assert message.html is not None and "token=abc&amp;y=1" in message.html  # escaped in HTML
    assert "List-Unsubscribe" not in message.headers


def test_digest_has_one_click_unsubscribe_and_both_parts() -> None:
    payload = {"user_id": 7, "week": "2026-W40", "followed": [ITEM], "top": [ITEM]}
    message = render_digest("a@b.co", 7, payload, "digest:7:2026-W40")
    link = unsubscribe_url(7)
    assert message.headers["List-Unsubscribe"] == f"<{link}>"
    assert message.headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert link in message.text and message.html is not None and "Unsubscribe" in message.html
    assert "Petrol rose 5% & more" in message.text  # plain text is not HTML-escaped
    assert "Petrol rose 5% &amp; more" in message.html
    assert "?ref=email" in message.text
    token = link.split("t=", 1)[1]
    assert token.startswith(unsubscribe_token(7).split(".")[0])


@pytest.mark.parametrize("kind", ["correction", "withdrawal", "new_version"])
def test_correction_email_per_kind(kind: str) -> None:
    payload = {"user_id": 3, "notification_kind": kind, "item": ITEM}
    message = render_correction("a@b.co", 3, payload, "k")
    assert ITEM["title"] in message.subject
    assert ITEM["change_summary"] in message.text
