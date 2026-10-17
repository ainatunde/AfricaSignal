from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from africasignal.guardian.public_pages import (
    CHECK_CODE,
    _findings_after_threshold,
    _PageParser,
    _public_origin,
)
from africasignal.models import GuardianCheckRun


def test_page_parser_reads_only_visible_main_content_and_local_links() -> None:
    parser = _PageParser()
    parser.feed(
        """<script><main><h1>fake</h1></main></script>
        <main><h1>Fuel update</h1><a href="/explore">Explore</a>
        <a href="https://outside.example/">outside</a>
        <tr data-guardian-fact-row="true"><td>Price</td><td>₦1,000</td></tr>
        <p>Visible facts appear in this section.</p></main>"""
    )

    assert parser.main_seen
    assert parser.headings == [" Fuel update"]
    assert parser.links == ["/explore"]
    assert parser.fact_rows == [["Price", "₦1,000"]]
    assert "fake" not in parser.main_text


@pytest.mark.parametrize(
    "address",
    [
        "",
        "https://example.test/path",
        "https://user:pass@example.test",
        "https://example.test?token=secret",
        "https://example.test/#fragment",
        "file:///tmp/page",
    ],
)
def test_public_origin_rejects_non_origin_addresses(address: str) -> None:
    with pytest.raises(ValueError):
        _public_origin(address)


def test_public_origin_normalizes_a_valid_origin() -> None:
    assert _public_origin("https://reader.example.test/") == "https://reader.example.test"


def test_incident_threshold_requires_two_failures_and_keeps_open_until_two_passes() -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE guardian_check_run ("
                "id INTEGER PRIMARY KEY, check_code TEXT, subject_key TEXT, status TEXT, "
                "summary TEXT, observed JSON, duration_ms INTEGER, checked_at DATETIME, "
                "created_at DATETIME)"
            )
        )
        connection.execute(
            text("CREATE TABLE setting (key TEXT PRIMARY KEY, value JSON, updated_at DATETIME)")
        )
    moment = datetime(2026, 10, 17, 12, tzinfo=UTC)

    with Session(engine) as session:
        first = GuardianCheckRun(
            id=1,
            check_code=CHECK_CODE,
            subject_key="home",
            status="fail",
            summary="HTTP 503",
            observed={"status_code": 503},
            checked_at=moment,
        )
        session.add(first)
        session.flush()
        assert _findings_after_threshold(session, [first]) == []

        second = GuardianCheckRun(
            id=2,
            check_code=CHECK_CODE,
            subject_key="home",
            status="fail",
            summary="HTTP 503",
            observed={"status_code": 503},
            checked_at=moment,
        )
        session.add(second)
        session.flush()
        findings = _findings_after_threshold(session, [second])
        assert [finding.code for finding in findings] == ["public_page_home"]

        session.execute(
            text("INSERT INTO setting (key, value) VALUES (:key, :value)"),
            {
                "key": "ops.alert.public_page_home",
                "value": '{"state":"open","summary":"visitor outage","detail":{}}',
            },
        )
        passing = GuardianCheckRun(
            id=3,
            check_code=CHECK_CODE,
            subject_key="home",
            status="pass",
            summary="recovered",
            observed={},
            checked_at=moment,
        )
        session.add(passing)
        session.flush()
        assert _findings_after_threshold(session, [passing])[0].code == "public_page_home"

        second_passing = GuardianCheckRun(
            id=4,
            check_code=CHECK_CODE,
            subject_key="home",
            status="pass",
            summary="recovered",
            observed={},
            checked_at=moment,
        )
        session.add(second_passing)
        session.flush()
        assert _findings_after_threshold(session, [second_passing]) == []


def test_checker_follows_local_http_journey_without_reusing_cookies(monkeypatch) -> None:
    requests: list[tuple[str, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append((self.path, self.headers.get("Cookie")))
            if self.path == "/":
                title = "What is changing around you in Nigeria?"
                link = '<a href="/explore">Explore</a>'
                body = f"<main><h1>{title}</h1><p>Current public information and source notes are available.</p>{link}</main>"
            else:
                self.send_response(200)
                if self.path == "/explore":
                    body = (
                        "<main><h1>Explore</h1><p>Find current public information and source notes here.</p>"
                        '<a href="/explore?item=NG-LA">Fuel situation</a></main>'
                    )
                elif self.path == "/explore?item=NG-LA":
                    body = (
                        "<main><h1>Selected situation</h1><p>Current public assessment and source detail.</p>"
                        '<a href="/s/price-pms_litre-ng-la">Open situation</a></main>'
                    )
                elif self.path == "/s/price-pms_litre-ng-la":
                    body = (
                        "<main><h1>Fuel prices in Lagos</h1><p>Current public assessment and source detail.</p>"
                        '<article data-guardian-version-id="42" data-guardian-status="published">'
                        '<table><tr data-guardian-fact-row="true"><th>Price</th>'
                        "<td>₦100.00 per litre</td><td>Sept 2026</td><td>NBS</td></tr></table></article>"
                        '<a href="/s/price-pms_litre-ng-la/history">Version history</a></main>'
                    )
                elif self.path == "/s/price-pms_litre-ng-la/history":
                    body = (
                        "<main><h1>Version history</h1><p>Published assessments and changes for readers.</p>"
                        '<ol data-guardian-current-version-id="42"></ol></main>'
                    )
                else:
                    self.send_error(404)
                    return
            if self.path == "/":
                self.send_response(200)
                self.send_header("Set-Cookie", "visitor=private; HttpOnly")
            body = body.encode()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE guardian_check_run ("
                "id INTEGER PRIMARY KEY, check_code TEXT, subject_key TEXT, status TEXT, "
                "summary TEXT, observed JSON, duration_ms INTEGER, checked_at DATETIME, "
                "created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text("CREATE TABLE setting (key TEXT PRIMARY KEY, value JSON, updated_at DATETIME)")
        )

    try:
        from africasignal import backup_alerts
        from africasignal.config import get_settings
        from africasignal.guardian.public_pages import check_public_pages
        from africasignal.web import queries

        monkeypatch.setattr(
            get_settings(), "guardian_public_base_url", f"http://127.0.0.1:{server.server_port}"
        )
        monkeypatch.setattr(backup_alerts, "apply", lambda *_args: None)
        fact = {
            "label": "Price",
            "value": 100,
            "unit": "NGN/litre",
            "period": "Sept 2026",
            "source_label": "NBS",
        }
        monkeypatch.setattr(
            queries,
            "current_situation",
            lambda *_args: SimpleNamespace(
                version=SimpleNamespace(id=42, facts=[fact]), effective_status="published"
            ),
        )
        with Session(engine) as session:
            rows = check_public_pages(session, datetime(2026, 10, 17, 12, 0, tzinfo=UTC))

        statuses = {row.subject_key: row.status for row in rows}
        assert statuses["situation"] == "pass", (statuses, rows[-2].summary, rows[-2].observed)
        assert [path for path, _cookie in requests] == [
            "/",
            "/explore",
            "/explore?item=NG-LA",
            "/s/price-pms_litre-ng-la",
            "/s/price-pms_litre-ng-la/history",
        ]
        assert all(cookie is None for _path, cookie in requests)
        assert statuses == {
            "home": "pass",
            "explore": "pass",
            "selected_explore": "pass",
            "situation": "pass",
            "history": "pass",
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
