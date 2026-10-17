"""Bounded external checks of the pages readers use most often."""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlsplit

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from africasignal import backup_alerts, settings_store
from africasignal.config import get_settings
from africasignal.models import GuardianCheckRun, Setting
from africasignal.publish.factfmt import fact_value_text
from africasignal.web import queries

log = logging.getLogger("africasignal.guardian.public_pages")

CHECK_CODE = "public_page"
ROUTES = {
    "home": ("/", "what is changing around you"),
    "explore": ("/explore", "explore"),
    "coverage": ("/coverage", "coverage"),
}
JOURNEY_SUBJECTS = ("selected_explore", "situation", "history")
CHECK_SUBJECTS = (*ROUTES, *JOURNEY_SUBJECTS)
MAX_BODY_BYTES = 512 * 1024
TIMEOUT_SECONDS = 5.0
RETENTION = timedelta(days=30)


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.main_depth = 0
        self.h1_depth = 0
        self.inert_depth = 0
        self.main_seen = False
        self.main_text: list[str] = []
        self.headings: list[str] = []
        self.links: list[str] = []
        self.guardian_attributes: dict[str, str] = {}
        self.fact_rows: list[list[str]] = []
        self._fact_row: list[str] | None = None
        self._fact_cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "template", "noscript"}:
            self.inert_depth += 1
        if self.inert_depth:
            return
        for key, value in attrs:
            if key.startswith("data-guardian-") and value is not None and len(value) <= 160:
                name = key.removeprefix("data-guardian-").replace("-", "_")
                self.guardian_attributes[name] = value
        if tag == "main":
            self.main_seen = True
            self.main_depth += 1
        elif tag == "h1" and self.main_depth:
            self.h1_depth += 1
            self.headings.append("")
        elif tag == "a" and self.main_depth:
            href = next((value for key, value in attrs if key == "href"), None)
            if href and len(href) <= 512:
                try:
                    parts = urlsplit(href)
                except ValueError:
                    return
                if not parts.scheme and not parts.netloc and parts.path.startswith("/"):
                    self.links.append(href)
        elif tag == "tr" and dict(attrs).get("data-guardian-fact-row") == "true":
            self._fact_row = []
            self._fact_cell = None
        elif tag in {"th", "td"} and self._fact_row is not None:
            self._fact_cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "template", "noscript"} and self.inert_depth:
            self.inert_depth -= 1
        if self.inert_depth:
            return
        if tag in {"th", "td"} and self._fact_row is not None and self._fact_cell is not None:
            self._fact_row.append(" ".join(" ".join(self._fact_cell).split()))
            self._fact_cell = None
        elif tag == "tr" and self._fact_row is not None:
            self.fact_rows.append(self._fact_row)
            self._fact_row = None
        if tag == "main" and self.main_depth:
            self.main_depth -= 1
        elif tag == "h1" and self.h1_depth:
            self.h1_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.inert_depth:
            return
        text = " ".join(data.split())
        if not text:
            return
        if self.main_depth:
            self.main_text.append(text)
        if self._fact_cell is not None:
            self._fact_cell.append(text)
        if self.h1_depth and self.headings:
            self.headings[-1] += f" {text}"


def _public_origin(raw: str) -> str:
    parts = urlsplit(raw)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            "Public address must be an origin without a path, login, query or fragment"
        )
    if get_settings().env != "development":
        try:
            address = ipaddress.ip_address(parts.hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("Public address resolves to a non-public IP literal")
        if parts.hostname.casefold() in {"localhost", "localhost.localdomain"}:
            raise ValueError("Public address is still set to localhost")
    return f"{parts.scheme}://{parts.netloc}"


def _read_page(
    client: httpx.Client, origin: str, route: str
) -> tuple[str, dict[str, object], list[str]]:
    url = f"{origin}{route}"
    with client.stream(
        "GET", url, headers={"Accept": "text/html", "User-Agent": "AfricaSignal-Guardian/1.0"}
    ) as response:
        content_type = response.headers.get("content-type", "").split(";", 1)[0].casefold()
        observed: dict[str, object] = {
            "route": route,
            "status_code": response.status_code,
            "content_type": content_type,
        }
        if response.status_code != 200:
            raise ValueError(f"HTTP {response.status_code}")
        if content_type != "text/html":
            raise ValueError("Response was not HTML")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                raise ValueError("Page exceeded the bounded response size")
            chunks.append(chunk)
    parser = _PageParser()
    parser.feed(b"".join(chunks).decode("utf-8", errors="replace"))
    heading = " ".join(parser.headings[0].split()) if parser.headings else ""
    visible_length = len(" ".join(parser.main_text))
    observed.update(
        {
            "main_present": parser.main_seen,
            "h1": heading[:160],
            "main_text_chars": visible_length,
            **{f"guardian_{key}": value for key, value in parser.guardian_attributes.items()},
            "guardian_fact_rows": parser.fact_rows,
        }
    )
    if not parser.main_seen or not parser.main_text or not heading or visible_length < 40:
        raise ValueError("Page has no substantial visible main content")
    return heading, observed, parser.links


def _latest_status(session: Session, subject: str) -> str | None:
    return session.scalar(
        select(GuardianCheckRun.status)
        .where(GuardianCheckRun.check_code == CHECK_CODE, GuardianCheckRun.subject_key == subject)
        .order_by(GuardianCheckRun.checked_at.desc(), GuardianCheckRun.id.desc())
        .limit(1)
    )


def _record(
    session: Session,
    subject: str,
    status: str,
    summary: str,
    observed: dict[str, object],
    started: float,
    now: datetime,
) -> GuardianCheckRun:
    row = GuardianCheckRun(
        check_code=CHECK_CODE,
        subject_key=subject,
        status=status,
        summary=summary[:500],
        observed=observed,
        duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        checked_at=now,
    )
    session.add(row)
    session.flush()
    return row


def _prune(session: Session, now: datetime) -> None:
    expired_ids = list(
        session.scalars(
            select(GuardianCheckRun.id)
            .where(GuardianCheckRun.checked_at < now - RETENTION)
            .order_by(GuardianCheckRun.checked_at)
            .limit(1000)
        )
    )
    if expired_ids:
        session.execute(delete(GuardianCheckRun).where(GuardianCheckRun.id.in_(expired_ids)))


def _findings_after_threshold(
    session: Session, rows: list[GuardianCheckRun]
) -> list[backup_alerts.Finding]:
    """Apply two-failure/two-pass thresholds and preserve incidents in between."""
    found: list[backup_alerts.Finding] = []
    for row in rows:
        alert_code = f"public_page_{row.subject_key}"
        latest = _latest_status(session, row.subject_key)
        previous = session.scalar(
            select(GuardianCheckRun.status)
            .where(
                GuardianCheckRun.check_code == CHECK_CODE,
                GuardianCheckRun.subject_key == row.subject_key,
                GuardianCheckRun.id != row.id,
            )
            .order_by(GuardianCheckRun.checked_at.desc(), GuardianCheckRun.id.desc())
            .limit(1)
        )
        setting = session.scalar(
            select(Setting.value).where(Setting.key == backup_alerts.ALERT_PREFIX + alert_code)
        )
        alert = setting if isinstance(setting, dict) else {}
        is_open = alert.get("state") == "open"
        if latest == previous == "fail":
            summary = (
                f"The public {row.subject_key} page failed two consecutive checks: {row.summary}"
            )
            found.append(
                backup_alerts.Finding(
                    alert_code,
                    summary,
                    dict(row.observed),
                )
            )
        elif latest == previous == "pass":
            continue
        elif is_open:
            found.append(
                backup_alerts.Finding(
                    alert_code,
                    str(alert.get("summary", "Visitor check remains under review")),
                    dict(alert.get("detail", {})),
                )
            )
    return found


def check_public_pages(session: Session, now: datetime | None = None) -> list[GuardianCheckRun]:
    """Check public pages and follow one bounded, read-only visitor journey."""
    now = now or datetime.now(UTC)
    check_origin = get_settings().guardian_public_base_url.strip() or settings_store.get(
        session, "public_base_url"
    )
    try:
        origin = _public_origin(check_origin or "")
    except ValueError as exc:
        unknown_rows = [
            _record(session, name, "unknown", str(exc), {}, time.monotonic(), now)
            for name in CHECK_SUBJECTS
        ]
        _prune(session, now)
        return unknown_rows

    rows: list[GuardianCheckRun] = []
    timeout = httpx.Timeout(TIMEOUT_SECONDS)
    try:
        with httpx.Client(
            timeout=timeout, follow_redirects=False, trust_env=False, verify=True
        ) as client:

            def probe(name: str, route: str, expected_heading: str | None = None) -> list[str]:
                # Each request starts without visitor state left by a previous response.
                client.cookies.clear()
                started = time.monotonic()
                observed: dict[str, object] = {"route": route}
                try:
                    heading, observed, links = _read_page(client, origin, route)
                    if expected_heading and expected_heading not in heading.casefold():
                        raise ValueError("Primary page heading did not match the expected route")
                    page_path = urlsplit(route).path
                    if name in {"situation", "history"}:
                        match = re.fullmatch(r"/s/([a-z0-9_-]{1,120})(?:/history)?", page_path)
                        current = (
                            queries.current_situation(session, match.group(1)) if match else None
                        )
                        if current is None:
                            raise ValueError(
                                "No eligible current assessment matched the public page"
                            )
                        if name == "situation":
                            if observed.get("guardian_version_id") != str(current.version.id):
                                raise ValueError(
                                    "Public assessment version did not match the current version"
                                )
                            if observed.get("guardian_status") != current.effective_status:
                                raise ValueError(
                                    "Public assessment status did not match the current status"
                                )
                            expected_rows = []
                            if current.effective_status != "withdrawn":
                                expected_rows = [
                                    [
                                        " ".join(str(fact.get("label") or "").split()),
                                        " ".join(fact_value_text(fact).split()),
                                        " ".join(str(fact.get("period") or "").split()),
                                        " ".join(str(fact.get("source_label") or "").split()),
                                    ]
                                    for fact in current.version.facts
                                ]
                            if observed.get("guardian_fact_rows") != expected_rows:
                                raise ValueError(
                                    "Visible assessment facts did not match the current version"
                                )
                        elif observed.get("guardian_current_version_id") != str(current.version.id):
                            raise ValueError(
                                "History page current version did not match the current version"
                            )
                    status, summary = "pass", f"{route} returned its expected visible page"
                except (httpx.HTTPError, ValueError) as exc:
                    status, summary = "fail", str(exc)[:300]
                    log.warning("public page check failed for %s (%s)", route, type(exc).__name__)
                    links = []
                rows.append(_record(session, name, status, summary, observed, started, now))
                return links

            # Alternate home and coverage so five bounded requests can include a
            # real Explore → situation → history path when eligible links exist.
            sentinel = "home" if (now.minute // 5) % 2 == 0 else "coverage"
            sentinel_route, sentinel_heading = ROUTES[sentinel]
            explore_links = probe(sentinel, sentinel_route, sentinel_heading)
            explore_route = ROUTES["explore"][0]
            home_explore = next(
                (
                    link
                    for link in explore_links
                    if urlsplit(link).path == explore_route
                    and not parse_qs(urlsplit(link).query).get("item")
                ),
                None,
            )
            explore_route = home_explore or explore_route
            explore_links = probe("explore", explore_route, ROUTES["explore"][1])

            selected_link = next(
                (
                    link
                    for link in explore_links
                    if urlsplit(link).path == "/explore"
                    and any(parse_qs(urlsplit(link).query).get("item", []))
                ),
                None,
            )
            situation_link = None
            history_link = None
            if selected_link:
                selected_parts = urlsplit(selected_link)
                selected_route = selected_parts.path
                if selected_parts.query:
                    selected_route += f"?{selected_parts.query}"
                selected_links = probe("selected_explore", selected_route)
                situation_link = next(
                    (
                        link
                        for link in selected_links
                        if re.fullmatch(r"/s/[a-z0-9_-]{1,120}", urlsplit(link).path)
                    ),
                    None,
                )
            else:
                rows.append(
                    _record(
                        session,
                        "selected_explore",
                        "unknown",
                        "No item link was available to follow from Explore",
                        {},
                        time.monotonic(),
                        now,
                    )
                )

            if situation_link:
                situation_path = urlsplit(situation_link).path
                situation_links = probe("situation", situation_path)
                history_link = next(
                    (
                        link
                        for link in situation_links
                        if urlsplit(link).path == f"{situation_path}/history"
                    ),
                    None,
                )
            else:
                rows.append(
                    _record(
                        session,
                        "situation",
                        "unknown",
                        "Explore had no current situation link to follow",
                        {},
                        time.monotonic(),
                        now,
                    )
                )

            if history_link:
                probe("history", urlsplit(history_link).path)
            else:
                rows.append(
                    _record(
                        session,
                        "history",
                        "unknown",
                        "The sampled situation had no history link",
                        {},
                        time.monotonic(),
                        now,
                    )
                )
    except httpx.HTTPError as exc:
        log.warning("could not create public page check client: %s", exc)
        checked = {row.subject_key for row in rows}
        for name in CHECK_SUBJECTS:
            if name in checked:
                continue
            rows.append(
                _record(
                    session,
                    name,
                    "fail",
                    "Public page check request failed",
                    {"error_class": type(exc).__name__},
                    time.monotonic(),
                    now,
                )
            )

    checked_codes = tuple(
        f"public_page_{name}"
        for name in dict.fromkeys(row.subject_key for row in rows if row.status in {"pass", "fail"})
    )
    backup_alerts.apply(session, now, checked_codes, _findings_after_threshold(session, rows))
    _prune(session, now)
    return rows
