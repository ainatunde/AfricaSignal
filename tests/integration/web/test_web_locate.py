"""'Use my location' (AS-030): the coordinates are used once and never written, logged or kept."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient
from geoalchemy2 import WKTElement
from sqlalchemy import Engine, event, select
from sqlalchemy.orm import Session

from africasignal.models import Place

LAT, LON = 6.61234567, 3.35123456  # inside the made-up Ikeja box below


def box(west: float, south: float, east: float, north: float) -> WKTElement:
    ring = f"{west} {south}, {east} {south}, {east} {north}, {west} {north}, {west} {south}"
    return WKTElement(f"MULTIPOLYGON((({ring})))", srid=4326)


@pytest.fixture
def geography(session: Session, places: dict[str, int]) -> None:
    lagos = session.get(Place, places["NG-LA"])
    assert lagos is not None
    lagos.geom = box(2.7, 6.3, 4.4, 6.8)
    session.add(
        Place(
            kind="lga",
            name="Ikeja",
            code="NG-LA-IKE",
            parent_id=places["NG-LA"],
            geom=box(3.3, 6.55, 3.45, 6.7),
        )
    )
    session.flush()


def test_a_point_resolves_to_its_lga_and_state(geography: None, client: TestClient) -> None:
    response = client.post("/places/locate", json={"lat": LAT, "lon": LON})
    assert response.status_code == 200
    assert response.json() == {
        "lga": {"code": "NG-LA-IKE", "name": "Ikeja", "kind": "lga"},
        "state": {"code": "NG-LA", "name": "Lagos", "kind": "state"},
    }
    assert response.headers["cache-control"] == "no-store"


def test_a_point_in_a_state_without_lga_boundaries_gives_the_state(
    geography: None, client: TestClient
) -> None:
    body = client.post("/places/locate", json={"lat": 6.4, "lon": 4.0}).json()
    assert body["lga"] is None and body["state"]["code"] == "NG-LA"


def test_a_point_outside_every_place_gives_nothing(geography: None, client: TestClient) -> None:
    body = client.post("/places/locate", json={"lat": 51.5, "lon": -0.12}).json()
    assert body == {"lga": None, "state": None}


@pytest.mark.parametrize(
    "payload",
    [{"lat": 91, "lon": 3}, {"lat": 6, "lon": 181}, {"lat": "x", "lon": 3}, {"lat": 6}, {}],
)
def test_bad_coordinates_are_refused(payload: dict[str, Any], client: TestClient) -> None:
    assert client.post("/places/locate", json=payload).status_code == 422


def test_the_request_only_reads_and_the_coordinates_reach_no_log_or_table(
    geography: None,
    client: TestClient,
    session: Session,
    engine: Engine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    caplog.set_level(logging.DEBUG)
    try:
        response = client.post("/places/locate", json={"lat": LAT, "lon": LON})
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert response.status_code == 200
    assert statements, "the lookup should have queried the database"
    assert all(s.lstrip().upper().startswith("SELECT") for s in statements), statements
    assert not session.new and not session.dirty and not session.deleted
    everything = " ".join(r.getMessage() for r in caplog.records)
    assert "6.6123" not in everything and "3.3512" not in everything
    for cookie in response.cookies.values():
        assert "6.61" not in cookie and "3.35" not in cookie
    assert "6.61" not in str(dict(response.headers)) and "3.35" not in str(dict(response.headers))
    # and nothing in the database holds them
    from sqlalchemy import text

    for table in ("event", "feedback", "audit_log", "job", "outbox"):
        assert session.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0, table
    assert session.scalars(select(Place.id).where(Place.name.like("%6.61%"))).first() is None
