"""Channel posts page: drafts of the WhatsApp and X posts for recent material changes (AS-033), and
a record of which ones an operator has posted by hand. Nothing is sent: the app only writes the
text, and "marked as posted" is a note that a person did it (the demand test D1 counts these).

A situation whose post breaks a rule (a number the facts do not state, a headline too long for the
channel) is listed as a problem and does not hide the others. While publication is suspended (the
kill switch) there are no drafts and nothing can be marked."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal import audit, settings_store
from africasignal.models import AssessmentVersion, ChannelPost, Operator, Situation
from africasignal.operations.paging import Page, page_of
from africasignal.publish import versions
from africasignal.publish.whatsapp_text import (
    MAX_CHARS,
    ChannelPosts,
    PostError,
    material_changes,
    write_post,
)
from africasignal.textclean import one_line

CHANNELS = tuple(MAX_CHARS)  # ("wa", "x")
CHANNEL_NAMES = {"wa": "WhatsApp", "x": "X"}
DRAFTS_PER_PAGE = 20  # situations per page: each has a WhatsApp and an X draft
MAX_URL = 300
MAX_NOTE = 200


class ChannelPostError(ValueError):
    """A refusal the operator should see."""


@dataclass
class Draft:
    posts: ChannelPosts
    version_id: int
    posted: dict[str, ChannelPost] = field(default_factory=dict)  # channel -> its record


@dataclass
class Drafts:
    posts: list[Draft] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    suspended: bool = False
    page: Page = field(default_factory=lambda: Page(1, DRAFTS_PER_PAGE, 0))


def drafts(session: Session, since: datetime, page: str | None = None) -> Drafts:
    """One page of drafts, newest first. Raises ``PostError`` when the public address is not
    set (the posts link to it)."""
    result = Drafts()
    if versions.publication_suspended(session):
        result.suspended = True
        return result
    base_url = settings_store.get(session, "public_base_url")
    if not base_url:
        raise PostError("the public address is not set: add it under Settings in the console")
    everything = material_changes(session, since)  # oldest first
    result.page = page_of(page, len(everything), DRAFTS_PER_PAGE)
    newest_first = everything[::-1]
    changes = newest_first[result.page.offset : result.page.offset + result.page.size]
    records: dict[int, dict[str, ChannelPost]] = {}
    for record in session.scalars(
        select(ChannelPost).where(ChannelPost.assessment_version_id.in_([v.id for _, v in changes]))
    ):
        records.setdefault(record.assessment_version_id, {})[record.channel] = record
    for situation, version in changes:
        try:
            posts = ChannelPosts(
                situation.slug,
                version.version,
                version.published_at,
                write_post(version, situation, base_url=base_url, channel="wa"),
                write_post(version, situation, base_url=base_url, channel="x"),
            )
        except PostError as exc:
            result.problems.append(str(exc))
            continue
        result.posts.append(Draft(posts, version.id, records.get(version.id, {})))
    return result


def recent_records(session: Session, limit: int = 20) -> list[tuple[ChannelPost, str, int]]:
    """The newest records with the situation slug and version number they are about."""
    rows = session.execute(
        select(ChannelPost, Situation.slug, AssessmentVersion.version)
        .join(AssessmentVersion, AssessmentVersion.id == ChannelPost.assessment_version_id)
        .join(Situation, Situation.id == AssessmentVersion.situation_id)
        .order_by(ChannelPost.posted_at.desc(), ChannelPost.id.desc())
        .limit(limit)
    )
    return [(post, slug, number) for post, slug, number in rows]


def _clean_url(url: str) -> str | None:
    url = one_line(url)
    if not url:
        return None
    try:
        parts = urlsplit(url)
        ok = parts.scheme == "https" and bool(parts.hostname) and not parts.username
    except ValueError:
        ok = False
    if not ok or len(url) > MAX_URL:
        raise ChannelPostError(
            f"the link to your post must be a plain https address of at most {MAX_URL} characters"
        )
    return url


def mark_posted(
    session: Session,
    operator: Operator,
    version_id: int,
    channel: str,
    *,
    post_url: str = "",
    note: str = "",
) -> ChannelPost:
    """Record that ``operator`` posted this version's draft on ``channel`` by hand."""
    if channel not in CHANNELS:
        raise ChannelPostError("choose WhatsApp or X")
    if versions.publication_suspended(session):
        raise ChannelPostError("publication is suspended, so nothing is posted")
    note = one_line(note)
    if len(note) > MAX_NOTE:
        raise ChannelPostError(f"keep the note under {MAX_NOTE} characters")
    url = _clean_url(post_url)
    version = session.scalars(
        select(AssessmentVersion).where(AssessmentVersion.id == version_id).with_for_update()
    ).first()
    situation = session.get(Situation, version.situation_id) if version else None
    if version is None or situation is None:
        raise ChannelPostError("no such version")
    if version.status != "published" or situation.current_version_id != version.id:
        raise ChannelPostError(
            "that version is no longer the published one, so its draft is out of date"
        )
    existing = session.scalars(
        select(ChannelPost.id).where(
            ChannelPost.assessment_version_id == version_id, ChannelPost.channel == channel
        )
    ).first()
    if existing is not None:
        raise ChannelPostError(f"already marked as posted on {CHANNEL_NAMES[channel]}")
    row = ChannelPost(
        assessment_version_id=version_id,
        channel=channel,
        posted_by_operator_id=operator.id,
        post_url=url,
        note=note or None,
    )
    session.add(row)
    session.flush()
    audit.record(
        session,
        operator,
        "channel_post.mark_posted",
        "channel_post",
        row.id,
        after={
            "situation": situation.slug,
            "version": version.version,
            "channel": channel,
            "post_url": url,
            "note": note or None,
        },
    )
    return row


def unmark(session: Session, operator: Operator, record_id: int) -> None:
    """Take a mistaken record back. The audit row keeps what it said."""
    row = session.scalars(
        select(ChannelPost).where(ChannelPost.id == record_id).with_for_update()
    ).first()
    if row is None:
        raise ChannelPostError("no such record")
    audit.record(
        session,
        operator,
        "channel_post.unmark",
        "channel_post",
        row.id,
        before={
            "assessment_version_id": row.assessment_version_id,
            "channel": row.channel,
            "posted_by_operator_id": row.posted_by_operator_id,
            "posted_at": row.posted_at.isoformat(),
            "post_url": row.post_url,
            "note": row.note,
        },
    )
    session.delete(row)
    session.flush()
