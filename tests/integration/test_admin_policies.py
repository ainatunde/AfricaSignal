"""Policy situations page: operators add and retire T2 policy series (spec B8.1, B11.5)."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal import policy_series
from africasignal.extract.claims import allowed_codes, system_prompt
from africasignal.models import AuditLog, OperatorPolicySeries, Place, Situation, Source
from africasignal.operations import policies as policy_ops
from africasignal.publish.policy_situations import ensure_policy_situations, policy_slug
from tests.integration.test_admin_console import (
    ORIGIN,
    client,  # noqa: F401  (fixture)
    make_operator,
    signed_in,
)
from tests.integration.test_policy_situations import (
    assess,
    nerc,  # noqa: F401  (fixture)
    order,
    places,  # noqa: F401  (fixture)
)

ABUJA = "electricity_tariff_band_a:abuja"
FILE_CODE = "electricity_tariff_band_a:ikeja-electric"


@pytest.fixture
def admin(client: TestClient, session: Session) -> TestClient:  # noqa: F811
    return signed_in(client, make_operator(session))


def form(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "code": ABUJA,
        "title": "Electricity tariff, Band A, Abuja Electricity",
        "topic": "energy",
        "unit": "NGN/kWh",
        "primary_sources": ["nerc"],
        "scope": "states",
        "state_codes": "NG-LA",
        "affected_groups": "Band A customers of Abuja Electricity",
        "materiality_pct": "5",
    }
    base.update(over)
    return base


def add(admin: TestClient, **over: Any):  # type: ignore[no-untyped-def]
    return admin.post("/admin/policies", data=form(**over), headers=ORIGIN)


def actions(session: Session) -> list[str]:
    return list(session.scalars(select(AuditLog.action).order_by(AuditLog.id)))


def test_the_page_lists_the_series_in_the_file(
    admin: TestClient,
    nerc: Source,  # noqa: F811
) -> None:
    page = admin.get("/admin/policies")
    assert page.status_code == 200
    assert FILE_CODE in page.text and "from policies.yaml" in page.text
    assert 'href="/admin/policies"' in page.text
    assert "NERC" not in page.text or "nerc" in page.text  # the source is offered in the form


def test_it_needs_sign_in_and_an_admin(client: TestClient, session: Session) -> None:  # noqa: F811
    assert client.get("/admin/policies").headers["location"] == "/admin/login"
    signed_in(client, make_operator(session, "editor@example.org", "editor"))
    assert client.get("/admin/policies").status_code == 403
    assert client.post("/admin/policies", data=form(), headers=ORIGIN).status_code == 403
    assert client.post("/admin/policies/1/retire", headers=ORIGIN).status_code == 403
    assert actions(session) == ["operator.sign_in"]


def test_adding_a_series_creates_its_situations_and_an_audit_row(
    admin: TestClient,
    session: Session,
    places: dict[str, int],  # noqa: F811
    nerc: Source,  # noqa: F811
) -> None:
    response = add(admin)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/policies?notice=policy_added"
    row = session.scalars(select(OperatorPolicySeries)).one()
    assert (row.code, row.scope, row.state_codes, row.active) == (ABUJA, "states", ["NG-LA"], True)
    sit = session.scalars(select(Situation).where(Situation.policy_series == ABUJA)).one()
    assert sit.slug == policy_slug(ABUJA, "NG-LA") and sit.kind == "policy"
    assert sit.title == "Electricity tariff, Band A, Abuja Electricity"  # one state: no suffix
    entry = session.scalars(select(AuditLog).where(AuditLog.action == "policy_series.add")).one()
    assert entry.after is not None and entry.after["situations"] == [sit.slug]
    page = admin.get("/admin/policies").text
    assert ABUJA in page and "added in the console" in page and sit.slug in page
    assert "Waiting for a primary document" in page


def test_a_national_series_makes_one_situation(
    admin: TestClient,
    session: Session,
    places: dict[str, int],  # noqa: F811
    nerc: Source,  # noqa: F811
) -> None:
    response = add(
        admin, code="pms_price_cap", scope="national", state_codes="", materiality_pct="2.5"
    )
    assert response.headers["location"].endswith("notice=policy_added")
    sit = session.scalars(select(Situation).where(Situation.policy_series == "pms_price_cap")).one()
    assert sit.slug == policy_slug("pms_price_cap", "NG")
    assert float(session.scalars(select(OperatorPolicySeries.materiality_pct)).one()) == 2.5


def test_with_no_places_loaded_the_series_is_stored_and_the_operator_is_told(
    admin: TestClient,
    session: Session,
    nerc: Source,  # noqa: F811
) -> None:
    session.add(Place(kind="state", name="Lagos", code="NG-LA"))
    session.flush()
    response = add(admin, scope="national", state_codes="", code="pms_price_cap")
    assert response.headers["location"].endswith("notice=policy_added_no_places")
    assert session.scalars(select(OperatorPolicySeries)).one().code == "pms_price_cap"
    assert session.scalars(select(Situation)).all() == []


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"code": "Has Spaces"}, "lower case"),
        ({"code": FILE_CODE}, "already exists"),
        ({"title": "ab"}, "title"),
        ({"unit": ""}, "unit"),
        ({"affected_groups": " "}, "who is affected"),
        ({"topic": "transport"}, "topic"),
        ({"scope": "regional"}, "national or for named states"),
        ({"primary_sources": []}, "at least one source"),
        ({"primary_sources": ["no-such-source"]}, "does not exist"),
        ({"state_codes": ""}, "at least one state code"),
        ({"state_codes": "Lagos"}, "not state codes"),
        ({"state_codes": "NG-ZZ"}, "no such state"),
        ({"scope": "national", "state_codes": "NG-LA"}, "clear the state field"),
        ({"materiality_pct": "lots"}, "must be a number"),
        ({"materiality_pct": "0"}, "between 0.1 and 100"),
    ],
)
def test_bad_input_is_refused_and_changes_nothing(
    admin: TestClient,
    session: Session,
    places: dict[str, int],  # noqa: F811
    nerc: Source,  # noqa: F811
    over: dict[str, Any],
    message: str,
) -> None:
    response = add(admin, **over)
    assert response.status_code == 400 and message in response.text
    assert session.scalars(select(OperatorPolicySeries)).all() == []
    assert "policy_series.add" not in actions(session)
    if "title" not in over:  # the form keeps what was typed
        assert "Band A customers of" in response.text or over.get("affected_groups")


def test_control_characters_are_cleaned_not_a_server_error(
    admin: TestClient,
    session: Session,
    places: dict[str, int],  # noqa: F811
    nerc: Source,  # noqa: F811
) -> None:
    response = add(
        admin,
        title="Electricity\x00 tariff\u202e Abuja",
        affected_groups="Band A\x00 customers",
        unit="NGN/kWh\x00",
        state_codes="NG-LA\x00",
    )
    assert response.status_code == 303
    row = session.scalars(select(OperatorPolicySeries)).one()
    assert (row.title, row.affected_groups, row.unit) == (
        "Electricity tariff Abuja",
        "Band A customers",
        "NGN/kWh",
    )


def test_a_code_cannot_make_the_slug_of_an_existing_situation(
    session: Session,
    places: dict[str, int],  # noqa: F811
    nerc: Source,  # noqa: F811
) -> None:
    operator = make_operator(session).operator
    session.add(
        Situation(
            slug=policy_slug("a_b:c", "NG-LA"),
            kind="policy",
            topic="energy",
            title="Taken",
            place_id=places["NG-LA"],
        )
    )
    session.flush()
    with pytest.raises(policy_ops.PolicyError, match="already exists"):
        policy_ops.add_series(session, operator, policy_ops.NewSeries(**_new("a_b:c")))


def _new(code: str) -> dict[str, Any]:
    return {
        "code": code,
        "title": "A series",
        "topic": "energy",
        "unit": "NGN/kWh",
        "primary_sources": ["nerc"],
        "scope": "states",
        "state_codes": "NG-LA",
        "affected_groups": "Customers",
    }


def test_the_series_reaches_extraction_and_the_tariff_reader(
    admin: TestClient,
    session: Session,
    places: dict[str, int],  # noqa: F811
    nerc: Source,  # noqa: F811
) -> None:
    assert ABUJA not in allowed_codes(session)[1]
    add(admin)
    assert ABUJA in allowed_codes(session)[1]
    assert ABUJA not in allowed_codes()[1]  # no session: the file only
    assert f"`{ABUJA}`" in system_prompt(session)
    assert ABUJA in policy_series.series_codes(session)
    assert [s.code for s in policy_series.all_series(session)][:1] == [FILE_CODE]


def test_a_claim_for_the_series_makes_a_published_style_assessment(
    admin: TestClient,
    session: Session,
    places: dict[str, int],  # noqa: F811
    nerc: Source,  # noqa: F811
) -> None:
    add(admin)
    order(session, nerc, "200.00", date(2026, 7, 1), series=ABUJA)
    order(session, nerc, "209.50", date(2026, 9, 1), series=ABUJA)
    sit = session.scalars(select(Situation).where(Situation.policy_series == ABUJA)).one()
    version = assess(session, sit)
    assert version.template == "T2_policy_change"
    assert "Band A customers of Abuja Electricity" in version.scope_label


def test_retiring_stops_new_work_but_keeps_what_exists(
    admin: TestClient,
    session: Session,
    places: dict[str, int],  # noqa: F811
    nerc: Source,  # noqa: F811
) -> None:
    add(admin)
    row = session.scalars(select(OperatorPolicySeries)).one()
    response = admin.post(f"/admin/policies/{row.id}/retire", headers=ORIGIN)
    assert response.headers["location"].endswith("notice=policy_retired")
    session.refresh(row)
    assert row.active is False
    assert ABUJA not in allowed_codes(session)[1]
    assert ensure_policy_situations(session, {ABUJA}) == []
    assert session.scalars(select(Situation).where(Situation.policy_series == ABUJA)).one()
    assert "retired" in admin.get("/admin/policies").text
    again = admin.post(f"/admin/policies/{row.id}/retire", headers=ORIGIN)
    assert again.status_code == 400 and "already retired" in again.text

    admin.post(f"/admin/policies/{row.id}/activate", headers=ORIGIN)
    session.refresh(row)
    assert row.active is True and ABUJA in allowed_codes(session)[1]
    assert {"policy_series.retire", "policy_series.activate"} <= set(actions(session))


def test_a_series_in_the_file_cannot_be_retired_here(admin: TestClient) -> None:
    response = admin.post("/admin/policies/999/retire", headers=ORIGIN)
    assert response.status_code == 400 and "policies.yaml" in response.text
    assert admin.post("/admin/policies/1/explode", headers=ORIGIN).status_code == 404


def test_state_changes_from_another_origin_are_refused(admin: TestClient) -> None:
    response = admin.post(
        "/admin/policies", data=form(), headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403
