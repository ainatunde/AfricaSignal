"""Where publication meets delivery.

Publishing or superseding an assessment version calls ``notify_published`` with the version id and
the kind of change. Delivery (notifying followers, AS-031) registers a hook here instead of the
publisher importing it, so the two can be built and merged independently:

    register_publication_hook(lambda session, version_id, kind: enqueue_notify_followers(...))

Hooks run inside the publisher's transaction, so a failed hook rolls the publication back with it.
Until something registers, publishing notifies nobody.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from sqlalchemy.orm import Session

NotificationKind = Literal["new_version", "correction", "withdrawal"]
PublicationHook = Callable[[Session, int, NotificationKind], None]

_hooks: list[PublicationHook] = []


def register_publication_hook(hook: PublicationHook) -> None:
    if hook not in _hooks:
        _hooks.append(hook)


def clear_publication_hooks() -> None:
    """For tests."""
    _hooks.clear()


def notify_published(session: Session, version_id: int, kind: NotificationKind) -> None:
    for hook in list(_hooks):
        hook(session, version_id, kind)
