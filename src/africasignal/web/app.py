"""FastAPI application."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from africasignal.config import get_settings
from africasignal.db import database_is_up, session_scope
from africasignal.publish.recovery import require_recovery_complete
from africasignal.web.analytics import AnalyticsMiddleware
from africasignal.web.csrf import AdminOriginGuard
from africasignal.web.public_csrf import PublicOriginGuard
from africasignal.web.render import STATIC_DIR
from africasignal.web.routes import (
    account,
    admin,
    admin_feedback,
    admin_ops,
    api_v1,
    feedback,
    legal,
    public,
)
from africasignal.web.security_headers import SecurityHeadersMiddleware


def create_app() -> FastAPI:
    get_settings()  # fail closed at startup when required settings are missing
    require_recovery_complete()
    app = FastAPI(title="AfricaSignal", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.event_session = session_scope  # where page views are written; tests replace it
    # Middleware added last runs first: origin checks refuse a request before anything else sees it.
    app.add_middleware(AnalyticsMiddleware)
    app.add_middleware(PublicOriginGuard)
    app.add_middleware(AdminOriginGuard)
    app.add_middleware(SecurityHeadersMiddleware)  # outermost: every response, errors included

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        db = database_is_up()
        return JSONResponse({"ok": db, "db": db}, status_code=200 if db else 503)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    # One line per router; streams that add pages append theirs here.
    for router in (
        admin.router,
        admin_feedback.router,
        admin_ops.router,
        public.router,
        legal.router,
        account.router,
        feedback.router,
        api_v1.router,
    ):
        app.include_router(router)
    public.register_error_pages(app)
    return app
