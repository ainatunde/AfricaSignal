"""Operator copy for the WhatsApp channel and X (AS-033).

For each newly published material change this writes one post of at most 600 characters for
WhatsApp and one of at most 270 for X. A post holds the headline, the scope and period, one key
number, the evidence badge in words and the link with ``?ref=wa`` or ``?ref=x``. The operator
pastes it into the channel by hand; nothing is sent from here.

Every sentence comes from the assessment's own fields, which code computed from the facts. Before a
post is returned, ``check_numbers`` proves that each number in it is one of the facts' numbers, so
a post can never state a figure the page does not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal import settings_store
from africasignal.models import AssessmentVersion, Situation
from africasignal.publish.factfmt import allowed_numbers, format_value, numbers_in

Channel = Literal["wa", "x"]
MAX_CHARS: dict[Channel, int] = {"wa": 600, "x": 270}

# The evidence badge in words (the same labels as the pages). T2 policy situations report what a
# document says rather than a measured figure.
BADGE_WORDS: dict[str, str] = {
    "reported": "Official figure, no independent report yet",
    "corroborated": "Official figure, confirmed by an independent report",
    "disputed": "Disputed: sources disagree",
    "insufficient": "Not enough evidence yet",
}
POLICY_REPORTED_WORDS = "Official statement, no independent report yet"


class PostError(ValueError):
    """A post could not be written within the rules."""


@dataclass(frozen=True)
class Post:
    channel: Channel
    text: str
    situation_slug: str
    version: int
    link: str


def badge_words(version: AssessmentVersion) -> str:
    if version.template == "T2_policy_change" and version.evidence_state == "reported":
        return POLICY_REPORTED_WORDS
    return BADGE_WORDS[version.evidence_state]


def situation_link(base_url: str, slug: str, channel: Channel) -> str:
    return f"{base_url.rstrip('/')}/s/{quote(slug)}?ref={channel}"


def check_numbers(text: str, facts: list[dict[str, Any]], *, link: str) -> list[str]:
    """Numbers in ``text`` (outside the link) that no fact states. Empty when the text is clean."""
    allowed = allowed_numbers(facts)
    return [n for n in numbers_in(text.replace(link, "")) if n not in allowed]


def _key_number_line(facts: list[dict[str, Any]]) -> str | None:
    """The one key number: the latest value, as a fact states it."""
    for fact in facts:
        if fact.get("label") == "Current price" and fact.get("value") is not None:
            line = f"Latest: {format_value(fact['value'], str(fact.get('unit') or ''))}"
            return f"{line} ({fact['period']})" if fact.get("period") else line
    return None


def _compose(parts: list[str | None], link: str) -> str:
    return "\n".join(p for p in [*parts, link] if p)


def write_post(
    version: AssessmentVersion, situation: Situation, *, base_url: str, channel: Channel
) -> Post:
    """One post for a published version. Optional lines (scope, then the key number) are dropped
    until the post fits the channel; the headline, badge and link are never dropped."""
    link = situation_link(base_url, situation.slug, channel)
    facts = list(version.facts)
    prefix = "Correction: " if (version.change_summary or "").startswith("Corrected") else ""
    headline = f"{prefix}{version.headline}"
    scope = f"{version.scope_label} · {version.period_label}"
    badge = f"Evidence: {badge_words(version)}"
    key = _key_number_line(facts) if version.evidence_state != "insufficient" else None

    limit = MAX_CHARS[channel]
    candidates: list[list[str | None]] = [
        [headline, scope, key, badge],
        [headline, scope, badge],
        [headline, badge],
    ]
    for parts in candidates:
        text = _compose(parts, link)
        if len(text) <= limit:
            break
    else:
        raise PostError(
            f"{situation.slug}: the headline and link alone need {len(text)} characters; "
            f"{channel} allows {limit}"
        )
    stray = check_numbers(text, facts, link=link)
    if stray:
        raise PostError(f"{situation.slug}: numbers not in the facts: {', '.join(stray)}")
    return Post(channel, text, situation.slug, version.version, link)


def material_changes(
    session: Session, since: datetime
) -> list[tuple[Situation, AssessmentVersion]]:
    """Published versions since ``since`` that are a material change and still current, oldest
    first. Material means a severity above none; "not enough evidence" cards have none."""
    rows = session.execute(
        select(Situation, AssessmentVersion)
        .join(AssessmentVersion, AssessmentVersion.id == Situation.current_version_id)
        .where(
            AssessmentVersion.status == "published",
            AssessmentVersion.severity != "none",
            AssessmentVersion.evidence_state != "insufficient",
            AssessmentVersion.published_at >= since,
        )
        .order_by(AssessmentVersion.published_at, AssessmentVersion.id)
    )
    return [(situation, version) for situation, version in rows]


@dataclass(frozen=True)
class ChannelPosts:
    situation_slug: str
    version: int
    published_at: datetime | None
    whatsapp: Post
    x: Post


def channel_posts(
    session: Session, since: datetime, *, base_url: str | None = None
) -> list[ChannelPosts]:
    """A WhatsApp post and an X post for every material change published since ``since``.

    The link uses the public address from the operator console, read now; pass ``base_url`` only
    to override it.
    """
    base_url = base_url or settings_store.get(session, "public_base_url")
    if not base_url:
        raise PostError("the public address is not set: add it under Settings in the console")
    return [
        ChannelPosts(
            situation.slug,
            version.version,
            version.published_at,
            write_post(version, situation, base_url=base_url, channel="wa"),
            write_post(version, situation, base_url=base_url, channel="x"),
        )
        for situation, version in material_changes(session, since)
    ]
