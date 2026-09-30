"""Pages read the public address from the console settings on every request."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from africasignal import operators, settings_store
from africasignal.web.routes import public
from tests.integration.email_support import add_place, add_situation, add_version

ADDRESS = "https://africasignal.example"
BROWSER = {"User-Agent": "Mozilla/5.0"}


@pytest.fixture(autouse=True)
def _situation(session: Session) -> None:
    situation = add_situation(session, "price-pms", add_place(session, "NG-LA", "Lagos", "state"))
    add_version(session, situation)


def test_share_links_use_the_address_from_the_console(client: TestClient, session: Session) -> None:
    page = client.get("/s/price-pms", headers=BROWSER).text
    assert 'data-share="/s/price-pms?ref=share"' in page  # nothing set: the link stays relative

    operator = operators.create_operator(
        session, "ops@example.org", "correct horse battery", "admin"
    )[0]
    settings_store.set_value(session, operator, "public_base_url", ADDRESS)
    public.clear_page_cache()
    page = client.get("/s/price-pms", headers=BROWSER).text
    assert f'data-share="{ADDRESS}/s/price-pms?ref=share"' in page
