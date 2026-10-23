"""Console pages for operations (spec B11.5): Jobs, Assessments with the R7 hold queue, range
checks, Costs, NBS upload, discovered domains, Channel posts and backup alerts.

All of them are admin-only. Every change goes through an audited service. WhatsApp remains a
copy-and-post workflow; X, Facebook, Instagram, Telegram, and YouTube require explicit operator
approval and are delivered by a worker. The NBS upload only stores a file and queues the import job.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import Response
from pydantic import ValidationError
from starlette.datastructures import UploadFile

from africasignal import audit, backup_alerts, settings_store
from africasignal.jobs import queue
from africasignal.operations import alerts as alerts_ops
from africasignal.operations import assessments as assessment_ops
from africasignal.operations import channel_posts as channel_ops
from africasignal.operations import costs as cost_ops
from africasignal.operations import delivery_corrections as delivery_correction_ops
from africasignal.operations import domains as domain_ops
from africasignal.operations import editorial as editorial_ops
from africasignal.operations import insight_candidates as insight_candidate_ops
from africasignal.operations import jobs as job_ops
from africasignal.operations import nbs_upload, range_review
from africasignal.operations import policies as policy_ops
from africasignal.operations import social_listening as listening_ops
from africasignal.operations import social_replies as social_reply_ops
from africasignal.operations import social_reply_policy as reply_policy_ops
from africasignal.publish import social as social_ops
from africasignal.publish import versions
from africasignal.publish.whatsapp_text import PostError
from africasignal.storage import store_for_session
from africasignal.web.deps import AdminOperator, DbSession
from africasignal.web.routes.admin import _page, _redirect, _settings_group

router = APIRouter(prefix="/admin")

NOTICES = {
    "job_retried": "Job put back in the queue.",
    "withheld": "Version withheld. It will not be published.",
    "released": "Version published.",
    "withdrawn": "Situation withdrawn. Readers now see the withdrawal.",
    "range_approved": "Value approved and stored. Assessments that use it are queued.",
    "range_rejected": "Value rejected. Assessments held back by it are queued again.",
    "upload_queued": "File stored and import queued. Watch the Jobs page for the result.",
    "domain_rejected": "Domain rejected. It no longer appears in the report.",
    "domain_added": "Source created, inactive, with no permission yet. Review it below.",
    "policy_added": "Policy series added. Its situations are created and appear once a primary "
    "document for it has been read.",
    "policy_added_no_places": "Policy series added, but no situation could be created yet because "
    "the places it covers are not loaded. They are created when the places list is loaded and "
    "the next claim for the series arrives.",
    "post_marked": "Recorded as posted. Nothing was sent by the app.",
    "x_post_queued": "X post approved and queued. Check its delivery status below.",
    "publication_queued": "Social post approved and queued. Check its delivery status below.",
    "social_query_created": "Watch query saved. It will run only while X listening is enabled.",
    "social_query_updated": "Watch query updated.",
    "social_poll_queued": ("A bounded X search was queued. Results are stored as post IDs only."),
    "social_settings_saved": "Social controls saved. The listener uses them on its next run.",
    "social_settings_unchanged": "Social controls are unchanged.",
    "social_lead_updated": "Lead status updated. This does not make a post verified evidence.",
    "social_mention_poll_queued": (
        "A bounded account mentions read was queued. Only post IDs and timestamps are retained."
    ),
    "social_reply_draft_created": (
        "A private evidence-grounded reply draft was created for editorial review."
    ),
    "social_reply_triaged": "Mention triage recorded.",
    "social_reply_reviewed": "Reply review recorded. A queued reply will be rechecked before X.",
    "social_reply_policy_evaluated": "Reply-policy evaluation recorded.",
    "social_reply_policy_evaluation_failed": (
        "Reply-policy evaluation did not meet its gates; policy remains disabled."
    ),
    "social_reply_policy_approved": "Editorial reply-policy approval recorded.",
    "social_reply_policy_activated": "Reply policy activated in staging.",
    "social_reply_policy_retired": "Reply policy retired.",
    "social_reply_policy_revised": "New reply-policy draft created.",
    "delivery_correction_reviewed": "Delivery correction work review recorded.",
    "insight_reviewed": "Editorial review recorded. No reader email or social post was sent.",
    "insight_email_queued": (
        "Reviewed insight email queued for opted-in followers. Check delivery status in Jobs."
    ),
    "post_unmarked": "Record removed.",
    "policy_retired": "Policy series retired. Published situations stay until you withdraw them.",
    "policy_activated": "Policy series active again.",
}


def _notice(request: Request) -> str | None:
    return NOTICES.get(request.query_params.get("notice", ""))


def _back(path: str, notice: str, auth: AdminOperator) -> Response:
    return _redirect(f"{path}?notice={notice}", auth)


# --- jobs ---------------------------------------------------------------------------------------


def _jobs_page(
    request: Request,
    db: DbSession,
    auth: AdminOperator,
    status: int = 200,
    error: str | None = None,
) -> Response:
    return _page(
        request,
        "admin/jobs.html",
        auth,
        status,
        totals=job_ops.totals(db),
        kinds=job_ops.counts_by_kind(db),
        problems=[(j, job_ops.short_error(j)) for j in job_ops.dead_jobs(db)],
        statuses=job_ops.STATUSES,
        notice=_notice(request),
        error=error,
    )


@router.get("/jobs")
def jobs_view(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    return _jobs_page(request, db, auth)


@router.post("/jobs/{job_id}/retry")
def jobs_retry(job_id: int, request: Request, auth: AdminOperator, db: DbSession) -> Response:
    try:
        job_ops.retry_job(db, auth.operator, job_id)
    except job_ops.JobError as exc:
        return _jobs_page(request, db, auth, 400, str(exc))
    return _back("/admin/jobs", "job_retried", auth)


# --- assessments --------------------------------------------------------------------------------


def _assessments_page(
    request: Request,
    db: DbSession,
    auth: AdminOperator,
    status_code: int = 200,
    error: str | None = None,
    notice: str | None = None,
) -> Response:
    status = request.query_params.get("status", "all")
    if status not in assessment_ops.STATUS_FILTERS:
        status = "all"
    held, held_page = assessment_ops.held_queue(db, request.query_params.get("held_page"))
    rows, page = assessment_ops.recent_versions(
        db, status=status, page=request.query_params.get("page")
    )
    return _page(
        request,
        "admin/assessments.html",
        auth,
        status_code,
        held=held,
        held_page=held_page,
        rows=rows,
        page=page,
        status=status,
        filters=assessment_ops.STATUS_FILTERS,
        suspended=versions.publication_suspended(db),
        min_reason=assessment_ops.MIN_REASON,
        max_reason=assessment_ops.MAX_REASON,
        now=datetime.now(UTC),
        notice=notice or _notice(request),
        error=error,
    )


@router.get("/assessments")
def assessments_view(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    return _assessments_page(request, db, auth)


@router.post("/assessments/versions/{version_id}/withhold")
def assessments_withhold(
    version_id: int,
    request: Request,
    auth: AdminOperator,
    db: DbSession,
    reason: Annotated[str, Form()] = "",
) -> Response:
    try:
        assessment_ops.withhold(db, auth.operator, version_id, reason)
    except assessment_ops.AssessmentError as exc:
        return _assessments_page(request, db, auth, 400, str(exc))
    return _back("/admin/assessments", "withheld", auth)


@router.post("/assessments/versions/{version_id}/release")
def assessments_release(
    version_id: int,
    request: Request,
    auth: AdminOperator,
    db: DbSession,
    reason: Annotated[str, Form()] = "",
) -> Response:
    try:
        result = assessment_ops.release_early(db, auth.operator, version_id, reason)
    except assessment_ops.AssessmentError as exc:
        return _assessments_page(request, db, auth, 400, str(exc))
    if not result.published:
        # The policy withheld it; the outcome and the audit row are kept, so this is not a failure.
        return _assessments_page(
            request,
            db,
            auth,
            200,
            error="The publication policy withholds this version now ("
            + ", ".join(result.withheld_by)
            + "), so it was not published.",
        )
    return _back("/admin/assessments", "released", auth)


@router.post("/assessments/situations/{situation_id}/withdraw")
def assessments_withdraw(
    situation_id: int,
    request: Request,
    auth: AdminOperator,
    db: DbSession,
    reason: Annotated[str, Form()] = "",
) -> Response:
    try:
        assessment_ops.withdraw(db, auth.operator, situation_id, reason)
    except assessment_ops.AssessmentError as exc:
        return _assessments_page(request, db, auth, 400, str(exc))
    return _back("/admin/assessments", "withdrawn", auth)


# --- range checks -------------------------------------------------------------------------------


def _range_page(
    request: Request,
    db: DbSession,
    auth: AdminOperator,
    status: int = 200,
    error: str | None = None,
) -> Response:
    return _page(
        request,
        "admin/range_checks.html",
        auth,
        status,
        rows=range_review.pending(db),
        decided=range_review.recently_decided(db),
        min_reason=assessment_ops.MIN_REASON,
        notice=_notice(request),
        error=error,
    )


@router.get("/range-checks")
def range_view(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    return _range_page(request, db, auth)


@router.post("/range-checks/{review_id}/{action}")
def range_decide(
    review_id: int,
    action: str,
    request: Request,
    auth: AdminOperator,
    db: DbSession,
    note: Annotated[str, Form()] = "",
) -> Response:
    if action not in ("approve", "reject"):
        raise HTTPException(status_code=404, detail="No such action")
    try:
        if action == "approve":
            range_review.approve(db, auth.operator, review_id, note)
            notice = "range_approved"
        else:
            range_review.reject(db, auth.operator, review_id, note)
            notice = "range_rejected"
    except (range_review.ReviewError, assessment_ops.AssessmentError) as exc:
        return _range_page(request, db, auth, 400, str(exc))
    return _back("/admin/range-checks", notice, auth)


# --- costs --------------------------------------------------------------------------------------


@router.get("/costs")
def costs_view(request: Request, auth: AdminOperator, db: DbSession, days: int = 14) -> Response:
    days = min(max(days, 1), cost_ops.MAX_DAYS)
    spend = cost_ops.llm_spend(db, days=days)
    return _page(
        request,
        "admin/costs.html",
        auth,
        spend=spend,
        day_totals=cost_ops.day_totals(spend),
        budget=cost_ops.today(db),
        emails=cost_ops.email_counts(db, days=days),
        days=days,
    )


# --- NBS upload ---------------------------------------------------------------------------------


def _upload_page(
    request: Request,
    db: DbSession,
    auth: AdminOperator,
    status: int = 200,
    error: str | None = None,
    form: dict[str, Any] | None = None,
) -> Response:
    return _page(
        request,
        "admin/nbs_upload.html",
        auth,
        status,
        sources=nbs_upload.nbs_sources(db),
        publications=nbs_upload.publication_choices(),
        max_mb=nbs_upload.MAX_UPLOAD_BYTES // 1_000_000,
        form=form or {},
        notice=_notice(request),
        error=error,
    )


@router.get("/nbs-upload")
def upload_view(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    return _upload_page(request, db, auth)


@router.post("/nbs-upload")
async def upload_submit(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    # The form parser spools a file to disk without a limit of its own, so the size must be
    # declared (a chunked body has none) and within the cap before anything is read.
    declared = request.headers.get("content-length", "")
    if not (declared.isdigit() and declared.isascii()):
        return _upload_page(
            request, db, auth, 411, "The upload must state its size (a Content-Length header)."
        )
    if int(declared) > nbs_upload.MAX_UPLOAD_BYTES + 1_000_000:
        return _upload_page(
            request,
            db,
            auth,
            413,
            f"The file is larger than {nbs_upload.MAX_UPLOAD_BYTES // 1_000_000} MB.",
        )
    form = await request.form()
    text = {k: v for k, v in form.items() if isinstance(v, str)}
    upload = form.get("file")
    if not isinstance(upload, UploadFile):
        return _upload_page(request, db, auth, 400, "Choose the file to upload.", text)
    data = await upload.read(nbs_upload.MAX_UPLOAD_BYTES + 1)
    store = store_for_session(db)
    if store is None:
        return _upload_page(
            request,
            db,
            auth,
            400,
            "Object storage is not set up yet: add the bucket and keys under Settings.",
            text,
        )
    try:
        source_id = int(text.get("source_id", ""))
    except ValueError:
        return _upload_page(request, db, auth, 400, "Choose a source.", text)
    try:
        nbs_upload.queue_upload(
            db,
            store,
            auth.operator,
            source_id=source_id,
            publication=text.get("publication", ""),
            published_on=text.get("published_on", ""),
            original_url=text.get("original_url", ""),
            title=text.get("title", ""),
            data=data,
        )
    except nbs_upload.UploadError as exc:
        return _upload_page(request, db, auth, 400, str(exc), text)
    return _back("/admin/jobs", "upload_queued", auth)


# --- discovered domains -------------------------------------------------------------------------


def _domains_page(
    request: Request,
    db: DbSession,
    auth: AdminOperator,
    status: int = 200,
    error: str | None = None,
    form: dict[str, str] | None = None,
) -> Response:
    return _page(
        request,
        "admin/domains.html",
        auth,
        status,
        found=domain_ops.report(db),
        decided=domain_ops.decided(db),
        form=form or {},
        notice=_notice(request),
        error=error,
    )


@router.get("/domains")
def domains_view(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    return _domains_page(request, db, auth)


@router.post("/domains/reject")
def domains_reject(
    request: Request,
    auth: AdminOperator,
    db: DbSession,
    domain: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
) -> Response:
    try:
        domain_ops.reject(db, auth.operator, domain, note)
    except domain_ops.DomainError as exc:
        return _domains_page(request, db, auth, 400, str(exc))
    return _back("/admin/domains", "domain_rejected", auth)


@router.post("/domains/add")
def domains_add(
    request: Request,
    auth: AdminOperator,
    db: DbSession,
    domain: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
    owner: Annotated[str, Form()] = "",
    feed_url: Annotated[str, Form()] = "",
) -> Response:
    try:
        source = domain_ops.add_as_source(
            db, auth.operator, domain, domain_ops.NewSource(name, owner, feed_url)
        )
    except domain_ops.DomainError as exc:
        form = {"domain": domain, "name": name, "owner": owner, "feed_url": feed_url}
        return _domains_page(request, db, auth, 400, str(exc), form)
    return _redirect(f"/admin/sources/{source.id}?notice=domain_added", auth)


# --- policy situations --------------------------------------------------------------------------


def _policies_page(
    request: Request,
    db: DbSession,
    auth: AdminOperator,
    status: int = 200,
    error: str | None = None,
    form: dict[str, Any] | None = None,
) -> Response:
    return _page(
        request,
        "admin/policies.html",
        auth,
        status,
        rows=policy_ops.listing(db),
        sources=policy_ops.source_choices(db),
        topics=policy_ops.TOPICS,
        form=form or {},
        notice=_notice(request),
        error=error,
    )


@router.get("/policies")
def policies_view(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    return _policies_page(request, db, auth)


@router.post("/policies")
async def policies_add(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    form = await request.form()
    text = {k: v for k, v in form.items() if isinstance(v, str)}
    new = policy_ops.NewSeries(
        code=text.get("code", ""),
        title=text.get("title", ""),
        topic=text.get("topic", ""),
        unit=text.get("unit", ""),
        primary_sources=[v for v in form.getlist("primary_sources") if isinstance(v, str)],
        scope=text.get("scope", ""),
        state_codes=text.get("state_codes", ""),
        affected_groups=text.get("affected_groups", ""),
        materiality_pct=text.get("materiality_pct", ""),
    )
    try:
        _, created = policy_ops.add_series(db, auth.operator, new)
    except policy_ops.PolicyError as exc:
        shown: dict[str, Any] = {**text, "primary_sources": new.primary_sources}
        return _policies_page(request, db, auth, 400, str(exc), shown)
    return _back("/admin/policies", "policy_added" if created else "policy_added_no_places", auth)


@router.post("/policies/{row_id}/{action}")
def policies_toggle(
    row_id: int, action: str, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    if action not in ("retire", "activate"):
        raise HTTPException(status_code=404, detail="No such action")
    try:
        policy_ops.set_active(db, auth.operator, row_id, action == "activate")
    except policy_ops.PolicyError as exc:
        return _policies_page(request, db, auth, 400, str(exc))
    return _back(
        "/admin/policies", "policy_retired" if action == "retire" else "policy_activated", auth
    )


# --- private, draft-only editorial insights ---------------------------------------------------


def _editorial_drafts_page(
    request: Request,
    db: DbSession,
    auth: AdminOperator,
    status_code: int = 200,
    error: str | None = None,
) -> Response:
    rows = editorial_ops.recent(db)
    evidence = {draft.id: editorial_ops.evidence_for(db, draft) for draft, _, _ in rows}
    candidates = insight_candidate_ops.recent(db, now=datetime.now(UTC))
    return _page(
        request,
        "admin/editorial_drafts.html",
        auth,
        status_code,
        error=error,
        drafts=rows,
        evidence=evidence,
        candidates=candidates,
        candidate_quality=insight_candidate_ops.quality_metrics(db),
        now=datetime.now(UTC),
        notice=_notice(request),
    )


@router.get("/insights")
def editorial_drafts_view(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    return _editorial_drafts_page(request, db, auth)


@router.post("/insights/{draft_id}/review")
async def editorial_draft_review(
    draft_id: int, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    form = await request.form()
    decision = form.get("decision", "")
    reason = form.get("reason", "")
    if not isinstance(decision, str) or not isinstance(reason, str):
        return _editorial_drafts_page(
            request, db, auth, 400, "Review decision and reason are required."
        )
    try:
        editorial_ops.review(db, auth.operator, draft_id, decision, reason, now=datetime.now(UTC))
    except editorial_ops.EditorialReviewError as exc:
        return _editorial_drafts_page(request, db, auth, 409, str(exc))
    return _back(
        "/admin/insights",
        "insight_email_queued" if decision == "email_queued" else "insight_reviewed",
        auth,
    )


@router.post("/insight-candidates/{candidate_id}/review")
async def insight_candidate_review(
    candidate_id: int, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    form = await request.form()
    decision = form.get("decision", "")
    reason = form.get("reason", "")
    rating_value = form.get("usefulness_rating", "")
    scope_value = form.get("scope_correct", "")
    critical_value = form.get("unsupported_critical", "")
    if (
        not isinstance(decision, str)
        or not isinstance(reason, str)
        or not isinstance(rating_value, str)
        or not rating_value.isdigit()
        or scope_value not in {"true", "false"}
        or critical_value not in {"true", "false"}
    ):
        return _editorial_drafts_page(
            request,
            db,
            auth,
            400,
            "Record usefulness, scope, critical support, decision, and reason.",
        )
    try:
        insight_candidate_ops.review(
            db,
            auth.operator,
            candidate_id,
            decision,
            reason,
            int(rating_value),
            scope_value == "true",
            critical_value == "true",
            now=datetime.now(UTC),
        )
    except insight_candidate_ops.InsightCandidateError as exc:
        return _editorial_drafts_page(request, db, auth, 409, str(exc))
    return _back("/admin/insights", "candidate_reviewed", auth)


# --- channel posts ------------------------------------------------------------------------------


def _channel_page(
    request: Request,
    db: DbSession,
    auth: AdminOperator,
    status: int = 200,
    error: str | None = None,
) -> Response:
    days = min(max(_int(request.query_params.get("days"), 7), 1), 60)
    found = channel_ops.Drafts()
    try:
        found = channel_ops.drafts(
            db, datetime.now(UTC) - timedelta(days=days), request.query_params.get("page")
        )
    except PostError as exc:
        error = str(exc)  # a setting to fill in, not a bad request: the page still answers 200
    publications = social_ops.publications_for_versions(
        db, [item.version_id for item in found.posts]
    )
    return _page(
        request,
        "admin/channel_posts.html",
        auth,
        status,
        posts=found.posts,
        page=found.page,
        problems=found.problems,
        suspended=found.suspended,
        recorded=channel_ops.recent_records(db),
        publications=publications,
        recent_publications=social_ops.recent_publications(db),
        x_token_configured=settings_store.get(db, "x_user_access_token") is not None,
        x_publishing_enabled=settings_store.get(db, "x_publishing_enabled") == "yes",
        social_enabled={
            channel: settings_store.get(db, key) == "yes"
            for channel, key in {
                "facebook": "facebook_publishing_enabled",
                "instagram": "instagram_publishing_enabled",
                "telegram": "telegram_publishing_enabled",
                "youtube": "youtube_publishing_enabled",
            }.items()
        },
        social_ready={
            "x": settings_store.get(db, "x_publishing_enabled") == "yes"
            and bool(settings_store.get(db, "x_user_access_token")),
            "facebook": settings_store.get(db, "facebook_publishing_enabled") == "yes"
            and all(
                settings_store.get(db, key)
                for key in (
                    "facebook_page_id",
                    "facebook_page_access_token",
                    "meta_graph_api_version",
                )
            ),
            "instagram": settings_store.get(db, "instagram_publishing_enabled") == "yes"
            and all(
                settings_store.get(db, key)
                for key in (
                    "instagram_professional_account_id",
                    "instagram_access_token",
                    "meta_graph_api_version",
                )
            )
            and store_for_session(db) is not None,
            "telegram": settings_store.get(db, "telegram_publishing_enabled") == "yes"
            and all(
                settings_store.get(db, key) for key in ("telegram_channel_id", "telegram_bot_token")
            ),
            "youtube": settings_store.get(db, "youtube_publishing_enabled") == "yes"
            and all(
                settings_store.get(db, key)
                for key in (
                    "youtube_channel_id",
                    "youtube_oauth_client_id",
                    "youtube_oauth_client_secret",
                    "youtube_refresh_token",
                )
            )
            and store_for_session(db) is not None,
        },
        channels=channel_ops.CHANNEL_NAMES,
        days=days,
        notice=_notice(request),
        error=error,
    )


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


@router.get("/channel-posts")
def channel_posts_view(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    """Show current WhatsApp and X drafts; X requires explicit approval before dispatch."""
    return _channel_page(request, db, auth)


@router.post("/channel-posts/versions/{version_id}/x/approve")
def channel_posts_approve_x(
    version_id: int, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    try:
        social_ops.queue_current_x_post(db, auth.operator, version_id, datetime.now(UTC))
    except social_ops.SocialPublicationError as exc:
        return _channel_page(request, db, auth, 400, str(exc))
    return _back("/admin/channel-posts", "x_post_queued", auth)


@router.post("/channel-posts/versions/{version_id}/{channel}/approve")
async def channel_posts_approve_social(
    version_id: int, channel: str, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    video = None
    if channel == "youtube":
        declared = request.headers.get("content-length", "")
        if not (declared.isdigit() and declared.isascii()):
            return _channel_page(
                request, db, auth, 411, "The upload must state its size (a Content-Length header)."
            )
        if int(declared) > 51 * 1024 * 1024:
            return _channel_page(request, db, auth, 413, "YouTube video must be 50 MiB or smaller.")
        form = await request.form(max_files=1, max_fields=2)
        upload = form.get("video")
        if not isinstance(upload, UploadFile) or not upload.filename:
            return _channel_page(request, db, auth, 400, "Choose an MP4 video before approval.")
        video = await upload.read(50 * 1024 * 1024 + 1)
        await upload.close()
        if len(video) > 50 * 1024 * 1024:
            return _channel_page(request, db, auth, 400, "YouTube video must be 50 MiB or smaller.")
    try:
        social_ops.queue_current_social_post(
            db, auth.operator, version_id, channel, datetime.now(UTC), video=video
        )
    except social_ops.SocialPublicationError as exc:
        return _channel_page(request, db, auth, 400, str(exc))
    return _back("/admin/channel-posts", "publication_queued", auth)


@router.post("/channel-posts/publications/{publication_id}/retry")
def channel_posts_retry_social(
    publication_id: int, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    try:
        social_ops.retry_rejected_post(db, auth.operator, publication_id, datetime.now(UTC))
    except social_ops.SocialPublicationError as exc:
        return _channel_page(request, db, auth, 400, str(exc))
    return _back("/admin/channel-posts", "publication_queued", auth)


@router.post("/channel-posts/versions/{version_id}/{channel}/posted")
def channel_posts_mark(
    version_id: int,
    channel: str,
    request: Request,
    auth: AdminOperator,
    db: DbSession,
    post_url: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
) -> Response:
    try:
        channel_ops.mark_posted(
            db, auth.operator, version_id, channel, post_url=post_url, note=note
        )
    except channel_ops.ChannelPostError as exc:
        return _channel_page(request, db, auth, 400, str(exc))
    return _back("/admin/channel-posts", "post_marked", auth)


@router.post("/channel-posts/records/{record_id}/undo")
def channel_posts_unmark(
    record_id: int, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    try:
        channel_ops.unmark(db, auth.operator, record_id)
    except channel_ops.ChannelPostError as exc:
        return _channel_page(request, db, auth, 400, str(exc))
    return _back("/admin/channel-posts", "post_unmarked", auth)


# --- X social listening ------------------------------------------------------------------------


def _social_listening_page(
    request: Request,
    db: DbSession,
    auth: AdminOperator,
    status_code: int = 200,
    error: str | None = None,
    *,
    submitted: dict[str, str] | None = None,
    conflict: bool = False,
    field_error: str | None = None,
    create_values: dict[str, str] | None = None,
    query_values: dict[int, dict[str, str]] | None = None,
) -> Response:
    return _page(
        request,
        "admin/social_listening.html",
        auth,
        status_code,
        error=error,
        conflict=conflict,
        control_error=error,
        field_error=field_error,
        group=_settings_group(db, "social_listening", submitted, field_error),
        queries=listening_ops.query_rows(db),
        leads=listening_ops.lead_rows(db),
        polls=listening_ops.poll_rows(db),
        listening=listening_ops.status(db, datetime.now(UTC)),
        create_values=create_values or {},
        query_values=query_values or {},
        notice=_notice(request),
    )


@router.get("/social-listening")
def social_listening_view(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    return _social_listening_page(request, db, auth)


@router.post("/social-listening/settings")
async def social_listening_settings_save(
    request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    form = await request.form()
    submitted = {key: value for key, value in form.items() if isinstance(value, str)}
    try:
        expected = int(submitted.get("expected_revision", ""))
    except ValueError:
        return _social_listening_page(
            request, db, auth, 400, "Reload the listening page before saving.", submitted=submitted
        )
    changes: dict[str, str | None] = {}
    for key in settings_store.group_keys("social_listening"):
        if key not in submitted and f"clear__{key}" not in submitted:
            continue
        defn = settings_store.definition(key)
        value = submitted.get(key, "").strip()
        if submitted.get(f"clear__{key}") == "on":
            changes[key] = None
        elif defn.secret:
            if value:
                changes[key] = value
        elif value == "":
            changes[key] = None
        elif value != (settings_store.resolve(db, key).value or ""):
            changes[key] = value
    try:
        changed, _revision = settings_store.apply_group_changes(
            db, auth.operator, "social_listening", expected, changes
        )
    except settings_store.SettingsRevisionConflict as exc:
        return _social_listening_page(
            request, db, auth, 409, str(exc), submitted=submitted, conflict=True
        )
    except settings_store.SettingError as exc:
        return _social_listening_page(
            request, db, auth, 400, str(exc), submitted=submitted, field_error=exc.key
        )
    return _back(
        "/admin/social-listening",
        "social_settings_saved" if changed else "social_settings_unchanged",
        auth,
    )


@router.post("/social-listening/queries")
def social_listening_query_create(
    request: Request,
    auth: AdminOperator,
    db: DbSession,
    name: Annotated[str, Form()],
    query_text: Annotated[str, Form()],
    max_results: Annotated[str, Form()] = "20",
) -> Response:
    values = {"name": name, "query_text": query_text, "max_results": max_results}
    try:
        draft = listening_ops.QueryDraft.model_validate(values)
        listening_ops.create_query(db, auth.operator, draft, datetime.now(UTC))
    except ValidationError as exc:
        message = exc.errors()[0]["msg"] if exc.errors() else "Check the query fields."
        return _social_listening_page(request, db, auth, 400, message, create_values=values)
    except listening_ops.SocialListeningError as exc:
        return _social_listening_page(request, db, auth, 400, str(exc), create_values=values)
    return _back("/admin/social-listening", "social_query_created", auth)


@router.post("/social-listening/queries/{query_id}")
async def social_listening_query_update(
    query_id: int, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    form = await request.form()
    values = {key: value for key, value in form.items() if isinstance(value, str)}
    try:
        expected = int(values.get("expected_revision", ""))
        draft = listening_ops.QueryDraft.model_validate(
            {key: values[key] for key in ("name", "query_text", "max_results")}
        )
        listening_ops.update_query(
            db, auth.operator, query_id, expected, draft, values.get("enabled") == "yes"
        )
    except ValidationError as exc:
        message = exc.errors()[0]["msg"] if exc.errors() else "Check the query fields."
        return _social_listening_page(
            request, db, auth, 400, message, query_values={query_id: values}
        )
    except listening_ops.SocialListeningConflict as exc:
        return _social_listening_page(
            request, db, auth, 409, str(exc), conflict=True, query_values={query_id: values}
        )
    except listening_ops.SocialListeningError as exc:
        return _social_listening_page(
            request, db, auth, 400, str(exc), query_values={query_id: values}
        )
    except ValueError as exc:
        return _social_listening_page(
            request, db, auth, 400, str(exc), query_values={query_id: values}
        )
    return _back("/admin/social-listening", "social_query_updated", auth)


@router.post("/social-listening/poll")
def social_listening_run_now(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    if settings_store.get(db, "x_listening_enabled") != "yes":
        return _social_listening_page(
            request, db, auth, 400, "Enable X listening before running a search."
        )
    if not settings_store.get(db, "x_app_bearer_token"):
        return _social_listening_page(
            request, db, auth, 400, "Configure the encrypted X app bearer token first."
        )
    if not any(row.enabled for row in listening_ops.query_rows(db)):
        return _social_listening_page(
            request, db, auth, 400, "Add and enable at least one watch query first."
        )
    now = datetime.now(UTC)
    job_id = queue.enqueue(
        db,
        "social_listen_poll",
        {"force": True, "operator_id": auth.operator.id},
        dedupe_key=f"social-listen:manual:{int(now.timestamp()) // 60}",
    )
    if job_id is not None:
        audit.record(
            db,
            auth.operator,
            "social_listening.poll_queued",
            "job",
            job_id,
            after={"job_id": job_id, "force": True},
        )
    return _back("/admin/social-listening", "social_poll_queued", auth)


@router.post("/social-listening/leads/{lead_id}/{status}")
def social_listening_lead_status(
    lead_id: int, status: str, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    try:
        listening_ops.set_lead_status(db, auth.operator, lead_id, status)
    except listening_ops.SocialListeningError as exc:
        return _social_listening_page(request, db, auth, 400, str(exc))
    return _back("/admin/social-listening", "social_lead_updated", auth)


def _social_replies_page(
    request: Request,
    db: DbSession,
    auth: AdminOperator,
    status_code: int = 200,
    error: str | None = None,
) -> Response:
    now = datetime.now(UTC)
    return _page(
        request,
        "admin/social_replies.html",
        auth,
        status_code,
        error=error,
        status=social_reply_ops.poll_status(db, now),
        mentions=social_reply_ops.mentions(db),
        replies=social_reply_ops.replies(db),
        policy=social_reply_ops.reply_policy_status(db),
        policy_case_families=sorted(reply_policy_ops.REQUIRED_EVALUATION_CASE_FAMILIES),
        assessments=social_reply_ops.replyable_assessments(db, now),
        polls=social_reply_ops.poll_history(db),
        now=now,
        notice=_notice(request),
    )


@router.get("/social-replies")
def social_replies_view(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    return _social_replies_page(request, db, auth)


@router.post("/social-replies/poll")
def social_replies_poll(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    if settings_store.get(db, "x_replies_enabled") != "yes":
        return _social_replies_page(request, db, auth, 400, "Enable the X mention inbox first.")
    if not settings_store.get(db, "x_user_access_token"):
        return _social_replies_page(
            request, db, auth, 400, "Configure the encrypted X user token first."
        )
    if not (settings_store.get(db, "x_account_user_id") or "").isdigit():
        return _social_replies_page(
            request, db, auth, 400, "Configure the numeric X account ID first."
        )
    now = datetime.now(UTC)
    job_id = queue.enqueue(
        db,
        "social_mention_poll",
        {"force": True, "operator_id": auth.operator.id},
        dedupe_key=f"social-mentions:manual:{int(now.timestamp()) // 60}",
    )
    if job_id is not None:
        audit.record(
            db,
            auth.operator,
            "social_mentions.poll_queued",
            "job",
            job_id,
            after={"job_id": job_id, "force": True},
        )
    return _back("/admin/social-replies", "social_mention_poll_queued", auth)


@router.post("/social-replies/policies/{revision}/evaluate")
async def social_reply_policy_evaluate(
    revision: int, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    form = await request.form()
    coverage: dict[str, dict[str, int]] = {}
    try:
        for case in reply_policy_ops.REQUIRED_EVALUATION_CASE_FAMILIES:
            cases_value = form.get(f"cases_{case}", "")
            passed_value = form.get(f"passed_{case}", "")
            if (
                not isinstance(cases_value, str)
                or not cases_value.isdigit()
                or not isinstance(passed_value, str)
                or not passed_value.isdigit()
            ):
                raise ValueError("record case and passing counts for every required case family")
            coverage[case] = {"cases": int(cases_value), "passed": int(passed_value)}
        errors_value = form.get("critical_errors", "")
        evidence_ref = form.get("evaluation_evidence_ref", "")
        if (
            not isinstance(errors_value, str)
            or not errors_value.isdigit()
            or not isinstance(evidence_ref, str)
        ):
            raise ValueError("record a critical-error count and evaluation evidence reference")
        policy_row = reply_policy_ops.record_evaluation(
            db,
            auth.operator,
            revision,
            coverage,
            int(errors_value),
            evidence_ref,
            now=datetime.now(UTC),
        )
    except ValueError as exc:
        return _social_replies_page(request, db, auth, 400, str(exc))
    notice = (
        "social_reply_policy_evaluated"
        if policy_row.status == "evaluated"
        else "social_reply_policy_evaluation_failed"
    )
    return _back("/admin/social-replies", notice, auth)


@router.post("/social-replies/policies/{revision}/approve")
async def social_reply_policy_approve(
    revision: int, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    form = await request.form()
    reason = form.get("reason", "")
    if not isinstance(reason, str):
        return _social_replies_page(
            request, db, auth, 400, "An editorial approval reason is required."
        )
    try:
        reply_policy_ops.approve(db, auth.operator, revision, reason, now=datetime.now(UTC))
    except ValueError as exc:
        return _social_replies_page(request, db, auth, 409, str(exc))
    return _back("/admin/social-replies", "social_reply_policy_approved", auth)


@router.post("/social-replies/policies/{revision}/activate")
async def social_reply_policy_activate(
    revision: int, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    form = await request.form()
    evidence_ref = form.get("staging_evidence_ref", "")
    reason = form.get("reason", "")
    if not isinstance(evidence_ref, str) or not isinstance(reason, str):
        return _social_replies_page(
            request, db, auth, 400, "Staging evidence and activation reason are required."
        )
    try:
        reply_policy_ops.activate(
            db,
            auth.operator,
            revision,
            evidence_ref,
            reason,
            now=datetime.now(UTC),
        )
    except ValueError as exc:
        return _social_replies_page(request, db, auth, 409, str(exc))
    return _back("/admin/social-replies", "social_reply_policy_activated", auth)


@router.post("/social-replies/policies/{revision}/retire")
async def social_reply_policy_retire(
    revision: int, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    form = await request.form()
    reason = form.get("reason", "")
    if not isinstance(reason, str):
        return _social_replies_page(request, db, auth, 400, "A retirement reason is required.")
    try:
        reply_policy_ops.retire(db, auth.operator, revision, reason, now=datetime.now(UTC))
    except ValueError as exc:
        return _social_replies_page(request, db, auth, 409, str(exc))
    return _back("/admin/social-replies", "social_reply_policy_retired", auth)


@router.post("/social-replies/policies/{revision}/revise")
async def social_reply_policy_revise(
    revision: int, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    form = await request.form()
    reason = form.get("reason", "")
    policy_value = form.get("policy_json", "")
    if not isinstance(reason, str) or not isinstance(policy_value, str):
        return _social_replies_page(
            request, db, auth, 400, "A reason and policy document are required."
        )
    try:
        policy_document = json.loads(policy_value)
        if not isinstance(policy_document, dict):
            raise ValueError("policy document must be a JSON object")
        reply_policy_ops.revise(
            db,
            auth.operator,
            revision,
            reason,
            policy_document,
            now=datetime.now(UTC),
        )
    except ValueError as exc:
        return _social_replies_page(request, db, auth, 409, str(exc))
    return _back("/admin/social-replies", "social_reply_policy_revised", auth)


@router.post("/social-replies/mentions/{mention_id}/draft")
async def social_reply_draft(
    mention_id: int, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    form = await request.form()
    assessment_id = form.get("assessment_version_id", "")
    triage = form.get("triage", "")
    if (
        not isinstance(assessment_id, str)
        or not assessment_id.isdigit()
        or not isinstance(triage, str)
    ):
        return _social_replies_page(
            request,
            db,
            auth,
            400,
            "Choose an in-scope dated-fact request and a current assessment.",
        )
    try:
        social_reply_ops.create_reply_draft(
            db,
            auth.operator,
            mention_id,
            int(assessment_id),
            triage,
            now=datetime.now(UTC),
        )
    except social_reply_ops.SocialReplyError as exc:
        return _social_replies_page(request, db, auth, 409, str(exc))
    return _back("/admin/social-replies", "social_reply_draft_created", auth)


@router.post("/social-replies/mentions/{mention_id}/{status}")
async def social_mention_triage(
    mention_id: int,
    status: str,
    request: Request,
    auth: AdminOperator,
    db: DbSession,
) -> Response:
    form = await request.form()
    category_value = form.get("category", "other")
    category = category_value if isinstance(category_value, str) else "other"
    try:
        social_reply_ops.triage_mention(db, auth.operator, mention_id, status, category)
    except social_reply_ops.SocialReplyError as exc:
        return _social_replies_page(request, db, auth, 409, str(exc))
    return _back("/admin/social-replies", "social_reply_triaged", auth)


@router.post("/social-replies/{reply_id}/review")
async def social_reply_review(
    reply_id: int, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    form = await request.form()
    decision = form.get("decision", "")
    reason = form.get("reason", "")
    if not isinstance(decision, str) or not isinstance(reason, str):
        return _social_replies_page(
            request, db, auth, 400, "A decision and review reason are required."
        )
    try:
        social_reply_ops.review_reply(
            db,
            auth.operator,
            reply_id,
            decision,
            reason,
            now=datetime.now(UTC),
        )
    except social_reply_ops.SocialReplyError as exc:
        return _social_replies_page(request, db, auth, 409, str(exc))
    return _back("/admin/social-replies", "social_reply_reviewed", auth)


# --- corrections to already delivered items ---------------------------------------------------


def _delivery_corrections_page(
    request: Request,
    db: DbSession,
    auth: AdminOperator,
    status_code: int = 200,
    error: str | None = None,
) -> Response:
    return _page(
        request,
        "admin/delivery_corrections.html",
        auth,
        status_code,
        error=error,
        rows=delivery_correction_ops.listing(db, now=datetime.now(UTC)),
        notice=_notice(request),
    )


@router.get("/delivery-corrections")
def delivery_corrections_view(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    return _delivery_corrections_page(request, db, auth)


@router.post("/delivery-corrections/{work_id}/review")
async def delivery_correction_review(
    work_id: int, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    form = await request.form()
    decision = form.get("decision", "")
    reason = form.get("reason", "")
    follow_up_value = form.get("follow_up_attempt_id", "")
    if (
        not isinstance(decision, str)
        or not isinstance(reason, str)
        or not isinstance(follow_up_value, str)
    ):
        return _delivery_corrections_page(
            request, db, auth, 400, "A decision and review reason are required."
        )
    if follow_up_value and not follow_up_value.isdigit():
        return _delivery_corrections_page(request, db, auth, 400, "Invalid receipt selection.")
    try:
        delivery_correction_ops.review(
            db,
            auth.operator,
            work_id,
            decision,
            reason,
            follow_up_attempt_id=int(follow_up_value) if follow_up_value else None,
            now=datetime.now(UTC),
        )
    except delivery_correction_ops.DeliveryCorrectionError as exc:
        return _delivery_corrections_page(request, db, auth, 409, str(exc))
    return _back("/admin/delivery-corrections", "delivery_correction_reviewed", auth)


# --- alerts -------------------------------------------------------------------------------------


@router.get("/alerts")
def alerts_view(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    return _page(
        request,
        "admin/alerts.html",
        auth,
        alerts=alerts_ops.alerts(db),
        page_checks=alerts_ops.recent_guardian_checks(db),
        backup=alerts_ops.status_record(db, backup_alerts.BACKUP_STATUS_KEY),
        drill=alerts_ops.status_record(db, backup_alerts.DRILL_STATUS_KEY),
        history=alerts_ops.history(db),
    )
