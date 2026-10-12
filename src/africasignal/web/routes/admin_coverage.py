"""Read-only configuration ownership manifest for operators."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response

from africasignal.operations.configuration_coverage import manifest
from africasignal.web.deps import AdminOperator, DbSession
from africasignal.web.routes.admin import _page

router = APIRouter(prefix="/admin")


@router.get("/configuration-coverage")
def configuration_coverage_view(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    data = manifest(db)
    return _page(
        request,
        "admin/configuration_coverage.html",
        auth,
        settings=data["settings"],
        workloads=data["workloads"],
        controls=data["business_controls"],
        jobs=data["job_kinds"],
        planned_jobs=data["planned_job_kinds"],
        deployment=data["deployment_owned"],
        gaps=data["coverage_gaps"],
        generated_at=data["generated_at"],
    )


@router.get("/api/configuration/coverage")
def configuration_coverage_api(auth: AdminOperator, db: DbSession) -> JSONResponse:
    return JSONResponse(jsonable_encoder(manifest(db)))
