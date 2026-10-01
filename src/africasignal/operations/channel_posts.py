"""Channel posts page: drafts of the WhatsApp and X posts for recent material changes (AS-033).
Nothing is sent. A situation whose post breaks a rule (a number the facts do not state, a headline
too long for the channel) is listed as a problem and does not hide the others."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from africasignal import settings_store
from africasignal.publish.whatsapp_text import ChannelPosts, PostError, material_changes, write_post


@dataclass
class Drafts:
    posts: list[ChannelPosts] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def drafts(session: Session, since: datetime) -> Drafts:
    """Newest first. Raises ``PostError`` when the public address is not set (the posts link to
    it)."""
    base_url = settings_store.get(session, "public_base_url")
    if not base_url:
        raise PostError("the public address is not set: add it under Settings in the console")
    result = Drafts()
    for situation, version in reversed(material_changes(session, since)):
        try:
            result.posts.append(
                ChannelPosts(
                    situation.slug,
                    version.version,
                    version.published_at,
                    write_post(version, situation, base_url=base_url, channel="wa"),
                    write_post(version, situation, base_url=base_url, channel="x"),
                )
            )
        except PostError as exc:
            result.problems.append(str(exc))
    return result
