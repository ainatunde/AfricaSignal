from fastapi.testclient import TestClient

from africasignal.web.app import create_app


def test_healthz_reports_database_up() -> None:
    client = TestClient(create_app())
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "db": True}


def test_bot_page_names_the_crawler(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("BOT_CONTACT_EMAIL", "bots@example.org")
    response = TestClient(create_app()).get("/about/bot")
    assert response.status_code == 200
    assert "AfricaSignalBot/1.0" in response.text
    assert "robots.txt" in response.text
    assert "bots@example.org" in response.text
