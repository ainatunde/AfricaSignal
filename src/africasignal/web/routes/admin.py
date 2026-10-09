"""Operator console (spec B11.5, AS-022): sign-in, the Sources page with permission approval, and
the audit log. Other console pages (jobs, assessments, feedback, costs, kill switch, metrics) are
added by their own tickets.

Roles: ``admin`` can approve or publish permissions, resume sources and pause them; ``editor``
can view everything and pause a source (the safe direction). Every change is audited in the same
transaction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal import audit, operators, settings_store
from africasignal.models import AuditLog, Operator, Setting, Source, SourcePermission
from africasignal.publish import versions
from africasignal.sources import console
from africasignal.sources.console import ConsoleError, PermissionInput
from africasignal.web.client_address import client_address
from africasignal.web.deps import (
    ABSOLUTE_SECONDS,
    COOKIE_NAME,
    AdminOperator,
    Authenticated,
    CurrentOperator,
    DbSession,
    cookie_secure,
    decode_session,
    new_session_cookie,
    refreshed_cookie,
)

router = APIRouter(prefix="/admin")

_TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES))


def _when(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if value else "never"


templates.env.filters["when"] = _when

NOTICES = {
    "approved": "Permission approved. The source can now be fetched.",
    "new_version": "New permission version published and approved.",
    "paused": "Source paused.",
    "resumed": "Source resumed.",
    "signed_out": "You have signed out.",
    "saved": "Settings saved.",
    "unchanged": "Nothing to change.",
    "owner_saved": "Owner saved.",
    "suspended": "Publication suspended. Nothing new is published and no notification goes out.",
    "resumed_publication": "Publication resumed.",
}

# Not ``no-referrer``: with that policy browsers send ``Origin: null`` on a same-origin form POST,
# which the origin guard (``csrf.py``) refuses, so nobody could sign in. ``same-origin`` still
# sends nothing to other sites.
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
}


def _finish(response: Response, auth: Authenticated | None = None) -> Response:
    """Add the console's security headers and, for a signed-in operator, renew the cookie."""
    for name, value in _SECURITY_HEADERS.items():
        response.headers[name] = value
    if auth is not None:
        response.set_cookie(
            COOKIE_NAME,
            refreshed_cookie(auth.session),
            max_age=ABSOLUTE_SECONDS,
            path="/admin",
            httponly=True,
            samesite="strict",
            secure=cookie_secure(),
        )
    return response


def _page(
    request: Request,
    name: str,
    auth: Authenticated | None,
    status_code: int = 200,
    **context: Any,
) -> Response:
    context.setdefault("operator", auth.operator if auth else None)
    context.setdefault("notice", NOTICES.get(request.query_params.get("notice", "")))
    context.setdefault("error", None)
    return _finish(
        templates.TemplateResponse(request, name, context, status_code=status_code), auth
    )


def error_page(request: Request, status_code: int, message: str) -> Response:
    """A console-styled page for an error raised by a console route (a 403 for an editor who
    typed an admin address, a 404). It needs no session lookup, so it shows no menu."""
    return _page(request, "admin/error.html", None, status_code, message=message, error=None)


def _redirect(url: str, auth: Authenticated | None = None) -> Response:
    return _finish(RedirectResponse(url, status_code=303), auth)


# --- sign in and out ----------------------------------------------------------------------------


@router.get("")
def index(auth: CurrentOperator) -> Response:
    return _redirect("/admin/sources", auth)


@router.get("/login")
def login_form(request: Request) -> Response:
    return _page(request, "admin/login.html", None)


@router.post("/login")
def login(
    request: Request,
    db: DbSession,
    email: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    code: Annotated[str, Form()] = "",
) -> Response:
    if len(email) > 320 or len(password) > 1024 or len(code) > 16:
        return _page(request, "admin/login.html", None, 401, error=_FAILED)
    result = operators.authenticate(
        db, email, password, code, client=operators.client_key(client_address(request))
    )
    if result.throttled:
        return _page(
            request,
            "admin/login.html",
            None,
            429,
            error="Too many failed attempts. Try again in 15 minutes.",
        )
    operator = result.operator
    if operator is None:
        return _page(request, "admin/login.html", None, 401, error=_FAILED)
    audit.record(db, operator, "operator.sign_in", "operator", operator.id)
    response = RedirectResponse("/admin/sources", status_code=303)
    response.set_cookie(
        COOKIE_NAME,
        new_session_cookie(operator),
        max_age=ABSOLUTE_SECONDS,
        path="/admin",
        httponly=True,
        samesite="strict",
        secure=cookie_secure(),
    )
    return _finish(response)


_FAILED = "Sign-in failed. Check your email, password and code."


@router.post("/logout")
def logout(request: Request, db: DbSession) -> Response:
    """Sign out. The epoch is raised, so every copy of this operator's cookie stops working, not
    just the one in this browser (a stolen cookie is ended too)."""
    data = decode_session(request.cookies.get(COOKIE_NAME))
    operator = db.get(Operator, data.operator_id) if data else None
    if operator is not None and data is not None and (operator.session_epoch or 0) == data.epoch:
        operators.revoke_sessions(db, operator)
        audit.record(db, operator, "operator.sign_out", "operator", operator.id)
    response = RedirectResponse("/admin/login?notice=signed_out", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/admin")
    return _finish(response)


# --- sources ------------------------------------------------------------------------------------


def _permission_state(source: Source, permissions: list[SourcePermission]) -> dict[str, Any]:
    """What the console shows about a source's permission; ``permissions`` is newest first."""
    in_force = next((p for p in permissions if p.approved_at is not None), None)
    pending = permissions[0] if permissions and permissions[0].approved_at is None else None
    if in_force is None:
        label = "Awaiting approval" if pending else "No permission recorded"
        can_fetch = False
    elif not in_force.may_collect:
        label, can_fetch = f"Collection not allowed (v{in_force.version})", False
    else:
        label, can_fetch = f"Approved (v{in_force.version})", source.active
    return {
        "label": label,
        "in_force": in_force,
        "pending": pending,
        "can_fetch": can_fetch,
    }


def _permissions_by_source(db: Session) -> dict[int, list[SourcePermission]]:
    grouped: dict[int, list[SourcePermission]] = {}
    for permission in db.scalars(
        select(SourcePermission).order_by(
            SourcePermission.source_id, SourcePermission.version.desc()
        )
    ):
        grouped.setdefault(permission.source_id, []).append(permission)
    return grouped


@router.get("/sources")
def sources_list(request: Request, auth: CurrentOperator, db: DbSession) -> Response:
    grouped = _permissions_by_source(db)
    rows = [
        {"source": s, "state": _permission_state(s, grouped.get(s.id, []))}
        for s in db.scalars(select(Source).order_by(Source.slug))
    ]
    return _page(request, "admin/sources.html", auth, rows=rows)


def _source_page(
    request: Request,
    db: Session,
    auth: Authenticated,
    source_id: int,
    status_code: int = 200,
    error: str | None = None,
    form: dict[str, Any] | None = None,
    pacing_form: dict[str, Any] | None = None,
) -> Response:
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="No such source")
    permissions = _permissions_by_source(db).get(source.id, [])
    approvers = {
        o.id: o.email
        for o in db.scalars(
            select(Operator).where(
                Operator.id.in_(
                    {p.approved_by_operator_id for p in permissions if p.approved_by_operator_id}
                )
            )
        )
    }
    newest = permissions[0] if permissions else None
    defaults = form or {
        "may_collect": newest.may_collect if newest else False,
        "may_store_full_text": newest.may_store_full_text if newest else False,
        "max_quote_chars": newest.max_quote_chars if newest else "",
        "may_republish_numbers": newest.may_republish_numbers if newest else False,
        "link_required": newest.link_required if newest else True,
        "retention_days": newest.retention_days if newest else "",
        "terms_url": newest.terms_url if newest else "",
        "rights_basis": newest.rights_basis if newest else "",
    }
    pacing = pacing_form or {
        "schedule_minutes": source.schedule_minutes,
        "max_requests_per_hour": source.max_requests_per_hour,
    }
    return _page(
        request,
        "admin/source.html",
        auth,
        status_code,
        source=source,
        permissions=permissions,
        approvers=approvers,
        state=_permission_state(source, permissions),
        defaults=defaults,
        pacing=pacing,
        is_admin=auth.operator.role == "admin",
        error=error,
    )


@router.get("/sources/{source_id}")
def source_detail(
    source_id: int,
    request: Request,
    auth: CurrentOperator,
    db: DbSession,
) -> Response:
    return _source_page(request, db, auth, source_id)


@router.post("/sources/{source_id}/permissions/{version}/approve")
def approve_permission(
    source_id: int,
    version: int,
    request: Request,
    auth: AdminOperator,
    db: DbSession,
    terms_reviewed: Annotated[str | None, Form()] = None,
) -> Response:
    try:
        console.approve_permission(
            db, auth.operator, source_id, version, terms_reviewed=terms_reviewed is not None
        )
    except ConsoleError as exc:
        return _source_page(request, db, auth, source_id, 400, error=str(exc))
    return _redirect(f"/admin/sources/{source_id}?notice=approved", auth)


def _optional_int(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    if not value.isascii() or not value.isdigit():
        raise ConsoleError("quote length and retention must be whole numbers")
    return int(value)


@router.post("/sources/{source_id}/permissions")
def new_permission_version(
    source_id: int,
    request: Request,
    auth: AdminOperator,
    db: DbSession,
    may_collect: Annotated[str | None, Form()] = None,
    may_store_full_text: Annotated[str | None, Form()] = None,
    may_republish_numbers: Annotated[str | None, Form()] = None,
    link_required: Annotated[str | None, Form()] = None,
    max_quote_chars: Annotated[str, Form()] = "",
    retention_days: Annotated[str, Form()] = "",
    terms_url: Annotated[str, Form()] = "",
    rights_basis: Annotated[str, Form()] = "",
    terms_reviewed: Annotated[str | None, Form()] = None,
) -> Response:
    flags = {
        "may_collect": may_collect is not None,
        "may_store_full_text": may_store_full_text is not None,
        "may_republish_numbers": may_republish_numbers is not None,
        "link_required": link_required is not None,
    }
    form: dict[str, Any] = {
        **flags,
        "max_quote_chars": max_quote_chars,
        "retention_days": retention_days,
        "terms_url": terms_url,
        "rights_basis": rights_basis,
    }
    try:
        data = PermissionInput(
            may_collect=flags["may_collect"],
            may_store_full_text=flags["may_store_full_text"],
            max_quote_chars=_optional_int(max_quote_chars),
            may_republish_numbers=flags["may_republish_numbers"],
            link_required=flags["link_required"],
            retention_days=_optional_int(retention_days),
            terms_url=terms_url.strip() or None,
            rights_basis=rights_basis.strip() or None,
        )
        console.publish_permission_version(
            db, auth.operator, source_id, data, terms_reviewed=terms_reviewed is not None
        )
    except ConsoleError as exc:
        return _source_page(request, db, auth, source_id, 400, error=str(exc), form=form)
    return _redirect(f"/admin/sources/{source_id}?notice=new_version", auth)


@router.post("/sources/{source_id}/pacing")
async def set_source_pacing(
    source_id: int,
    request: Request,
    auth: AdminOperator,
    db: DbSession,
) -> Response:
    form = await request.form()
    values = {
        key: value
        for key, value in form.items()
        if isinstance(value, str) and key in ("schedule_minutes", "max_requests_per_hour")
    }
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="No such source")
    try:

        def whole_number(key: str) -> int:
            value = values.get(key, "")
            if not value.isascii() or not value.isdigit():
                raise ConsoleError(f"{key.replace('_', ' ')} must be a whole number")
            return int(value)

        interval = None if source.adapter == "gdelt" else whole_number("schedule_minutes")
        max_requests = whole_number("max_requests_per_hour")
        console.set_source_pacing(
            db,
            auth.operator,
            source_id,
            schedule_minutes=interval,
            max_requests_per_hour=max_requests,
        )
    except ConsoleError as exc:
        return _source_page(request, db, auth, source_id, 400, error=str(exc), pacing_form=values)
    return _redirect(f"/admin/sources/{source_id}?notice=pacing_saved", auth)


@router.post("/sources/{source_id}/owner")
def set_owner(
    source_id: int,
    request: Request,
    auth: AdminOperator,
    db: DbSession,
    owner: Annotated[str, Form()] = "",
) -> Response:
    try:
        console.set_source_owner(db, auth.operator, source_id, owner)
    except ConsoleError as exc:
        return _source_page(request, db, auth, source_id, 400, error=str(exc))
    return _redirect(f"/admin/sources/{source_id}?notice=owner_saved", auth)


@router.post("/sources/{source_id}/pause")
def pause_source(
    source_id: int,
    request: Request,
    auth: CurrentOperator,
    db: DbSession,
) -> Response:
    try:
        console.set_source_active(db, auth.operator, source_id, False)
    except ConsoleError as exc:
        return _source_page(request, db, auth, source_id, 400, error=str(exc))
    return _redirect(f"/admin/sources/{source_id}?notice=paused", auth)


@router.post("/sources/{source_id}/resume")
def resume_source(
    source_id: int,
    request: Request,
    auth: AdminOperator,
    db: DbSession,
) -> Response:
    try:
        console.set_source_active(db, auth.operator, source_id, True)
    except ConsoleError as exc:
        return _source_page(request, db, auth, source_id, 400, error=str(exc))
    return _redirect(f"/admin/sources/{source_id}?notice=resumed", auth)


# --- audit log ----------------------------------------------------------------------------------


@router.get("/audit")
def audit_log(request: Request, auth: CurrentOperator, db: DbSession) -> Response:
    rows = db.execute(
        select(AuditLog, Operator.email)
        .outerjoin(Operator, Operator.id == AuditLog.operator_id)  # system rows have no operator
        .order_by(AuditLog.id.desc())
        .limit(200)
    ).all()
    return _page(request, "admin/audit.html", auth, rows=rows)


# --- settings -----------------------------------------------------------------------------------

SOURCE_LABELS = {
    "console": "Set here",
    "environment": "From environment variable",
    "default": "Default",
    "unset": "Not set",
    "unreadable": "Saved value unreadable: enter it again",
}


def _settings_group(
    db: Session,
    group_id: str,
    submitted: dict[str, str] | None = None,
    field_error: str | None = None,
    query: str = "",
) -> dict[str, Any]:
    title = dict(settings_store.GROUPS).get(group_id)
    if title is None:
        raise ValueError("unknown settings group")
    fields = []
    for key in settings_store.group_keys(group_id):
        defn = settings_store.definition(key)
        resolved = settings_store.resolve(db, key)
        value = "" if defn.secret else (resolved.value or "")
        if submitted is not None and key in submitted and not defn.secret:
            value = submitted[key]
        searchable = " ".join((defn.key, defn.label, defn.group, defn.help)).casefold()
        if (
            query
            and query.casefold() not in searchable
            and query.casefold() not in title.casefold()
        ):
            continue
        fields.append(
            {
                "defn": defn,
                "value": value,
                "options": settings_store.options(key),
                "source": SOURCE_LABELS[resolved.source],
                "source_id": resolved.source,
                "configured": resolved.value is not None,
                "has_console_value": resolved.source == "console",
                "current_value": "" if defn.secret else (resolved.value or ""),
                "submitted_secret": (
                    bool(submitted and submitted.get(key)) if defn.secret else False
                ),
                "field_error": field_error == key,
            }
        )
    return {
        "id": group_id,
        "title": title,
        "fields": fields,
        "revision": settings_store.group_revision(db, group_id),
    }


def _settings_page(
    request: Request,
    db: Session,
    auth: Authenticated,
    status_code: int = 200,
    error: str | None = None,
    submitted: dict[str, str] | None = None,
    conflict: bool = False,
    field_error: str | None = None,
    query: str = "",
) -> Response:
    groups = [
        _settings_group(db, group_id, submitted, field_error, query)
        for group_id, _title in settings_store.GROUPS
    ]
    groups = [group for group in groups if group["fields"]]
    return _page(
        request,
        "admin/settings.html",
        auth,
        status_code,
        groups=groups,
        missing=settings_store.missing_expected(db),
        error=error,
        conflict=conflict,
        query=query,
    )


@router.get("/settings")
def settings_view(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    return _settings_page(request, db, auth, query=request.query_params.get("q", "").strip())


@router.post("/settings/{group}")
async def settings_save(
    group: str, request: Request, auth: AdminOperator, db: DbSession
) -> Response:
    if group not in dict(settings_store.GROUPS):
        raise HTTPException(status_code=404, detail="No such settings group")
    form = await request.form()
    submitted = {k: v for k, v in form.items() if isinstance(v, str)}
    query = submitted.get("query", "").strip()
    try:
        expected_revision = (
            int(submitted["expected_revision"])
            if "expected_revision" in submitted
            else settings_store.group_revision(db, group)
        )
    except ValueError:
        return _settings_page(
            request, db, auth, 400, error="Reload this settings page before saving.", query=query
        )
    changes: dict[str, str | None] = {}
    for key in settings_store.group_keys(group):
        if key not in submitted and f"clear__{key}" not in submitted:
            continue
        defn = settings_store.definition(key)
        value = submitted.get(key, "").strip()
        if submitted.get(f"clear__{key}") == "on":
            changes[key] = None
        elif defn.secret:
            if value:  # a blank secret field means "leave it as it is"
                changes[key] = value
        elif value == "":
            changes[key] = None
        elif value != (settings_store.resolve(db, key).value or ""):
            changes[key] = value  # the field is pre-filled, so only a real edit counts
    try:
        changed, _revision = settings_store.apply_group_changes(
            db, auth.operator, group, expected_revision, changes
        )
    except settings_store.SettingsRevisionConflict as exc:
        return _settings_page(
            request,
            db,
            auth,
            409,
            error=str(exc),
            submitted=submitted,
            conflict=True,
            query=query,
        )
    except settings_store.SettingError as exc:
        return _settings_page(
            request,
            db,
            auth,
            400,
            error=str(exc),
            submitted=submitted,
            field_error=exc.key,
            query=query,
        )
    target = "/admin/settings"
    if query:
        target += "?q=" + quote(query, safe="") + "&notice="
        target += "saved" if changed else "unchanged"
    else:
        target += "?notice=" + ("saved" if changed else "unchanged")
    return _redirect(target, auth)


# --- publication kill switch --------------------------------------------------------------------


@router.get("/publication")
def publication_view(request: Request, auth: AdminOperator, db: DbSession) -> Response:
    return _page(
        request,
        "admin/publication.html",
        auth,
        suspended=versions.publication_suspended(db),
        since=db.scalar(select(Setting.updated_at).where(Setting.key == versions.SUSPENDED_KEY)),
    )


@router.post("/publication/{action}")
def publication_switch(action: str, auth: AdminOperator, db: DbSession) -> Response:
    """Suspend or resume publication (plan B12, rule R1). Admin only; audited in the same
    transaction. Repeating the current state changes nothing and writes no audit row."""
    if action not in ("suspend", "resume"):
        raise HTTPException(status_code=404, detail="No such action")
    suspend = action == "suspend"
    if versions.publication_suspended(db) == suspend:
        return _redirect("/admin/publication?notice=unchanged", auth)
    versions.set_publication_suspended(db, suspend, datetime.now(UTC))
    audit.record(
        db,
        auth.operator,
        "publication.suspend" if suspend else "publication.resume",
        "setting:publication_suspended",
        None,
        before={"publication_suspended": not suspend},
        after={"publication_suspended": suspend},
    )
    return _redirect(
        f"/admin/publication?notice={'suspended' if suspend else 'resumed_publication'}", auth
    )
