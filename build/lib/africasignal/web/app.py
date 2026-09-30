"""FastAPI application."""

from __future__ import annotations

import html
import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from africasignal.config import get_settings
from africasignal.db import database_is_up
from africasignal.net.netutil import USER_AGENT
from africasignal.web.csrf import AdminOriginGuard
from africasignal.web.routes import admin


def create_app() -> FastAPI:
    get_settings()  # fail closed at startup when required settings are missing
    app = FastAPI(title="AfricaSignal", docs_url=None, redoc_url=None)
    app.add_middleware(AdminOriginGuard)
    app.include_router(admin.router)

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        db = database_is_up()
        return {"ok": db, "db": db}

    @app.get("/about/bot", response_class=HTMLResponse)
    def about_bot() -> str:
        contact = os.environ.get("BOT_CONTACT_EMAIL", "")
        contact_line = (
            f'<p>Contact: <a href="mailto:{html.escape(contact)}">{html.escape(contact)}</a></p>'
            if contact
            else ""
        )
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AfricaSignal crawler</title></head>
<body>
<h1>AfricaSignal crawler</h1>
<p>AfricaSignalBot collects public prices and policy announcements in Nigeria so we can show
people what has changed. It identifies itself as <code>{html.escape(USER_AGENT)}</code>.</p>
<ul>
<li>It obeys robots.txt and asks for at most a few pages per minute from any one site.</li>
<li>To stop it visiting your site, disallow <code>AfricaSignalBot</code> in robots.txt.</li>
</ul>
{contact_line}
</body></html>"""

    return app
