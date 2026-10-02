"""Admin dashboard and private API for supervised external-agent profiles."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, ValidationError

from africasignal.config import get_settings
from africasignal.jobs.policy import WorkloadSchedule
from africasignal.operations import external_agents as agent_ops
from africasignal.operations.external_agents import (
    ExternalAgentError,
    ProfileDraft,
    ProfileUpdate,
    RevisionConflict,
    TaskRequest,
)
from africasignal.operations.workloads import current as workload_rows
from africasignal.web.deps import AdminOperator, Authenticated, DbSession
from africasignal.web.routes.admin import _page, _redirect

router = APIRouter(prefix="/admin")


class EnabledChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    expected_revision: int


def _form_mapping(form: Any, slug: str | None = None) -> dict[str, Any]:
    raw: dict[str, Any] = {key: value for key, value in form.items() if isinstance(value, str)}
    raw["slug"] = slug or raw.get("slug", "")
    raw["credential"] = raw.get("credential") or None
    raw["allowed_purposes"] = [
        value for value in form.getlist("allowed_purposes") if isinstance(value, str)
    ]
    domains = str(raw.get("allowed_domains", "")).replace(",", "\n")
    raw["allowed_domains"] = [value.strip() for value in domains.splitlines() if value.strip()]
    return raw


def _workload(db: Any) -> dict[str, object] | None:
    return next((row for row in workload_rows(db) if row["name"] == "external_agents"), None)


def _view(
    request: Request,
    db: Any,
    auth: Authenticated,
    status_code: int = 200,
    error: str | None = None,
    submitted: dict[str, Any] | None = None,
) -> Response:
    notice = {
        "profile_created": (
            "External-agent profile saved disabled. Test its protocol before enabling."
        ),
        "profile_updated": (
            "Profile updated and health check invalidated; test it again before enabling."
        ),
        "profile_enabled": "External-agent profile enabled.",
        "profile_disabled": (
            "External-agent profile disabled; cancellation and reconciliation remain active."
        ),
        "health_ok": (
            "Remote health contract passed. Its declared enforcement still needs independent "
            "qualification."
        ),
        "task_queued": "External-agent task queued inside the configured workload policy.",
        "task_cancelled": "Cancellation or remote reconciliation queued.",
    }.get(request.query_params.get("notice", ""))
    return _page(
        request,
        "admin/agents.html",
        auth,
        status_code,
        profiles=agent_ops.profiles(db),
        tasks=agent_ops.recent_tasks(db),
        workload=_workload(db),
        notice=notice,
        error=error,
        submitted=submitted or {},
    )


@router.get("/agents")
def agents_view(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    return _view(request, db, auth)


@router.post("/agents/profiles")
async def create_profile(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    form = await request.form()
    values = _form_mapping(form)
    try:
        draft = ProfileDraft.model_validate(values)
        agent_ops.configure_profile(db, auth.operator, draft)
    except (ExternalAgentError, ValidationError, ValueError, TypeError) as exc:
        return _view(request, db, auth, 400, str(exc), values)
    return _redirect("/admin/agents?notice=profile_created", auth)


@router.post("/agents/profiles/{slug}")
async def update_profile(
    slug: str, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    form = await request.form()
    values = _form_mapping(form, slug)
    try:
        update = ProfileUpdate.model_validate(
            {**values, "expected_revision": int(values.get("expected_revision", ""))}
        )
        draft = ProfileDraft.model_validate(
            {key: value for key, value in update.model_dump().items() if key != "expected_revision"}
            | {"slug": slug}
        )
        agent_ops.configure_profile(
            db, auth.operator, draft, expected_revision=update.expected_revision
        )
    except RevisionConflict as exc:
        return _view(request, db, auth, 409, str(exc), values)
    except (ExternalAgentError, ValidationError, ValueError, TypeError) as exc:
        return _view(request, db, auth, 400, str(exc), values)
    return _redirect("/admin/agents?notice=profile_updated", auth)


@router.post("/agents/profiles/{slug}/enabled")
async def set_profile_enabled(
    slug: str, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    form = await request.form()
    try:
        agent_ops.set_profile_enabled(
            db,
            auth.operator,
            slug,
            enabled=form.get("enabled") == "on",
            expected_revision=int(str(form.get("expected_revision", ""))),
        )
    except RevisionConflict as exc:
        return _view(request, db, auth, 409, str(exc))
    except (ExternalAgentError, ValueError, TypeError) as exc:
        return _view(request, db, auth, 400, str(exc))
    notice = "profile_enabled" if form.get("enabled") == "on" else "profile_disabled"
    return _redirect("/admin/agents?notice=" + notice, auth)


@router.post("/agents/profiles/{slug}/health")
def test_profile(slug: str, request: Request, auth: AdminOperator, db: DbSession) -> Response:
    try:
        agent_ops.test_profile(db, auth.operator, slug)
    except ExternalAgentError as exc:
        return _view(request, db, auth, 502, str(exc))
    return _redirect("/admin/agents?notice=health_ok", auth)


@router.post("/agents/profiles/{slug}/tasks")
async def submit_task(slug: str, request: Request, auth: AdminOperator, db: DbSession) -> Response:
    form = await request.form()
    values = {key: value for key, value in form.items() if isinstance(value, str)}
    try:
        task_request = TaskRequest.model_validate(values)
        agent_ops.submit_task(db, auth.operator, slug, task_request)
    except (ExternalAgentError, ValidationError, ValueError, TypeError) as exc:
        return _view(request, db, auth, 400, str(exc), values)
    return _redirect("/admin/agents?notice=task_queued", auth)


@router.post("/agents/tasks/{task_id}/cancel")
def cancel_task(task_id: int, request: Request, auth: AdminOperator, db: DbSession) -> Response:
    try:
        agent_ops.cancel_task(db, auth.operator, task_id)
    except ExternalAgentError as exc:
        return _view(request, db, auth, 409, str(exc))
    return _redirect("/admin/agents?notice=task_cancelled", auth)


@router.get("/api/agents")
def agents_api(auth: AdminOperator, db: DbSession) -> JSONResponse:
    workload = _workload(db)
    if workload is not None:
        schedule = workload["schedule"]
        if isinstance(schedule, WorkloadSchedule):
            next_open = workload["next_open"]
            updated_at = workload["updated_at"]
            if not isinstance(next_open, datetime) or not isinstance(updated_at, datetime):
                raise HTTPException(status_code=500, detail="Invalid workload state.")
            workload = {
                **workload,
                "schedule": schedule.model_dump(mode="json"),
                "next_open": next_open.isoformat(),
                "updated_at": updated_at.isoformat(),
            }
    return JSONResponse(
        {
            "workload": workload,
            "deployment_denied": get_settings().external_agents_deny,
            "profiles": agent_ops.profiles(db),
            "tasks": [agent_ops.task_json(task) for task in agent_ops.recent_tasks(db)],
        }
    )


@router.post("/api/agents/profiles")
async def create_profile_api(request: Request, auth: AdminOperator, db: DbSession) -> JSONResponse:
    try:
        draft = ProfileDraft.model_validate(await request.json())
        profile = agent_ops.configure_profile(db, auth.operator, draft)
    except (ExternalAgentError, ValidationError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    safe = next(row for row in agent_ops.profiles(db) if row["slug"] == profile.slug)
    return JSONResponse(safe, status_code=201)


@router.put("/api/agents/profiles/{slug}")
async def update_profile_api(
    slug: str, request: Request, auth: AdminOperator, db: DbSession
) -> JSONResponse:
    try:
        values = await request.json()
        update = ProfileUpdate.model_validate(values)
        draft = ProfileDraft.model_validate(
            {key: value for key, value in update.model_dump().items() if key != "expected_revision"}
            | {"slug": slug}
        )
        profile = agent_ops.configure_profile(
            db, auth.operator, draft, expected_revision=update.expected_revision
        )
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except (ExternalAgentError, ValidationError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return JSONResponse(next(row for row in agent_ops.profiles(db) if row["slug"] == profile.slug))


@router.put("/api/agents/profiles/{slug}/enabled")
async def set_profile_enabled_api(
    slug: str, request: Request, auth: AdminOperator, db: DbSession
) -> JSONResponse:
    try:
        change = EnabledChange.model_validate(await request.json())
        profile = agent_ops.set_profile_enabled(
            db,
            auth.operator,
            slug,
            enabled=change.enabled,
            expected_revision=change.expected_revision,
        )
    except RevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except (ExternalAgentError, ValidationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return JSONResponse(next(row for row in agent_ops.profiles(db) if row["slug"] == profile.slug))


@router.post("/api/agents/profiles/{slug}/health")
def test_profile_api(slug: str, auth: AdminOperator, db: DbSession) -> JSONResponse:
    try:
        profile, health = agent_ops.test_profile(db, auth.operator, slug)
    except ExternalAgentError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    safe = next(row for row in agent_ops.profiles(db) if row["slug"] == profile.slug)
    return JSONResponse({"profile": safe, "health": health.model_dump(mode="json")})


@router.post("/api/agents/{slug}/tasks")
async def submit_task_api(
    slug: str, request: Request, auth: AdminOperator, db: DbSession
) -> JSONResponse:
    try:
        body = TaskRequest.model_validate(await request.json())
        task = agent_ops.submit_task(db, auth.operator, slug, body)
    except ExternalAgentError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except (ValidationError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return JSONResponse(agent_ops.task_json(task), status_code=202)


@router.delete("/api/agents/tasks/{task_id}")
def cancel_task_api(task_id: int, auth: AdminOperator, db: DbSession) -> JSONResponse:
    try:
        task = agent_ops.cancel_task(db, auth.operator, task_id)
    except ExternalAgentError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return JSONResponse(agent_ops.task_json(task))
