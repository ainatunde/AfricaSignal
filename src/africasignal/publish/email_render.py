"""Builds the three kinds of email (sign-in link, correction, weekly digest) from outbox payloads.

Plain text and HTML come from the same data. Templates live in this module rather than under
``web/templates`` so the email backend does not depend on the public site's layout. Payloads hold
display text only, never an email address: the address is read from the user row at send time.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from jinja2 import Environment, StrictUndefined

from africasignal.config import get_settings
from africasignal.publish import tokens
from africasignal.publish.email import EmailMessage

# Paths served by the web stream (accounts and public pages).
LOGIN_PATH = "/signin/verify"
UNSUBSCRIBE_PATH = "/unsubscribe"
SITUATION_PATH = "/s/"
UNSUBSCRIBE_PURPOSE = "unsubscribe"

_text_env = Environment(  # plain text, not HTML
    autoescape=False, undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True
)
_html_env = Environment(
    autoescape=True, undefined=StrictUndefined, trim_blocks=True, lstrip_blocks=True
)

_LOGIN_TEXT = """\
Sign in to AfricaSignal:

{{ link }}

The link works once and expires in 15 minutes. If you did not ask for it, ignore this email.
"""

_LOGIN_HTML = """\
<p>Sign in to AfricaSignal:</p>
<p><a href="{{ link }}">Sign in</a></p>
<p>The link works once and expires in 15 minutes. If you did not ask for it, ignore this email.</p>
"""

_CORRECTION_TEXT = """\
{{ heading }}

{{ item.headline }}
{{ item.scope_label }}
{% if item.change_summary %}
{{ item.change_summary }}
{% endif %}

See the page and its evidence: {{ item.url }}

Unsubscribe from these emails: {{ unsubscribe_url }}
"""

_CORRECTION_HTML = """\
<h2>{{ heading }}</h2>
<p><strong>{{ item.headline }}</strong><br>{{ item.scope_label }}</p>
{% if item.change_summary %}
<p>{{ item.change_summary }}</p>
{% endif %}
<p><a href="{{ item.url }}">See the page and its evidence</a></p>
<p><a href="{{ unsubscribe_url }}">Unsubscribe from these emails</a></p>
"""

_DIGEST_TEXT = """\
Your AfricaSignal week ({{ week }})
{% if followed %}

Situations you follow
{% for item in followed %}
- {{ item.headline }} ({{ item.scope_label }})
{% if item.change_summary %}
  {{ item.change_summary }}
{% endif %}
  {{ item.url }}
{% endfor %}
{% endif %}
{% if top %}

Biggest changes in your places
{% for item in top %}
- {{ item.headline }} ({{ item.scope_label }})
  {{ item.url }}
{% endfor %}
{% endif %}

Unsubscribe from the weekly digest: {{ unsubscribe_url }}
"""

_DIGEST_HTML = """\
<h1>Your AfricaSignal week ({{ week }})</h1>
{% if followed %}
<h2>Situations you follow</h2>
<ul>
{% for item in followed %}
<li><a href="{{ item.url }}">{{ item.headline }}</a> ({{ item.scope_label }})
{% if item.change_summary %}<br>{{ item.change_summary }}{% endif %}</li>
{% endfor %}
</ul>
{% endif %}
{% if top %}
<h2>Biggest changes in your places</h2>
<ul>
{% for item in top %}
<li><a href="{{ item.url }}">{{ item.headline }}</a> ({{ item.scope_label }})</li>
{% endfor %}
</ul>
{% endif %}
<p><a href="{{ unsubscribe_url }}">Unsubscribe from the weekly digest</a></p>
"""

_TEMPLATES = {
    "login_text": _text_env.from_string(_LOGIN_TEXT),
    "login_html": _html_env.from_string(_LOGIN_HTML),
    "correction_text": _text_env.from_string(_CORRECTION_TEXT),
    "correction_html": _html_env.from_string(_CORRECTION_HTML),
    "digest_text": _text_env.from_string(_DIGEST_TEXT),
    "digest_html": _html_env.from_string(_DIGEST_HTML),
}

CORRECTION_HEADINGS = {
    "correction": "A correction to a situation you follow",
    "withdrawal": "A situation you follow was withdrawn",
    "new_version": "A situation you follow has new information",
}


def base_url() -> str:
    return get_settings().public_base_url.rstrip("/")


def situation_url(slug: str, *, ref: str | None = None) -> str:
    url = f"{base_url()}{SITUATION_PATH}{slug}"
    return f"{url}?{urlencode({'ref': ref})}" if ref else url


def login_url(raw_token: str) -> str:
    return f"{base_url()}{LOGIN_PATH}?{urlencode({'token': raw_token})}"


def unsubscribe_url(user_id: int) -> str:
    token = tokens.sign(UNSUBSCRIBE_PURPOSE, str(user_id))
    return f"{base_url()}{UNSUBSCRIBE_PATH}?{urlencode({'t': token})}"


def _with_url(item: dict[str, Any]) -> dict[str, Any]:
    return {**item, "url": situation_url(item["slug"], ref="email")}


def _unsubscribe_headers(url: str) -> dict[str, str]:
    """RFC 8058 one-click unsubscribe: mail clients POST to the URL without opening a page."""
    return {
        "List-Unsubscribe": f"<{url}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


def render_login(to: str, payload: dict[str, Any], key: str) -> EmailMessage:
    data = {"link": payload["link"]}
    return EmailMessage(
        to=to,
        subject="Your AfricaSignal sign-in link",
        text=_TEMPLATES["login_text"].render(**data),
        html=_TEMPLATES["login_html"].render(**data),
        idempotency_key=key,
    )


def render_correction(to: str, user_id: int, payload: dict[str, Any], key: str) -> EmailMessage:
    unsub = unsubscribe_url(user_id)
    data = {
        "heading": CORRECTION_HEADINGS[payload["notification_kind"]],
        "item": _with_url(payload["item"]),
        "unsubscribe_url": unsub,
    }
    return EmailMessage(
        to=to,
        subject=f"{data['heading']}: {payload['item']['title']}",
        text=_TEMPLATES["correction_text"].render(**data),
        html=_TEMPLATES["correction_html"].render(**data),
        headers=_unsubscribe_headers(unsub),
        idempotency_key=key,
    )


def render_digest(to: str, user_id: int, payload: dict[str, Any], key: str) -> EmailMessage:
    unsub = unsubscribe_url(user_id)
    data = {
        "week": payload["week"],
        "followed": [_with_url(i) for i in payload["followed"]],
        "top": [_with_url(i) for i in payload["top"]],
        "unsubscribe_url": unsub,
    }
    return EmailMessage(
        to=to,
        subject=f"Your AfricaSignal week: {payload['week']}",
        text=_TEMPLATES["digest_text"].render(**data),
        html=_TEMPLATES["digest_html"].render(**data),
        headers=_unsubscribe_headers(unsub),
        idempotency_key=key,
    )
