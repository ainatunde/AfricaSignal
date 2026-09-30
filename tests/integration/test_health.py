from fastapi.testclient import TestClient

from africasignal.web.app import create_app


def test_healthz_reports_database_up() -> None:
    client = TestClient(create_app())
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "db": True}
