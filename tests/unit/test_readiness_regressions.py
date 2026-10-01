"""Regression checks for destructive test safety and observable database failure."""

import pytest
from fastapi.testclient import TestClient

from tests.db_safety import require_test_database


@pytest.mark.parametrize(
    "database,env", [("production", "development"), ("africasignal_test", "production")]
)
def test_destructive_fixture_refuses_application_databases(monkeypatch, database, env):
    monkeypatch.setenv("ENV", env)
    monkeypatch.setenv("DATABASE_URL", f"postgresql://u:p@localhost/{database}")
    with pytest.raises(RuntimeError, match="dedicated"):
        require_test_database()


def test_database_failure_is_observable_by_http_monitors(monkeypatch):
    from africasignal.web.app import create_app

    monkeypatch.setattr("africasignal.web.app.database_is_up", lambda: False)
    response = TestClient(create_app()).get("/healthz")
    assert response.status_code == 503
    assert response.json() == {"ok": False, "db": False}


@pytest.mark.parametrize(
    "peer,networks,expected",
    [
        ("198.51.100.10", "127.0.0.1/32", False),
        ("127.0.0.1", "127.0.0.1/32", True),
        ("127.0.0.1", "", False),
        ("127.0.0.1", "bad-network", False),
    ],
)
def test_proxy_headers_require_an_allowed_connecting_peer(monkeypatch, peer, networks, expected):
    from africasignal.web.client_address import trusted_proxy_peer

    monkeypatch.setenv("TRUSTED_PROXY_NETWORKS", networks)
    assert trusted_proxy_peer(peer) is expected
