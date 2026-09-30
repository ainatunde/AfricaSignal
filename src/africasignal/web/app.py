"""FastAPI application."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from africasignal.config import get_settings
from africasignal.db import database_is_up
from africasignal.web.csrf import AdminOriginGuard
from africasignal.web.render import STATIC_DIR
from africasignal.web.routes import admin, api_v1, public


def create_app() -> FastAPI:
    get_settings()  # fail closed at startup when required settings are missing
    app = FastAPI(title="AfricaSignal", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(AdminOriginGuard)

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        db = database_is_up()
        return {"ok": db, "db": db}

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    # One line per router; streams that add pages append theirs here.
    for router in (admin.router, public.router, api_v1.router):
        app.include_router(router)
    public.register_error_pages(app)
    return app
