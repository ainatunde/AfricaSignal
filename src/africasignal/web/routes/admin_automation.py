"""Dashboard and private API for workload switches and operating windows."""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError

from africasignal.jobs.policy import WorkloadSchedule, schedule_from_text
from africasignal.operations import commercial_controls, workloads
from africasignal.operations.commercial_controls import (
    CommercialControlChange,
)
from africasignal.operations.commercial_controls import (
    RevisionConflict as CommercialRevisionConflict,
)
from africasignal.operations.workloads import (
    RevisionConflict,
    WorkloadChange,
    WorkloadName,
)
from africasignal.web.deps import AdminOperator, Authenticated, DbSession
from africasignal.web.routes.admin import _page, _redirect

router = APIRouter(prefix="/admin")


def _window_text(schedule: WorkloadSchedule) -> str:
    return "\n".join(
        f"{','.join(str(day) for day in window.days)} {window.start}-{window.end}"
        for window in schedule.windows
    )


def _form_values(row: dict[str, object]) -> dict[str, object]:
    schedule = row["schedule"]
    assert isinstance(schedule, WorkloadSchedule)
    return {
        **row,
        "timezone": schedule.timezone,
        "max_concurrency": schedule.max_concurrency,
        "max_items_per_run": schedule.max_items_per_run,
        "windows_text": _window_text(schedule),
    }


def _view(
    request: Request,
    db: Any,
    auth: Authenticated,
    status_code: int = 200,
    error: str | None = None,
    submitted: dict[str, str] | None = None,
    commercial_error: str | None = None,
    commercial_submitted: dict[str, str] | None = None,
) -> Response:
    rows = workloads.current(db)
    commercial = commercial_controls.current(db)
    if commercial_submitted is not None:
        for key in ("global_enabled", "explore_sponsorship_enabled", "context_ai_enabled"):
            commercial[key] = commercial_submitted.get(key) == "on"
    contexts = []
    for row in rows:
        values = _form_values(row)
        if submitted and submitted.get("workload") == row["name"]:
            values.update(
                {
                    "enabled": submitted.get("enabled") == "on",
                    "timezone": submitted.get("timezone", ""),
                    "max_concurrency": submitted.get("max_concurrency", ""),
                    "max_items_per_run": submitted.get("max_items_per_run", ""),
                    "windows_text": submitted.get("windows", ""),
                    "revision": submitted.get("expected_revision", row["revision"]),
                }
            )
        contexts.append(values)
    return _page(
        request,
        "admin/automation.html",
        auth,
        status_code,
        workloads=contexts,
        error=error,
        submitted=submitted,
        commercial=commercial,
        commercial_error=commercial_error,
    )


@router.get("/automation")
def automation_view(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    return _view(request, db, auth)


@router.post("/automation/commercial")
async def commercial_control_save(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    form = await request.form()
    submitted = {key: value for key, value in form.items() if isinstance(value, str)}
    try:
        change = CommercialControlChange(
            global_enabled=submitted.get("global_enabled") == "on",
            explore_sponsorship_enabled=submitted.get("explore_sponsorship_enabled") == "on",
            context_ai_enabled=submitted.get("context_ai_enabled") == "on",
            expected_revision=int(submitted.get("expected_revision", "")),
        )
        commercial_controls.configure(db, auth.operator, change)
    except CommercialRevisionConflict as exc:
        return _view(
            request,
            db,
            auth,
            409,
            commercial_error=str(exc),
            commercial_submitted=submitted,
        )
    except (ValueError, TypeError, ValidationError, PermissionError) as exc:
        return _view(
            request,
            db,
            auth,
            400,
            commercial_error=str(exc),
            commercial_submitted=submitted,
        )
    return _redirect("/admin/automation?notice=commercial_saved", auth)


@router.post("/automation/{name}")
async def automation_save(
    name: str, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    if name not in ("ai", "agent_reach", "external_agents", "processing", "commercial"):
        raise HTTPException(status_code=404, detail="No such workload")
    workload_name = cast(WorkloadName, name)
    form = await request.form()
    submitted: dict[str, str] = {
        key: value for key, value in form.items() if isinstance(value, str)
    }
    submitted["workload"] = name
    try:
        schedule = schedule_from_text(submitted.get("windows", ""))
        schedule = WorkloadSchedule.model_validate(
            {
                **schedule.model_dump(mode="json"),
                "timezone": submitted.get("timezone"),
                "max_concurrency": int(submitted.get("max_concurrency", "")),
                "max_items_per_run": int(submitted.get("max_items_per_run", "")),
            }
        )
        change = WorkloadChange(
            enabled=submitted.get("enabled") == "on",
            schedule=schedule,
            expected_revision=int(submitted.get("expected_revision", "")),
        )
        workloads.configure(db, auth.operator, workload_name, change)
    except RevisionConflict as exc:
        return _view(request, db, auth, 409, error=str(exc), submitted=submitted)
    except (ValueError, TypeError, ValidationError) as exc:
        return _view(request, db, auth, 400, error=str(exc), submitted=submitted)
    return _redirect("/admin/automation?notice=saved", auth)


@router.get("/api/automation")
def automation_api(auth: AdminOperator, db: DbSession) -> JSONResponse:
    rows = workloads.current(db)
    return JSONResponse(
        [
            {
                "name": row["name"],
                "enabled": row["enabled"],
                "effective": row["effective"],
                "active_now": row["active_now"],
                "next_open": cast(datetime, row["next_open"]).isoformat(),
                "queued": row["queued"],
                "running": row["running"],
                "revision": row["revision"],
                "schedule": cast(WorkloadSchedule, row["schedule"]).model_dump(mode="json"),
            }
            for row in rows
        ]
    )


@router.put("/api/automation/{name}")
async def automation_api_save(
    name: str, request: Request, auth: AdminOperator, db: DbSession
) -> JSONResponse:
    if name not in ("ai", "agent_reach", "external_agents", "processing", "commercial"):
        raise HTTPException(status_code=404, detail="No such workload")
    workload_name = cast(WorkloadName, name)
    try:
        change = WorkloadChange.model_validate(await request.json())
        row = workloads.configure(db, auth.operator, workload_name, change)
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except (ValueError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return JSONResponse(
        {
            "name": row.name,
            "enabled": row.enabled,
            "revision": row.revision,
            "schedule": row.schedule,
        }
    )
