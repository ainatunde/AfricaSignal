"""Private deterministic commercial delivery report and bounded aggregate rebuild."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from africasignal.operations import commercial_reporting
from africasignal.web.deps import AdminOperator, DbSession
from africasignal.web.routes.admin import _page, _redirect

router = APIRouter(prefix="/admin")


@router.get("/commercial/reports")
def commercial_report(
    request: Request,
    auth: AdminOperator,
    db: DbSession,
    days: Annotated[int, Query(ge=1, le=30)] = 30,
) -> Response:
    report = commercial_reporting.build_report(db, days=days, now=datetime.now(UTC))
    return _page(
        request,
        "admin/commercial_reports.html",
        auth,
        days=days,
        report=report,
        error=None,
    )


@router.post("/commercial/reports/rebuild")
async def commercial_report_rebuild(
    request: Request,
    auth: AdminOperator,
    db: DbSession,
) -> Response:
    days = 30
    try:
        form = await request.form()
        pairs = list(form.multi_items())
        if len(pairs) != 1 or pairs[0][0] != "days" or not isinstance(pairs[0][1], str):
            raise ValueError("report request fields are invalid")
        days = int(pairs[0][1])
        if not 1 <= days <= 30:
            raise ValueError("report rebuild is limited to 1-30 days")
        commercial_reporting.rebuild_recent_aggregates(
            db,
            days=days,
            now=datetime.now(UTC),
        )
    except (ValueError, TypeError) as exc:
        db.rollback()
        safe_days = days if 1 <= days <= 30 else 30
        return _page(
            request,
            "admin/commercial_reports.html",
            auth,
            400,
            days=safe_days,
            report=commercial_reporting.build_report(db, days=safe_days, now=datetime.now(UTC)),
            error=str(exc),
        )
    return _redirect(f"/admin/commercial/reports?days={days}", auth)
