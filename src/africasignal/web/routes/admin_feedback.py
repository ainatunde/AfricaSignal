"""Console pages for feedback and the demand-test metrics (spec B11.5, AS-034).

Feedback: the inbox, and a detail page where an operator sets the status and a resolution note
(audited). Both roles can triage. Metrics: the D1 numbers by week (``metrics.d1_report``); an
admin records the two numbers that come from outside the app (WhatsApp channel followers for a
week, and the monthly infrastructure bill).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import select

from africasignal import audit, metrics
from africasignal.models import AppUser, AssessmentVersion, Feedback, Situation
from africasignal.web.deps import AdminOperator, CurrentOperator, DbSession
from africasignal.web.routes.admin import _page, _redirect

router = APIRouter(prefix="/admin")

STATUSES = (
    "received",
    "triaged",
    "investigating",
    "resolved_updated",
    "resolved_no_change",
    "resolved_insufficient",
    "closed",
)
STATUS_WORDS = {
    "received": "Received",
    "triaged": "Triaged",
    "investigating": "Investigating",
    "resolved_updated": "Resolved: page updated",
    "resolved_no_change": "Resolved: no change needed",
    "resolved_insufficient": "Resolved: not enough evidence",
    "closed": "Closed",
}
KIND_WORDS = {
    "useful_yes": "Useful: yes",
    "useful_no": "Useful: no",
    "error_report": "Error report",
}
OPEN_STATUSES = ("received", "triaged", "investigating")
FINISHED = tuple(s for s in STATUSES if s.startswith("resolved") or s == "closed")
MAX_NOTE = 2000
LIST_LIMIT = 200

NOTICES = {
    "feedback_saved": "Feedback updated.",
    "wa_saved": "Follower count saved.",
    "infra_saved": "Monthly infrastructure cost saved.",
}


@router.get("/feedback")
def feedback_inbox(
    request: Request,
    auth: CurrentOperator,
    db: DbSession,
    show: str = "open",
    kind: str = "reports",
) -> Response:
    query = (
        select(Feedback, Situation)
        .join(AssessmentVersion, AssessmentVersion.id == Feedback.assessment_version_id)
        .join(Situation, Situation.id == AssessmentVersion.situation_id)
        .order_by(Feedback.id.desc())
        .limit(LIST_LIMIT)
    )
    if show == "open":
        query = query.where(Feedback.status.in_(OPEN_STATUSES))
    elif show in STATUSES:
        query = query.where(Feedback.status == show)
    else:
        show = "all"
    if kind == "reports":
        query = query.where(Feedback.kind == "error_report")
    elif kind in KIND_WORDS:
        query = query.where(Feedback.kind == kind)
    else:
        kind = "all"
    rows = db.execute(query).all()
    return _page(
        request,
        "admin/feedback.html",
        auth,
        rows=rows,
        show=show,
        kind=kind,
        statuses=STATUSES,
        status_words=STATUS_WORDS,
        kind_words=KIND_WORDS,
        limit=LIST_LIMIT,
        notice=NOTICES.get(request.query_params.get("notice", "")),
    )


def _feedback_page(
    request: Request,
    db: DbSession,
    auth: CurrentOperator,
    feedback_id: int,
    error: str | None = None,
) -> Response:
    found = db.execute(
        select(Feedback, Situation, AssessmentVersion)
        .join(AssessmentVersion, AssessmentVersion.id == Feedback.assessment_version_id)
        .join(Situation, Situation.id == AssessmentVersion.situation_id)
        .where(Feedback.id == feedback_id)
    ).first()
    if found is None:
        raise HTTPException(status_code=404, detail="No such feedback")
    feedback, situation, version = found
    reader = db.get(AppUser, feedback.user_id) if feedback.user_id else None
    return _page(
        request,
        "admin/feedback_detail.html",
        auth,
        400 if error else 200,
        feedback=feedback,
        situation=situation,
        version=version,
        reader=reader,
        statuses=STATUSES,
        status_words=STATUS_WORDS,
        kind_words=KIND_WORDS,
        error=error,
    )


@router.get("/feedback/{feedback_id}")
def feedback_detail(
    feedback_id: int, request: Request, auth: CurrentOperator, db: DbSession
) -> Response:
    return _feedback_page(request, db, auth, feedback_id)


@router.post("/feedback/{feedback_id}")
def feedback_update(
    feedback_id: int,
    request: Request,
    auth: CurrentOperator,
    db: DbSession,
    status: Annotated[str, Form()] = "",
    resolution_note: Annotated[str, Form()] = "",
) -> Response:
    feedback = db.get(Feedback, feedback_id)
    if feedback is None:
        raise HTTPException(status_code=404, detail="No such feedback")
    note = resolution_note.strip()
    if status not in STATUSES:
        return _feedback_page(request, db, auth, feedback_id, "Choose a status from the list.")
    if len(note) > MAX_NOTE:
        return _feedback_page(
            request, db, auth, feedback_id, f"Keep the note under {MAX_NOTE} characters."
        )
    if status in FINISHED and not note:
        return _feedback_page(
            request, db, auth, feedback_id, "Add a short note saying what was decided."
        )
    before = {"status": feedback.status, "resolution_note": feedback.resolution_note}
    feedback.status = status
    feedback.resolution_note = note or None
    if status in FINISHED:
        if feedback.resolved_at is None:
            feedback.resolved_at = datetime.now(UTC)
    else:
        feedback.resolved_at = None
    audit.record(
        db,
        auth.operator,
        "feedback.update",
        "feedback",
        feedback.id,
        before=before,
        after={"status": feedback.status, "resolution_note": feedback.resolution_note},
    )
    return _redirect("/admin/feedback?notice=feedback_saved", auth)


# --- metrics ------------------------------------------------------------------------------------


@router.get("/metrics")
def metrics_page(
    request: Request, auth: CurrentOperator, db: DbSession, weeks: int = 12
) -> Response:
    now = datetime.now(UTC)
    report = metrics.d1_report(db, now, weeks=min(max(weeks, 2), 52))
    return _page(
        request,
        "admin/metrics.html",
        auth,
        report=report,
        summary=report.summary(),
        rows=list(reversed(report.weeks)),  # newest first
        current_week=metrics.iso_week_label(now),
        is_admin=auth.operator.role == "admin",
        notice=NOTICES.get(request.query_params.get("notice", "")),
    )


@router.post("/metrics/whatsapp")
def save_whatsapp(
    request: Request,
    auth: AdminOperator,
    db: DbSession,
    week: Annotated[str, Form()] = "",
    followers: Annotated[str, Form()] = "",
) -> Response:
    try:
        count = int(followers.strip())
        metrics.record_whatsapp_followers(db, auth.operator, week.strip(), count)
    except ValueError as exc:
        return _metrics_error(request, db, auth, str(exc) or "Enter a whole number.")
    return _redirect("/admin/metrics?notice=wa_saved", auth)


@router.post("/metrics/infra")
def save_infra(
    request: Request,
    auth: AdminOperator,
    db: DbSession,
    monthly_usd: Annotated[str, Form()] = "",
) -> Response:
    try:
        amount = Decimal(monthly_usd.strip())
        metrics.record_infra_cost(db, auth.operator, amount)
    except (InvalidOperation, ValueError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "Enter a number."
        return _metrics_error(request, db, auth, message)
    return _redirect("/admin/metrics?notice=infra_saved", auth)


def _metrics_error(
    request: Request, db: DbSession, auth: CurrentOperator, message: str
) -> Response:
    now = datetime.now(UTC)
    report = metrics.d1_report(db, now)
    return _page(
        request,
        "admin/metrics.html",
        auth,
        400,
        report=report,
        summary=report.summary(),
        rows=list(reversed(report.weeks)),
        current_week=metrics.iso_week_label(now),
        is_admin=auth.operator.role == "admin",
        error=message,
    )
