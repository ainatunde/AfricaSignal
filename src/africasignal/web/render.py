"""Templates, filters and the words the pages use for evidence states."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.responses import HTMLResponse

from africasignal import settings_store
from africasignal.publish.factfmt import fact_value_text, format_change

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# (badge text, glyph). The glyph repeats the meaning for people who cannot tell the colours apart.
BADGES: dict[str, tuple[str, str]] = {
    "reported": ("Official figure", "●"),
    "corroborated": ("Confirmed by independent report", "✓"),
    "disputed": ("Disputed", "⚠"),
    "insufficient": ("Not enough evidence", "?"),
}
POLICY_BADGES = {**BADGES, "reported": ("Official statement", "●")}

EVIDENCE_STATE_HELP = {
    "reported": "The figure comes from one official source. No independent report has checked it.",
    "corroborated": "An official figure, and an independent report from a different origin says "
    "the same.",
    "disputed": "An official source says something different for the same period.",
    "insufficient": "We do not have enough recent data to say what changed.",
}

SEVERITY_WORDS = {
    "none": "",
    "low": "Small change",
    "medium": "Notable change",
    "high": "Large change",
}

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def badge(version: Any) -> dict[str, str]:
    table = POLICY_BADGES if version.template == "T2_policy_change" else BADGES
    text, glyph = table[version.evidence_state]
    return {"state": version.evidence_state, "text": text, "glyph": glyph}


def fmt_date(value: datetime | None) -> str:
    if value is None:
        return ""
    return f"{value.astimezone(UTC):%-d %B %Y}"


def fmt_datetime(value: datetime | None) -> str:
    if value is None:
        return ""
    return f"{value.astimezone(UTC):%-d %B %Y, %H:%M} UTC"


def ucfirst(value: str) -> str:
    """Capitalise the first letter only: "petrol (PMS)" becomes "Petrol (PMS)"."""
    return value[:1].upper() + value[1:]


templates.env.filters["ucfirst"] = ucfirst
templates.env.filters["fact_value"] = fact_value_text
templates.env.filters["change"] = format_change
templates.env.filters["date"] = fmt_date
templates.env.filters["datetime"] = fmt_datetime
templates.env.globals["severity_words"] = SEVERITY_WORDS


def public_base_url(db: Session) -> str:
    """The site's public address, for links that leave the page. It is read from the console
    settings (environment variable as the fallback) every time, so a change applies at once."""
    return (settings_store.get(db, "public_base_url") or "http://localhost:8000").rstrip("/")


def render(
    request: Request,
    name: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
    cache_seconds: int | None = None,
) -> HTMLResponse:
    response = templates.TemplateResponse(request, name, context, status_code=status_code)
    if cache_seconds is not None:
        response.headers["Cache-Control"] = f"public, max-age={cache_seconds}"
    else:
        response.headers["Cache-Control"] = "private, no-cache"
    return response
