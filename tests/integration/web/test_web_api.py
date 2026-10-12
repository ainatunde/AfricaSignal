"""The JSON API (AS-035): schemas, filters, and the rules on quotes and rate limits."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.assess.explain import PROMPT_VERSION
from africasignal.models import EvidenceDocument, Place, Source, SourcePermission
from africasignal.storage import S3Store
from africasignal.web.routes import api_v1
from africasignal.web.routes.api_v1 import (
    Coverage,
    PlaceSearch,
    SituationDetail,
    SituationList,
    VersionList,
)
from tests.integration.nbs_support import import_bytes
from tests.integration.web.web_support import FOOD_OCT, assess_and_publish, seed_petrol

REQUIRED_ON_ASSESSMENTS = {
    "scope_label",
    "period_label",
    "evidence_state",
    "last_checked_at",
    "valid_until",
    "version",
}


def test_list_follows_its_schema_and_carries_the_required_fields(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    seed_petrol(session, store, source)
    response = client.get("/v1/situations")
    assert response.status_code == 200
    body = SituationList.model_validate(response.json())
    assert body.count == 2 and {s.slug for s in body.situations} == {
        "price-pms_litre-ng",
        "price-pms_litre-ng-la",
    }
    for raw in response.json()["situations"]:
        assert REQUIRED_ON_ASSESSMENTS <= raw.keys()
    lagos = next(s for s in body.situations if s.slug.endswith("ng-la"))
    assert lagos.scope_label == "Lagos State (state average, NBS)"
    assert lagos.period_label == "October 2024"
    assert (lagos.place.code, lagos.place.kind, lagos.topic) == ("NG-LA", "state", "energy")
    assert lagos.url == "/s/price-pms_litre-ng-la" and lagos.version == 1
    assert response.headers["cache-control"] == "public, max-age=60"


def test_filters_by_topic_and_place(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    seed_petrol(session, store, source)
    import_bytes(session, store, source, FOOD_OCT)
    assess_and_publish(session, "rice_local_1kg", "NG")
    slugs = lambda r: sorted(s["slug"] for s in r.json()["situations"])  # noqa: E731
    assert slugs(client.get("/v1/situations?topic=food")) == ["price-rice_local_1kg-ng"]
    assert len(slugs(client.get("/v1/situations?topic=energy"))) == 2
    assert slugs(client.get("/v1/situations?place=NG-LA")) == ["price-pms_litre-ng-la"]
    assert slugs(client.get("/v1/situations?place=ng")) == [
        "price-pms_litre-ng",
        "price-rice_local_1kg-ng",
    ]
    assert client.get("/v1/situations?place=NG-XX").status_code == 404
    assert client.get("/v1/situations?topic=weather").status_code == 422


def test_a_local_government_area_gets_its_states_situations(
    session: Session, store: S3Store, source: Source, places: dict[str, int], client: TestClient
) -> None:
    seed_petrol(session, store, source)
    session.add(Place(kind="lga", name="Ikeja", code="NG-LA-IKE", parent_id=places["NG-LA"]))
    session.flush()
    found = client.get("/v1/situations?place=NG-LA-IKE").json()["situations"]
    assert [s["slug"] for s in found] == ["price-pms_litre-ng-la"]
    assert found[0]["scope_label"].startswith("Lagos State (state average")  # never "Ikeja"


def test_pagination(session: Session, store: S3Store, source: Source, client: TestClient) -> None:
    seed_petrol(session, store, source)
    page = client.get("/v1/situations?limit=1&offset=1").json()
    assert (page["count"], page["limit"], page["offset"], len(page["situations"])) == (2, 1, 1, 1)
    assert client.get("/v1/situations?limit=0").status_code == 422
    assert client.get("/v1/situations?limit=500").status_code == 422


def test_unpublished_versions_are_not_listed(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    seeded = seed_petrol(session, store, source)
    seeded["NG"][1].status = "withheld"
    session.flush()
    slugs = [s["slug"] for s in client.get("/v1/situations").json()["situations"]]
    assert slugs == ["price-pms_litre-ng-la"]
    assert client.get("/v1/situations/price-pms_litre-ng").status_code == 404
    assert client.get("/v1/situations/price-pms_litre-ng/versions").status_code == 404


def test_detail_has_facts_unknowns_and_evidence_links(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    seed_petrol(session, store, source)
    response = client.get("/v1/situations/price-pms_litre-ng-la")
    body = SituationDetail.model_validate(response.json())
    assert [f["label"] for f in body.facts][:2] == ["Current price", "Previous month"]
    assert body.facts[0]["value"] == 1080.95 and body.facts[0]["unit"] == "NGN/litre"
    assert any("state-wide" in u for u in body.unknowns)
    assert {f["status"] for f in body.possible_factors} == {"not_checked"}
    assert len(body.evidence) == 2
    assert all(e.url.startswith("https://") and e.source == "nbs-elibrary" for e in body.evidence)
    assert all(e.quote is None for e in body.evidence)  # no excerpt stored
    assert client.get("/v1/situations/nope").status_code == 404


def test_insufficient_evidence_has_no_explanation_in_the_api(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    (_, version) = seed_petrol(session, store, source)["NG-LA"]
    version.explanation = "Explains things."
    version.prompt_version = PROMPT_VERSION
    session.flush()
    assert client.get("/v1/situations/price-pms_litre-ng-la").json()["explanation"] == (
        "Explains things."
    )
    version.evidence_state, version.severity = "insufficient", "none"
    session.flush()
    assert client.get("/v1/situations/price-pms_litre-ng-la").json()["explanation"] is None


def test_quotes_never_exceed_the_sources_limit(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    (_, version) = seed_petrol(session, store, source)["NG-LA"]
    doc = session.get(EvidenceDocument, version.facts[0]["evidence_ids"][0])
    assert doc is not None
    doc.excerpt = "Petrol prices climbed in Lagos as depots restocked and queues eased. " * 30
    permission = session.scalars(
        select(SourcePermission).where(SourcePermission.source_id == source.id)
    ).one()
    for limit in (None, 300, 120, 50, 7, 1):
        permission.max_quote_chars = limit
        session.flush()
        quotes = [
            e["quote"]
            for e in client.get("/v1/situations/price-pms_litre-ng-la").json()["evidence"]
            if e["quote"]
        ]
        assert quotes, limit
        assert all(len(q) <= (limit or 300) for q in quotes), (limit, quotes)
    permission.max_quote_chars = 0
    session.flush()
    evidence = client.get("/v1/situations/price-pms_litre-ng-la").json()["evidence"]
    assert all(e["quote"] is None for e in evidence)
    permission.max_quote_chars, permission.approved_at = 100, None  # permission not approved
    session.flush()
    evidence = client.get("/v1/situations/price-pms_litre-ng-la").json()["evidence"]
    assert all(e["quote"] is None for e in evidence)


def test_versions_show_history_and_status(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    seed_petrol(session, store, source)
    body = VersionList.model_validate(
        client.get("/v1/situations/price-pms_litre-ng-la/versions").json()
    )
    assert [(v.version, v.status, v.current) for v in body.versions] == [(1, "published", True)]


def test_place_search(
    session: Session, store: S3Store, source: Source, places: dict[str, int], client: TestClient
) -> None:
    session.add(Place(kind="lga", name="Lagos Island", code="NG-LA-LAI", parent_id=places["NG-LA"]))
    session.flush()
    from africasignal.places.load import add_alias

    lga = session.scalars(select(Place).where(Place.code == "NG-LA-LAI")).one()
    add_alias(session, lga.id, "Lagos Island")
    found = PlaceSearch.model_validate(client.get("/v1/places/search?q=Lagos").json())
    assert [p.code for p in found.places] == ["NG-LA", "NG-LA-LAI"]  # exact state first
    assert found.places[1].parent is not None and found.places[1].parent.code == "NG-LA"
    assert [
        p.code
        for p in PlaceSearch.model_validate(
            client.get("/v1/places/search?q=akwa-ibom").json()
        ).places
    ] == ["NG-AK"]
    assert client.get("/v1/places/search?q=l").status_code == 422
    assert client.get("/v1/places/search").status_code == 422
    assert client.get("/v1/places/search?q=zzzzzz").json()["places"] == []
    assert client.get("/v1/places/search?q=100%25").json()["places"] == []  # LIKE is escaped


def test_coverage(session: Session, store: S3Store, source: Source, client: TestClient) -> None:
    seed_petrol(session, store, source)
    body = Coverage.model_validate(client.get("/v1/coverage").json())
    pms = next(i for i in body.items if i.code == "pms_litre")
    assert (pms.latest_period, pms.situations) == ("October 2024", 2) and pms.places_with_data == 38
    diesel = next(i for i in body.items if i.code == "ago_litre")
    assert (diesel.latest_period, diesel.places_with_data) == (None, 0)
    assert [s.slug for s in body.sources] == ["nbs-elibrary"]
    assert body.sources[0].latest_period == "October 2024"


def test_the_sixty_first_request_in_a_minute_is_refused(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    for _ in range(60):
        assert client.get("/v1/coverage").status_code == 200
    refused = client.get("/v1/coverage")
    assert refused.status_code == 429
    assert int(refused.headers["retry-after"]) >= 1
    assert client.get("/healthz").status_code == 200  # only /v1 is limited


def test_the_limiter_holds_no_address_and_forgets_old_windows() -> None:
    clock = [1000.0]
    limiter = api_v1.RateLimiter(limit=2, window_seconds=60, clock=lambda: clock[0])
    assert limiter.check("203.0.113.9")[0] and limiter.check("203.0.113.9")[0]
    assert limiter.check("203.0.113.9") == (False, 61)
    assert limiter.check("198.51.100.4")[0]  # another client has its own allowance
    assert all("203.0.113" not in key and "198.51" not in key for key in limiter._hits)
    clock[0] += 61
    assert limiter.check("203.0.113.9")[0]
    assert len(limiter._hits) == 1  # the other client's empty window was swept
