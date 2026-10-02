"""Admin dashboard and private API for isolated Agent Reach discovery."""

from __future__ import annotations

from typing import Any, Literal, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import ValidationError
from sqlalchemy.orm import Session

from africasignal.jobs.policy import WorkloadSchedule
from africasignal.models import AgentReachCandidate, AgentReachTask
from africasignal.operations import agent_reach as reach_ops
from africasignal.operations.agent_reach import AgentReachError, TaskSubmission
from africasignal.operations.workloads import (
    RevisionConflict,
    WorkloadChange,
)
from africasignal.operations.workloads import (
    configure as configure_workload,
)
from africasignal.web.deps import AdminOperator, Authenticated, DbSession
from africasignal.web.routes.admin import _page, _redirect

router = APIRouter(prefix="/admin")


def _task_json(task: AgentReachTask) -> dict[str, Any]:
    return {
        "id": task.id,
        "topic": task.topic,
        "query": task.query,
        "max_results": task.max_results,
        "status": task.status,
        "created_at": task.created_at.isoformat(),
        "finished_at": task.finished_at.isoformat() if task.finished_at else None,
        "last_error": task.last_error,
        "revision": task.control_revision,
    }


def _candidate_json(candidate: AgentReachCandidate, task: AgentReachTask) -> dict[str, Any]:
    return {
        "id": candidate.id,
        "task_id": task.id,
        "topic": task.topic,
        "url": candidate.url,
        "canonical_url": candidate.canonical_url,
        "domain": candidate.domain,
        "title": candidate.title,
        "summary": candidate.summary,
        "publisher": candidate.publisher,
        "platform": candidate.platform,
        "backend": candidate.backend,
        "published_at": candidate.published_at.isoformat() if candidate.published_at else None,
        "retrieved_at": candidate.retrieved_at.isoformat(),
    }


def _view(
    request: Request,
    db: Session,
    auth: Authenticated,
    status_code: int = 200,
    error: str | None = None,
    submitted: dict[str, str] | None = None,
) -> Response:
    status = reach_ops.runner_readiness(db)
    notice = {
        "submitted": "Discovery task queued.",
        "cancelled": "Cancellation requested.",
        "candidate_accept": "Candidate capture queued through the approved source.",
        "candidate_reject": "Candidate rejected.",
        "health_ok": "Runner health passed; source rights and search quality remain unqualified.",
        "enabled": "Agent Reach enabled.",
        "disabled": "Agent Reach disabled.",
        "recovered": "Reconciliation queued; no retry until the prior outcome is checked.",
    }.get(request.query_params.get("notice", ""))
    if status.get("state") == "Migration required":
        task_rows, candidate_rows, source_rows = [], [], []
    else:
        task_rows = reach_ops.tasks(db)
        candidate_rows = reach_ops.candidates(db)
        source_rows = reach_ops.eligible_sources(db)
    return _page(
        request,
        "admin/agent_reach.html",
        auth,
        status_code,
        status=status,
        notice=notice,
        tasks=task_rows,
        candidates=candidate_rows,
        sources=source_rows,
        submitted=submitted or {},
    )


@router.get("/agent-reach")
def agent_reach_view(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    return _view(request, db, auth)


@router.post("/agent-reach/enabled")
async def agent_reach_enabled(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    form = await request.form()
    enabled = form.get("enabled") == "on"
    try:
        status = reach_ops.runner_readiness(db)
        schedule = status.get("schedule")
        if not isinstance(schedule, WorkloadSchedule):
            raise AgentReachError(
                "Agent Reach controls are unavailable; apply database migrations."
            )
        configure_workload(
            db,
            auth.operator,
            "agent_reach",
            WorkloadChange(
                enabled=enabled,
                schedule=schedule,
                expected_revision=int(str(form.get("expected_revision", ""))),
            ),
        )
    except RevisionConflict as exc:
        return _view(request, db, auth, 409, str(exc))
    except (AgentReachError, ValueError, TypeError) as exc:
        return _view(request, db, auth, 400, str(exc))
    return _redirect("/admin/agent-reach?notice=" + ("enabled" if enabled else "disabled"), auth)


@router.post("/agent-reach/health")
def agent_reach_health(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    try:
        reach_ops.test_runner(db, auth.operator)
    except AgentReachError as exc:
        return _view(request, db, auth, 502, str(exc))
    return _redirect("/admin/agent-reach?notice=health_ok", auth)


@router.post("/agent-reach/tasks")
async def agent_reach_submit(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    form = await request.form()
    submitted = {
        key: value
        for key, value in form.items()
        if isinstance(value, str) and key in ("topic", "query", "max_results")
    }
    try:
        body = TaskSubmission.model_validate(
            {
                "topic": submitted.get("topic", ""),
                "query": submitted.get("query", ""),
                "max_results": int(submitted.get("max_results", "10")),
            }
        )
        reach_ops.submit(db, auth.operator, body)
    except (AgentReachError, ValidationError, ValueError, TypeError) as exc:
        return _view(request, db, auth, 400, str(exc), submitted)
    return _redirect("/admin/agent-reach?notice=submitted", auth)


@router.post("/agent-reach/tasks/{task_id}/cancel")
def agent_reach_cancel(
    task_id: int, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    try:
        reach_ops.cancel(db, auth.operator, task_id)
    except AgentReachError as exc:
        return _view(request, db, auth, 400, str(exc))
    return _redirect("/admin/agent-reach?notice=cancelled", auth)


@router.post("/agent-reach/tasks/{task_id}/recover")
def agent_reach_recover(
    task_id: int, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    try:
        reach_ops.recover_unknown(db, auth.operator, task_id)
    except AgentReachError as exc:
        return _view(request, db, auth, 409, str(exc))
    return _redirect("/admin/agent-reach?notice=recovered", auth)


@router.post("/agent-reach/candidates/{candidate_id}/{action}")
async def agent_reach_review(
    candidate_id: int,
    action: str,
    request: Request,
    auth: AdminOperator,
    db: DbSession,
) -> Response:
    if action not in ("accept", "reject"):
        raise HTTPException(status_code=404, detail="Unknown candidate action")
    form = await request.form()
    raw_source_id = form.get("source_id")
    try:
        source_id = int(raw_source_id) if isinstance(raw_source_id, str) and raw_source_id else None
        reach_ops.review_candidate(
            db,
            auth.operator,
            candidate_id,
            action=cast(Literal["accept", "reject"], action),
            source_id=source_id,
        )
    except (AgentReachError, ValueError, TypeError) as exc:
        return _view(request, db, auth, 400, str(exc))
    return _redirect(f"/admin/agent-reach?notice=candidate_{action}", auth)


@router.get("/api/agent-reach")
def agent_reach_api(auth: AdminOperator, db: DbSession) -> JSONResponse:
    return JSONResponse(
        {
            "readiness": {
                key: (
                    value.model_dump(mode="json")
                    if hasattr(value, "model_dump")
                    else value.isoformat()
                    if hasattr(value, "isoformat")
                    else value
                )
                for key, value in reach_ops.runner_readiness(db).items()
            },
            "tasks": [_task_json(task) for task in reach_ops.tasks(db)],
            "candidates": [
                _candidate_json(candidate, task) for candidate, task in reach_ops.candidates(db)
            ],
        }
    )


@router.post("/api/agent-reach/health")
def agent_reach_api_health(auth: AdminOperator, db: DbSession) -> JSONResponse:
    try:
        result = reach_ops.test_runner(db, auth.operator)
    except AgentReachError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    return JSONResponse(result)


@router.post("/api/agent-reach/tasks")
async def agent_reach_api_submit(
    request: Request, auth: AdminOperator, db: DbSession
) -> JSONResponse:
    try:
        body = TaskSubmission.model_validate(await request.json())
        task = reach_ops.submit(db, auth.operator, body)
    except AgentReachError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except (ValidationError, ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return JSONResponse(_task_json(task), status_code=202)


@router.post("/api/agent-reach/tasks/{task_id}/cancel")
def agent_reach_api_cancel(task_id: int, auth: AdminOperator, db: DbSession) -> JSONResponse:
    try:
        task = reach_ops.cancel(db, auth.operator, task_id)
    except AgentReachError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return JSONResponse(_task_json(task))


@router.post("/api/agent-reach/candidates/{candidate_id}/{action}")
async def agent_reach_api_review(
    candidate_id: int,
    action: str,
    request: Request,
    auth: AdminOperator,
    db: DbSession,
) -> JSONResponse:
    if action not in ("accept", "reject"):
        raise HTTPException(status_code=404, detail="Unknown candidate action")
    try:
        body = await request.json()
        source_id = body.get("source_id") if isinstance(body, dict) else None
        candidate = reach_ops.review_candidate(
            db,
            auth.operator,
            candidate_id,
            action=cast(Literal["accept", "reject"], action),
            source_id=int(source_id) if source_id is not None else None,
        )
    except AgentReachError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="source_id must be an integer") from None
    return JSONResponse({"id": candidate.id, "status": candidate.status})
