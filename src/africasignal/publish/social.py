"""Operator-approved X publication queue and conservative single-attempt dispatcher."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from africasignal import audit, settings_store
from africasignal.models import (
    AssessmentVersion,
    ChannelPost,
    Operator,
    Situation,
    SocialPublication,
)
from africasignal.publish import social_creative, social_platforms, versions
from africasignal.publish.whatsapp_text import PostError, write_post
from africasignal.publish.x_api import XPostOutcomeUnknown, XPostRejected, create_post
from africasignal.storage import store_for_session

log = logging.getLogger("africasignal.social_publication")
LAGOS = ZoneInfo("Africa/Lagos")
MAX_BATCH = 3
STALE_SENDING_AFTER = timedelta(minutes=5)


class SocialPublicationError(ValueError):
    """A safe refusal shown to the operator."""


@dataclass
class DispatchResult:
    sent: int = 0
    rejected: int = 0
    unknown: int = 0
    cancelled: int = 0
    held: int = 0


def _daily_usage(
    session: Session,
    day_start: datetime,
    next_day: datetime,
    channel: str = "x",
    *,
    include_queued: bool = True,
) -> int:
    attempted = and_(
        SocialPublication.status.in_(("sending", "sent", "outcome_unknown")),
        SocialPublication.attempt_started_at >= day_start,
        SocialPublication.attempt_started_at < next_day,
    )
    reserved = and_(
        SocialPublication.status == "queued",
        SocialPublication.approved_at >= day_start,
        SocialPublication.approved_at < next_day,
    )
    return (
        session.scalar(
            select(func.count())
            .select_from(SocialPublication)
            .where(
                SocialPublication.channel == channel,
                or_(attempted, reserved) if include_queued else attempted,
            )
        )
        or 0
    )


def queue_current_x_post(
    session: Session, operator: Operator, version_id: int, now: datetime
) -> SocialPublication:
    """Queue the server-generated X draft after a human explicitly approves it."""
    if versions.publication_suspended(session):
        raise SocialPublicationError("publication is suspended")
    if settings_store.get(session, "x_publishing_enabled") != "yes":
        raise SocialPublicationError("X publishing is disabled in Settings")
    version = session.scalar(
        select(AssessmentVersion).where(AssessmentVersion.id == version_id).with_for_update()
    )
    situation = session.get(Situation, version.situation_id) if version else None
    if version is None or situation is None:
        raise SocialPublicationError("no such published version")
    if (
        version.status != "published"
        or situation.current_version_id != version.id
        or (version.valid_until is not None and version.valid_until <= now)
    ):
        raise SocialPublicationError("that version is no longer current and publishable")
    if (
        session.scalar(
            select(ChannelPost.id).where(
                ChannelPost.assessment_version_id == version_id, ChannelPost.channel == "x"
            )
        )
        is not None
    ):
        raise SocialPublicationError("this version is already recorded as manually posted on X")
    existing = session.scalar(
        select(SocialPublication).where(
            SocialPublication.assessment_version_id == version_id,
            SocialPublication.channel == "x",
        )
    )
    if existing is not None:
        raise SocialPublicationError(
            "this version already has an X publication record "
            f"({existing.status}); it will not be retried"
        )

    base_url = settings_store.get(session, "public_base_url")
    if not base_url:
        raise SocialPublicationError("the public address is not configured")
    try:
        draft = write_post(version, situation, base_url=base_url, channel="x")
    except PostError as exc:
        raise SocialPublicationError(str(exc)) from None

    local_day = now.astimezone(LAGOS).date()
    day_start = datetime.combine(local_day, time.min, tzinfo=LAGOS).astimezone(UTC)
    next_day = datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=LAGOS).astimezone(
        UTC
    )
    session.execute(
        select(func.pg_advisory_xact_lock(func.hashtext(f"africasignal.x-posts.{local_day}")))
    )
    count = _daily_usage(session, day_start, next_day)
    cap = settings_store.get_int(session, "x_daily_post_limit") or 5
    if count >= cap:
        raise SocialPublicationError(
            f"the configured limit of {cap} X posts for today has been reached"
        )

    row = SocialPublication(
        assessment_version_id=version.id,
        channel="x",
        requested_by_operator_id=operator.id,
        approved_at=now,
        body=draft.text,
        status="queued",
    )
    session.add(row)
    session.flush()
    audit.record(
        session,
        operator,
        "social_publication.approved",
        "social_publication",
        row.id,
        after={"assessment_version_id": version.id, "channel": "x", "status": "queued"},
    )
    return row


def _platform_credentials() -> dict[str, tuple[str, ...]]:
    return {
        "x": ("x_user_access_token",),
        "facebook": (
            "facebook_page_id",
            "facebook_page_access_token",
            "meta_graph_api_version",
        ),
        "instagram": (
            "instagram_professional_account_id",
            "instagram_access_token",
            "meta_graph_api_version",
        ),
        "telegram": ("telegram_channel_id", "telegram_bot_token"),
        "youtube": (
            "youtube_channel_id",
            "youtube_oauth_client_id",
            "youtube_oauth_client_secret",
            "youtube_refresh_token",
        ),
    }


PUBLISHING_ENABLED_SETTINGS = {
    "x": "x_publishing_enabled",
    "facebook": "facebook_publishing_enabled",
    "instagram": "instagram_publishing_enabled",
    "telegram": "telegram_publishing_enabled",
    "youtube": "youtube_publishing_enabled",
}

DAILY_CAP_SETTINGS = {
    "x": "x_daily_post_limit",
    "facebook": "facebook_daily_post_limit",
    "instagram": "instagram_daily_post_limit",
    "telegram": "telegram_daily_post_limit",
    "youtube": "youtube_daily_post_limit",
}


def queue_current_social_post(
    session: Session,
    operator: Operator,
    version_id: int,
    channel: str,
    now: datetime,
    *,
    video: bytes | None = None,
) -> SocialPublication:
    """Approve one platform post; each platform has its own record and daily cap."""
    credentials = _platform_credentials()
    if channel not in credentials:
        raise SocialPublicationError("unsupported social platform")
    if versions.publication_suspended(session):
        raise SocialPublicationError("publication is suspended")
    if settings_store.get(session, PUBLISHING_ENABLED_SETTINGS[channel]) != "yes":
        raise SocialPublicationError(f"{channel.title()} publishing is disabled in Settings")
    if any(not settings_store.get(session, key) for key in credentials[channel]):
        raise SocialPublicationError(
            "configure the platform credentials in Settings before approval"
        )

    version = session.scalar(
        select(AssessmentVersion).where(AssessmentVersion.id == version_id).with_for_update()
    )
    situation = session.get(Situation, version.situation_id) if version else None
    if (
        version is None
        or situation is None
        or version.status != "published"
        or situation.current_version_id != version.id
        or (version.valid_until is not None and version.valid_until <= now)
    ):
        raise SocialPublicationError("that version is no longer current and publishable")
    manual = session.scalar(
        select(ChannelPost.id).where(
            ChannelPost.assessment_version_id == version_id,
            ChannelPost.channel == channel,
        )
    )
    if manual is not None:
        raise SocialPublicationError(
            "this version is already recorded as manually posted on that channel"
        )
    existing = session.scalar(
        select(SocialPublication.id).where(
            SocialPublication.assessment_version_id == version_id,
            SocialPublication.channel == channel,
        )
    )
    if existing is not None:
        raise SocialPublicationError(
            "this version already has a publication record for that channel"
        )

    base_url = settings_store.get(session, "public_base_url")
    if not base_url:
        raise SocialPublicationError("the public address is not configured")
    try:
        draft = write_post(version, situation, base_url=base_url, channel="x")
    except PostError as exc:
        raise SocialPublicationError(str(exc)) from None
    body = draft.text.replace("?ref=x", f"?ref={channel}")

    media_key = None
    media_data = None
    media_type = None
    if channel == "instagram":
        if not base_url.startswith("https://"):
            raise SocialPublicationError("Instagram media requires a public HTTPS address")
        if store_for_session(session) is None:
            raise SocialPublicationError("configure object storage before Instagram approval")
        media_key = f"social-assets/{uuid.uuid4().hex}.jpg"
        media_data = social_creative.render_instagram_image(version, situation)
        media_type = "image/jpeg"
    elif channel == "youtube":
        if not video or len(video) > 50 * 1024 * 1024 or video[4:8] != b"ftyp":
            raise SocialPublicationError("attach a valid MP4 video under 50 MiB before approval")
        if store_for_session(session) is None:
            raise SocialPublicationError("configure object storage before YouTube approval")
        media_key = f"social-assets/youtube/{uuid.uuid4().hex}.mp4"
        media_data = video
        media_type = "video/mp4"

    local_day = now.astimezone(LAGOS).date()
    day_start = datetime.combine(local_day, time.min, tzinfo=LAGOS).astimezone(UTC)
    next_day = datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=LAGOS).astimezone(
        UTC
    )
    lock_name = f"africasignal.{channel}-posts.{local_day}"
    session.execute(select(func.pg_advisory_xact_lock(func.hashtext(lock_name))))
    count = _daily_usage(session, day_start, next_day, channel)
    cap = settings_store.get_int(session, DAILY_CAP_SETTINGS[channel]) or 1
    if count >= cap:
        raise SocialPublicationError(
            f"the configured limit of {cap} {channel} posts for today has been reached"
        )

    if media_key and media_data and media_type:
        object_store = store_for_session(session)
        if object_store is None:
            raise SocialPublicationError("object storage is no longer configured")
        object_store.put(media_key, media_data, media_type)
    row = SocialPublication(
        assessment_version_id=version.id,
        channel=channel,
        requested_by_operator_id=operator.id,
        approved_at=now,
        body=body,
        status="queued",
        media_storage_key=media_key,
        media_content_type=media_type,
    )
    session.add(row)
    session.flush()
    audit.record(
        session,
        operator,
        "social_publication.approved",
        "social_publication",
        row.id,
        after={"assessment_version_id": version.id, "channel": channel, "status": "queued"},
    )
    return row


def publications_for_versions(
    session: Session, version_ids: list[int]
) -> dict[tuple[int, str], SocialPublication]:
    if not version_ids:
        return {}
    rows = session.scalars(
        select(SocialPublication).where(SocialPublication.assessment_version_id.in_(version_ids))
    )
    return {(row.assessment_version_id, row.channel): row for row in rows}


def retry_rejected_post(
    session: Session, operator: Operator, publication_id: int, now: datetime
) -> SocialPublication:
    """Explicitly requeue a definitively rejected platform attempt after its cause is fixed."""
    if versions.publication_suspended(session):
        raise SocialPublicationError("publication is suspended")
    row = session.scalar(
        select(SocialPublication).where(SocialPublication.id == publication_id).with_for_update()
    )
    if row is None or row.status != "failed":
        raise SocialPublicationError("only a definitively failed post can be requeued")
    credentials = _platform_credentials()
    if row.channel not in credentials or any(
        not settings_store.get(session, key) for key in credentials.get(row.channel, ())
    ):
        raise SocialPublicationError("configure the platform credentials before retrying")

    version = session.scalar(
        select(AssessmentVersion)
        .where(AssessmentVersion.id == row.assessment_version_id)
        .with_for_update()
    )
    situation = session.get(Situation, version.situation_id) if version else None
    if (
        version is None
        or situation is None
        or version.status != "published"
        or situation.current_version_id != version.id
        or (version.valid_until is not None and version.valid_until <= now)
    ):
        raise SocialPublicationError("that version is no longer current and publishable")

    local_day = now.astimezone(LAGOS).date()
    day_start = datetime.combine(local_day, time.min, tzinfo=LAGOS).astimezone(UTC)
    next_day = datetime.combine(local_day + timedelta(days=1), time.min, tzinfo=LAGOS).astimezone(
        UTC
    )
    lock_name = f"africasignal.{row.channel}-posts.{local_day}"
    session.execute(select(func.pg_advisory_xact_lock(func.hashtext(lock_name))))
    if settings_store.get(session, PUBLISHING_ENABLED_SETTINGS[row.channel]) != "yes":
        raise SocialPublicationError(f"{row.channel.title()} publishing is disabled in Settings")
    cap = settings_store.get_int(session, DAILY_CAP_SETTINGS[row.channel]) or 1
    if _daily_usage(session, day_start, next_day, row.channel) >= cap:
        raise SocialPublicationError(
            f"the configured limit of {cap} {row.channel} posts for today has been reached"
        )
    row.status = "queued"
    row.approved_at = now
    row.attempt_started_at = None
    row.last_error = None
    audit.record(
        session,
        operator,
        "social_publication.retry_approved",
        "social_publication",
        row.id,
        after={
            "assessment_version_id": row.assessment_version_id,
            "channel": row.channel,
            "status": "queued",
        },
    )
    return row


def recent_publications(session: Session, limit: int = 30) -> list[SocialPublication]:
    return list(
        session.scalars(
            select(SocialPublication)
            .order_by(SocialPublication.created_at.desc(), SocialPublication.id.desc())
            .limit(limit)
        )
    )


MEDIA_PURGE_BATCH = 20


def _delete_media(session: Session, row: SocialPublication) -> None:
    if not row.media_storage_key:
        return
    store = store_for_session(session)
    if store is None:
        return
    try:
        store.delete(row.media_storage_key)
    except Exception:
        log.exception("Could not delete media for social publication %s", row.id)
        return
    row.media_storage_key = None
    row.media_content_type = None


def _purge_old_media(session: Session, now: datetime) -> None:
    """Delete queued, rejected, or ambiguous media after 30 days; sent media is removed earlier."""
    store = store_for_session(session)
    if store is None:
        return
    retention_days = settings_store.get_int(session, "social_media_retention_days") or 30
    rows = list(
        session.scalars(
            select(SocialPublication)
            .where(
                SocialPublication.media_storage_key.is_not(None),
                SocialPublication.created_at < now - timedelta(days=retention_days),
            )
            .order_by(SocialPublication.created_at)
            .limit(MEDIA_PURGE_BATCH)
            .with_for_update(skip_locked=True)
        )
    )
    for row in rows:
        if row.status == "queued":
            row.status = "cancelled"
            row.last_error = "The approved post expired before dispatch; approve a current version."
        _delete_media(session, row)
    if rows:
        session.commit()


def _finish(
    session: Session,
    row_id: int,
    status: str,
    *,
    post_id: str | None = None,
    error: str | None = None,
    now: datetime,
) -> None:
    row = session.scalar(
        select(SocialPublication).where(SocialPublication.id == row_id).with_for_update()
    )
    if row is None or row.status != "sending":
        session.rollback()
        return
    row.status = status
    row.external_post_id = post_id
    row.sent_at = now if status == "sent" else None
    row.last_error = error[:300] if error else None
    if status == "sent":
        _delete_media(session, row)
    session.commit()


def dispatch_pending(session: Session, now: datetime, limit: int = MAX_BATCH) -> DispatchResult:
    """Send each approved platform item once; uncertain outcomes require reconciliation."""
    result = DispatchResult()
    _purge_old_media(session, now)
    stale = list(
        session.scalars(
            select(SocialPublication)
            .where(
                SocialPublication.status == "sending",
                SocialPublication.attempt_started_at < now - STALE_SENDING_AFTER,
            )
            .order_by(SocialPublication.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    for row in stale:
        row.status = "outcome_unknown"
        row.last_error = (
            "The sender stopped during the request; confirm the account before any retry."
        )
        result.unknown += 1
    if stale:
        session.commit()

    for _ in range(limit):
        if versions.publication_suspended(session):
            result.held += (
                session.scalar(
                    select(func.count())
                    .select_from(SocialPublication)
                    .where(SocialPublication.status == "queued")
                )
                or 0
            )
            break

        candidates = list(
            session.scalars(
                select(SocialPublication)
                .where(SocialPublication.status == "queued")
                .order_by(SocialPublication.created_at, SocialPublication.id)
                .limit(50)
                .with_for_update(skip_locked=True)
            )
        )
        if not candidates:
            break

        local_day = now.astimezone(LAGOS).date()
        day_start = datetime.combine(local_day, time.min, tzinfo=LAGOS).astimezone(UTC)
        next_day = datetime.combine(
            local_day + timedelta(days=1), time.min, tzinfo=LAGOS
        ).astimezone(UTC)
        candidate = None
        unsupported = False
        for queued in candidates:
            cap_key = DAILY_CAP_SETTINGS.get(queued.channel)
            if cap_key is None:
                queued.status = "failed"
                queued.last_error = "Unsupported social platform."
                session.commit()
                result.rejected += 1
                unsupported = True
                break
            if settings_store.get(session, PUBLISHING_ENABLED_SETTINGS[queued.channel]) != "yes":
                continue
            lock_name = f"africasignal.{queued.channel}-posts.{local_day}"
            session.execute(select(func.pg_advisory_xact_lock(func.hashtext(lock_name))))
            cap = settings_store.get_int(session, cap_key) or 1
            used = _daily_usage(
                session,
                day_start,
                next_day,
                queued.channel,
                include_queued=False,
            )
            if used < cap:
                candidate = queued
                break
        if candidate is None:
            if unsupported:
                continue
            result.held += len(candidates)
            session.rollback()
            break

        row = candidate
        version = session.scalar(
            select(AssessmentVersion).where(AssessmentVersion.id == row.assessment_version_id)
        )
        situation = session.get(Situation, version.situation_id) if version else None
        if (
            version is None
            or situation is None
            or version.status != "published"
            or situation.current_version_id != version.id
            or (version.valid_until is not None and version.valid_until <= now)
        ):
            row.status = "cancelled"
            row.last_error = "The approved version is no longer current and publishable."
            _delete_media(session, row)
            result.cancelled += 1
            session.commit()
            continue

        row.status = "sending"
        row.attempt_started_at = now
        row.last_error = None
        row_id, channel, body, media_key = row.id, row.channel, row.body, row.media_storage_key
        session.commit()  # establish the no-retry fence before the remote side effect
        store = store_for_session(session)
        try:
            if channel == "x":
                token = settings_store.get(session, "x_user_access_token")
                if not token:
                    raise social_platforms.SocialRejected("X user access token is not configured")
                external_id = create_post(token, body).post_id
            elif channel == "facebook":
                external_id = social_platforms.create_facebook_post(
                    settings_store.get(session, "facebook_page_id") or "",
                    settings_store.get(session, "facebook_page_access_token") or "",
                    settings_store.get(session, "meta_graph_api_version") or "v26.0",
                    body,
                ).external_id
            elif channel == "instagram":
                public_url = settings_store.get(session, "public_base_url") or ""
                if not media_key or not public_url:
                    raise social_platforms.SocialRejected("Instagram media is not configured")
                external_id = social_platforms.create_instagram_post(
                    settings_store.get(session, "instagram_professional_account_id") or "",
                    settings_store.get(session, "instagram_access_token") or "",
                    settings_store.get(session, "meta_graph_api_version") or "v26.0",
                    body,
                    social_creative.signed_asset_url(public_url, media_key, now),
                ).external_id
            elif channel == "telegram":
                external_id = social_platforms.create_telegram_post(
                    settings_store.get(session, "telegram_bot_token") or "",
                    settings_store.get(session, "telegram_channel_id") or "",
                    body,
                ).external_id
            else:
                if not media_key or store is None:
                    raise social_platforms.SocialRejected(
                        "YouTube video is unavailable in object storage"
                    )
                video = store.get(media_key)
                external_id = social_platforms.upload_youtube_video(
                    client_id=settings_store.get(session, "youtube_oauth_client_id") or "",
                    client_secret=settings_store.get(session, "youtube_oauth_client_secret") or "",
                    refresh_token=settings_store.get(session, "youtube_refresh_token") or "",
                    expected_channel_id=settings_store.get(session, "youtube_channel_id") or "",
                    title=version.headline,
                    description=body,
                    category_id=settings_store.get(session, "youtube_category_id") or "25",
                    privacy_status=(
                        settings_store.get(session, "youtube_video_privacy") or "unlisted"
                    ),
                    video=video,
                ).external_id
        except (XPostRejected, social_platforms.SocialRejected) as exc:
            _finish(session, row_id, "failed", error=str(exc), now=now)
            result.rejected += 1
        except (XPostOutcomeUnknown, social_platforms.SocialOutcomeUnknown) as exc:
            _finish(session, row_id, "outcome_unknown", error=str(exc), now=now)
            result.unknown += 1
        except Exception as exc:
            log.exception("Social publication %s failed without a definitive outcome", row_id)
            _finish(
                session,
                row_id,
                "outcome_unknown",
                error=(
                    f"Unexpected {type(exc).__name__}; confirm the {channel} account "
                    "before retrying."
                ),
                now=now,
            )
            result.unknown += 1
        else:
            _finish(session, row_id, "sent", post_id=external_id, now=now)
            result.sent += 1
    return result
