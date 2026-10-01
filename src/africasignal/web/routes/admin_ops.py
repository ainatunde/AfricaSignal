"""Console pages for operations (spec B11.5): Jobs, Assessments with the R7 hold queue, range
checks, Costs, NBS upload, discovered domains, Channel posts and backup alerts.

All of them are admin-only. Every change goes through ``africasignal.operations``, which records
the audit row in the same transaction as the change. Nothing here sends anything to an outside
service: channel posts are drafts to copy, and the NBS upload only stores a file and queues the
existing import job.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import Response
from starlette.datastructures import UploadFile

from africasignal import backup_alerts
from africasignal.operations import alerts as alerts_ops
from africasignal.operations import assessments as assessment_ops
from africasignal.operations import channel_posts as channel_ops
from africasignal.operations import costs as cost_ops
from africasignal.operations import domains as domain_ops
from africasignal.operations import jobs as job_ops
from africasignal.operations import nbs_upload, range_review
from africasignal.publish import versions
from africasignal.publish.whatsapp_text import PostError
from africasignal.storage import store_for_session
from africasignal.web.deps import AdminOperator, DbSession
from africasignal.web.routes.admin import _page, _redirect

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
    return _page(
        request,
        "admin/assessments.html",
        auth,
        status_code,
        held=assessment_ops.held_queue(db),
        rows=assessment_ops.recent_versions(db, status=status),
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
        publications=nbs_upload.publication_codes(),
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


# --- channel posts ------------------------------------------------------------------------------


@router.get("/channel-posts")
def channel_posts_view(
    request: Request, auth: AdminOperator, db: DbSession, days: int = 7
) -> Response:
    """Drafts of the WhatsApp and X posts for material changes. Nothing is sent: an operator
    copies the text and posts it by hand."""
    days = min(max(days, 1), 60)
    error: str | None = None
    found = channel_ops.Drafts()
    try:
        found = channel_ops.drafts(db, datetime.now(UTC) - timedelta(days=days))
    except PostError as exc:
        error = str(exc)
    return _page(
        request,
        "admin/channel_posts.html",
        auth,
        400 if error else 200,
        posts=found.posts,
        problems=found.problems,
        days=days,
        error=error,
    )


# --- alerts -------------------------------------------------------------------------------------


@router.get("/alerts")
def alerts_view(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    return _page(
        request,
        "admin/alerts.html",
        auth,
        alerts=alerts_ops.alerts(db),
        backup=alerts_ops.status_record(db, backup_alerts.BACKUP_STATUS_KEY),
        drill=alerts_ops.status_record(db, backup_alerts.DRILL_STATUS_KEY),
        history=alerts_ops.history(db),
    )
