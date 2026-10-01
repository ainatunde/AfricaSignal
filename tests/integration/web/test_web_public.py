"""The public pages (AS-030): they render from real NBS data, without JavaScript, for every
evidence state, and show only what a reader may see."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.models import (
    AssessmentVersion,
    EvidenceDocument,
    Setting,
    Source,
    SourcePermission,
)
from africasignal.storage import S3Store
from africasignal.web.routes import public
from tests.integration.nbs_support import import_bytes
from tests.integration.web.web_support import (
    FOOD_OCT,
    PMS_SEP,
    PUBLISHED_AT,
    assess_and_publish,
    publish,
    seed_petrol,
)

_TAGS = re.compile(r"<[^>]+>")


def text_of(html: str) -> str:
    body = html.split("<main", 1)[-1].split("</main>", 1)[0]
    return re.sub(r"\s+", " ", _TAGS.sub(" ", body))


def test_a_situation_page_follows_the_layout_of_the_spec(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    seed_petrol(session, store, source)
    response = client.get("/s/price-pms_litre-ng-la")
    assert response.status_code == 200
    page, plain = response.text, text_of(response.text)
    assert '<html lang="en">' in page and "<h1>" in page
    assert "Average petrol (PMS) price in Lagos State rose" in page  # 1. headline
    assert "Official figure" in page  # 1. evidence-state badge
    assert "Lagos State (state average, NBS) · October 2024" in plain  # 2. scope and period
    assert "₦1,080.95 per litre" in plain  # 3. key facts
    assert "Change vs last month" in plain and "Change vs last year" in plain
    assert "<svg" in page and "Price history" in plain  # 4. chart, server-rendered
    assert "Possible factors" in plain and "Crude oil price: not checked" in plain  # 6.
    assert "What we don't know" in plain and "state-wide" in plain  # 7.
    assert "Evidence" in plain and "Premium Motor Spirit" in plain  # 8.
    assert "Last checked 25 November 2024" in plain and "version 1" in plain  # 9.
    assert 'href="/s/price-pms_litre-ng-la/history"' in page
    assert "Share this page" in page and "?ref=share" in page  # 10.
    assert "Why this matters" not in plain  # no explanation yet (AS-028)


def test_the_page_needs_no_javascript_and_is_light(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    seed_petrol(session, store, source)
    response = client.get("/s/price-pms_litre-ng-la")
    scripts = re.findall(r"<script[^>]*>", response.text)
    assert scripts == ['<script src="/static/site.js" defer>']  # an enhancement, no inline code
    assert len(response.content) < 30_000
    css, js = client.get("/static/site.css"), client.get("/static/site.js")
    assert css.status_code == js.status_code == 200
    assert len(response.content) + len(css.content) + len(js.content) < 150_000  # B11.3


def test_the_national_page_lists_states_up_and_down(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    seed_petrol(session, store, source)
    plain = text_of(client.get("/s/price-pms_litre-ng").text)
    assert "Nigeria (national average, NBS)" in plain
    assert "States up" in plain and "Highest states" in plain and "Lowest states" in plain


def test_every_evidence_state_has_its_badge_and_words(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    (_, version) = seed_petrol(session, store, source)["NG-LA"]
    expected = {
        "reported": "Official figure",
        "corroborated": "Confirmed by independent report",
        "disputed": "Disputed",
        "insufficient": "Not enough evidence",
    }
    for state, words in expected.items():
        version.evidence_state = state
        version.severity = "none" if state == "insufficient" else "medium"
        session.flush()
        public.clear_page_cache()  # in real use a version never changes state; a new one is a new key
        page = client.get("/s/price-pms_litre-ng-la").text
        assert f"badge badge-{state}" in page and words in page, state
        others = {s for s in expected if s != state}
        assert not any(f"badge badge-{other}" in page for other in others), state


def test_insufficient_evidence_shows_no_key_facts_or_explanation(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    (_, version) = seed_petrol(session, store, source)["NG-LA"]
    version.evidence_state, version.severity = "insufficient", "none"
    version.explanation = "An explanation that must not be shown."
    session.flush()
    plain = text_of(client.get("/s/price-pms_litre-ng-la").text)
    assert "Not enough evidence" in plain
    assert "Why this matters" not in plain and "must not be shown" not in plain
    assert "Key facts" not in plain  # R3: no severity, no explanation, no headline number card


def test_an_explanation_is_shown_when_there_is_one(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    (_, version) = seed_petrol(session, store, source)["NG-LA"]
    version.explanation = "Fuel costs move with the exchange rate."
    session.flush()
    plain = text_of(client.get("/s/price-pms_litre-ng-la").text)
    assert "Why this matters Fuel costs move with the exchange rate." in plain


def test_an_expired_version_says_out_of_date(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    (_, version) = seed_petrol(session, store, source)["NG-LA"]
    version.valid_until = datetime.now(UTC) - timedelta(days=1)  # published, not yet marked stale
    session.flush()
    plain = text_of(client.get("/s/price-pms_litre-ng-la").text)
    assert "Out of date: last checked 25 November 2024" in plain


def test_a_withdrawn_assessment_says_so(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    (_, version) = seed_petrol(session, store, source)["NG-LA"]
    version.status = "withdrawn"
    version.change_summary = "The source document was withdrawn."
    session.flush()
    plain = text_of(client.get("/s/price-pms_litre-ng-la").text)
    assert "This assessment was withdrawn." in plain and "source document was withdrawn" in plain


def test_unpublished_versions_are_not_reachable(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    situation, version = seed_petrol(session, store, source)["NG-LA"]
    for status in ("draft", "withheld", "superseded"):
        version.status = status
        session.flush()
        assert client.get("/s/price-pms_litre-ng-la").status_code == 404, status
    situation.current_version_id = None
    session.flush()
    assert client.get("/s/price-pms_litre-ng-la").status_code == 404
    assert client.get("/s/no-such-situation").status_code == 404
    assert "We could not find that page" in client.get("/s/nope").text


def test_the_page_cache_is_keyed_by_version(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    situation, v1 = seed_petrol(session, store, source)["NG-LA"]
    client.cookies.set("anon_id", "a-returning-visitor-1234")  # a first visit gets a private page
    first = client.get("/s/price-pms_litre-ng-la")
    assert first.headers["cache-control"] == "public, max-age=300"
    v2 = AssessmentVersion(
        situation_id=situation.id,
        version=2,
        template=v1.template,
        template_version=v1.template_version,
        policy_version="pp-1",
        inputs_hash="other",
        status="draft",
        evidence_state="reported",
        severity="high",
        headline="A corrected headline",
        facts=v1.facts,
        possible_factors=[],
        unknowns=[],
        scope_label=v1.scope_label,
        period_label=v1.period_label,
        last_checked_at=v1.last_checked_at,
        supersedes_id=v1.id,
    )
    session.add(v2)
    session.flush()
    v1.status = "superseded"
    publish(session, situation, v2)
    assert "A corrected headline" in client.get("/s/price-pms_litre-ng-la").text


def test_the_suspension_banner_shows_on_every_page(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    seed_petrol(session, store, source)
    assert "Updates are paused" not in client.get("/s/price-pms_litre-ng-la").text
    session.add(Setting(key="publication_suspended", value=True))
    session.flush()
    for path in ("/s/price-pms_litre-ng-la", "/", "/explore", "/coverage", "/about/method"):
        assert "Updates are paused" in client.get(path).text, path


def test_evidence_quotes_follow_the_source_permission(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    _, version = seed_petrol(session, store, source)["NG-LA"]
    doc = session.get(EvidenceDocument, version.facts[0]["evidence_ids"][0])
    assert doc is not None
    doc.excerpt = "Prices of petrol rose across most states in October as depots restocked. " * 10
    permission = session.scalars(
        select(SourcePermission).where(SourcePermission.source_id == source.id)
    ).one()
    permission.max_quote_chars = 80
    session.flush()
    plain = text_of(client.get("/s/price-pms_litre-ng-la").text)
    match = re.search(r"(Prices of petrol rose[^<]*?…)", plain)
    assert match and len(match.group(1)) <= 80
    permission.max_quote_chars = 0
    session.flush()
    public.clear_page_cache()
    assert "Prices of petrol" not in client.get("/s/price-pms_litre-ng-la").text


def test_history_lists_every_published_version_and_hides_drafts(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    situation, v1 = seed_petrol(session, store, source)["NG-LA"]
    draft = AssessmentVersion(
        situation_id=situation.id,
        version=2,
        template=v1.template,
        template_version=v1.template_version,
        policy_version="unapplied",
        inputs_hash="draft",
        status="draft",
        evidence_state="reported",
        severity="none",
        headline="A draft nobody should see",
        facts=[],
        possible_factors=[],
        unknowns=[],
        scope_label="x",
        period_label="y",
    )
    session.add(draft)
    session.flush()
    page = client.get("/s/price-pms_litre-ng-la/history")
    assert page.status_code == 200
    plain = text_of(page.text)
    assert "Version 1 Current" in plain and "draft nobody" not in plain
    assert client.get("/s/price-pms_litre-ng-xx/history").status_code == 404


def test_places_label_state_averages_for_finer_places(
    session: Session, store: S3Store, source: Source, places: dict[str, int], client: TestClient
) -> None:
    from africasignal.models import Place

    seed_petrol(session, store, source)
    session.add(Place(kind="lga", name="Ikeja", code="NG-LA-IKE", parent_id=places["NG-LA"]))
    session.flush()
    lagos = text_of(client.get("/places/ng-la").text)
    assert "In Lagos" in lagos and "Average petrol (PMS) price in Lagos State" in lagos
    assert "Across Nigeria" in lagos and "Average petrol (PMS) price in Nigeria" in lagos
    ikeja = text_of(client.get("/places/NG-LA-IKE").text)
    assert "not for Ikeja itself" in ikeja and "State average: Lagos" in ikeja
    nigeria = text_of(client.get("/places/NG").text)
    assert "Nigeria as a whole" in nigeria and "Across Nigeria" not in nigeria
    assert client.get("/places/ZZ").status_code == 404


def test_explore_shows_items_and_a_state_table(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    import_bytes(session, store, source, PMS_SEP)
    import_bytes(session, store, source, "PMS_OCT_2024_REPORT.xlsx")
    for code in ("NG", "NG-LA", "NG-OG"):
        assess_and_publish(session, "pms_litre", code)
    plain = text_of(client.get("/explore?topic=energy&item=pms_litre").text)
    assert "Petrol (PMS)" in plain and "3 places" in plain
    assert "Kerosene" in plain or "kerosene" in plain.lower()
    assert "By state" in plain and "Lagos" in plain and "Ogun" in plain
    assert "₦1,080.95 per litre" in plain
    home = text_of(client.get("/explore").text)  # energy is the default topic
    assert "Energy items" in home
    assert "Food items" in text_of(client.get("/explore?topic=food").text)
    assert "Energy items" in text_of(client.get("/explore?topic=weather").text)  # unknown topic


def test_food_has_a_national_figure_and_no_state_table(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    import_bytes(session, store, source, FOOD_OCT)
    assess_and_publish(session, "rice_local_1kg", "NG")
    plain = text_of(client.get("/explore?topic=food&item=rice_local_1kg").text)
    assert "national averages only" in plain and "By state" not in plain
    assert (
        'href="/s/price-rice_local_1kg-ng"'
        in client.get("/explore?topic=food&item=rice_local_1kg").text
    )


def test_coverage_lists_items_and_sources(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    seed_petrol(session, store, source)
    source.health, source.last_success_at = "degraded", datetime(2026, 9, 29, tzinfo=UTC)
    session.flush()
    plain = text_of(client.get("/coverage").text)
    assert "Petrol (PMS)" in plain and "October 2024" in plain
    assert "National Bureau of Statistics" in plain and "Degraded" in plain
    assert "29 September 2026" in plain


def test_method_page_explains_every_state_and_the_correction_policy(
    client: TestClient, session: Session
) -> None:
    plain = text_of(client.get("/about/method").text)
    for words in ("Official figure", "Confirmed by independent report", "Disputed",
                  "Not enough evidence", "Corrections", "120 days"):  # fmt: skip
        assert words in plain


# --- For You and the place picker ---------------------------------------------------------------


def test_for_you_without_a_place_offers_a_picker_that_works_without_javascript(
    session: Session, places: dict[str, int], client: TestClient
) -> None:
    page = client.get("/")
    plain = text_of(page.text)
    assert "Choose your place" in plain and 'type="search"' in page.text
    assert 'href="/?place=NG-LA"' in page.text  # every state is a plain link
    assert "hidden" in re.search(r'<p id="geo-wrap"[^>]*>', page.text).group(0)  # type: ignore[union-attr]
    found = client.get("/?q=lag")
    assert 'href="/?place=NG-LA"' in found.text and "Results for “lag”" in found.text
    assert "No place found" in client.get("/?q=zzzzz").text


def test_choosing_a_place_sets_a_cookie_and_shows_its_situations(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    seed_petrol(session, store, source)
    chosen = client.get("/?place=ng-la", follow_redirects=False)
    assert chosen.status_code == 303 and chosen.headers["location"] == "/"
    cookie = chosen.headers["set-cookie"]
    assert "place=NG-LA" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert "Max-Age=31536000" in cookie
    home = client.get("/")  # the test client keeps the cookie
    plain = text_of(home.text)
    assert "For you in Lagos" in plain
    assert "Average petrol (PMS) price in Lagos State" in plain
    assert "Average petrol (PMS) price in Nigeria" in plain
    assert client.get("/?place=nowhere", follow_redirects=False).status_code == 200


def test_a_state_user_does_not_see_other_states(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    seed_petrol(session, store, source, ("NG-LA", "NG-OG", "NG"))
    client.cookies.set("place", "NG-OG")
    plain = text_of(client.get("/").text)
    assert "Ogun State" in plain and "Lagos State" not in plain


def test_since_your_last_visit_lists_new_versions(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    seed_petrol(session, store, source)  # published 26 November 2024
    client.cookies.set("place", "NG-LA")
    first = text_of(client.get("/").text)
    assert "Since your last visit" not in first  # a first visit has no earlier visit
    recent = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    client.cookies.clear()
    client.cookies.set("place", "NG-LA")
    client.cookies.set("visit_prev", (PUBLISHED_AT - timedelta(days=2)).isoformat())
    client.cookies.set("visit_cur", recent)
    second = text_of(client.get("/").text)
    assert "Since your last visit" in second and "Average petrol (PMS) price in Lagos" in second
    client.cookies.clear()  # the server set its own copies of these cookies above
    client.cookies.set("place", "NG-LA")
    client.cookies.set("visit_prev", (PUBLISHED_AT + timedelta(days=1)).isoformat())
    client.cookies.set("visit_cur", recent)
    third = text_of(client.get("/").text)
    assert "Nothing new since 27 November 2024" in third


def test_a_visit_ends_after_thirty_minutes_of_quiet() -> None:
    from africasignal.web.routes.public import visit_times

    now = datetime(2026, 9, 30, 12, tzinfo=UTC)
    earlier = (now - timedelta(minutes=10)).isoformat()
    long_ago = (now - timedelta(hours=5)).isoformat()
    assert visit_times(None, None, now) == (None, now)
    assert visit_times("2026-09-01T00:00:00+00:00", earlier, now)[0] == datetime(
        2026, 9, 1, tzinfo=UTC
    )  # same visit: "previous" is unchanged
    assert visit_times("2026-09-01T00:00:00+00:00", long_ago, now)[0] == now - timedelta(hours=5)
    assert visit_times("not a date", "also not", now) == (None, now)


def test_for_you_shows_latest_figures_when_nothing_is_material(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    seeded = seed_petrol(session, store, source)
    for _, version in seeded.values():
        version.severity = "none"
    session.flush()
    client.cookies.set("place", "NG-LA")
    plain = text_of(client.get("/").text)
    assert "Latest figures" in plain
    seeded["NG-LA"][1].severity = "high"
    session.flush()
    assert "Latest material changes" in text_of(client.get("/").text)


def test_unknown_urls_get_an_html_page_but_the_api_keeps_json(client: TestClient) -> None:
    page = client.get("/no/such/page")
    assert page.status_code == 404 and "We could not find that page" in page.text
    api = client.get("/v1/nothing")
    assert api.status_code == 404 and api.headers["content-type"] == "application/json"


def _policy_facts(
    *, with_previous: bool = True, announced: bool = False
) -> list[dict[str, object]]:
    def fact(label: str, value: float, unit: str, period: str) -> dict[str, object]:
        return {
            "label": label,
            "value": value,
            "unit": unit,
            "period": period,
            "source_label": "NERC order",
        }

    facts = [fact("Current rate", 209.5, "NGN/kWh", "from 3 June 2024")]
    if with_previous:
        facts += [
            fact("Previous rate", 68.0, "NGN/kWh", "from 1 March 2024"),
            fact("Increase in rate", 141.5, "NGN/kWh", "from 3 June 2024"),
            fact("Change in rate", 208.1, "%", "from 3 June 2024"),
        ]
    if announced:
        facts.append(fact("Announced rate", 230.0, "NGN/kWh", "from 1 January 2025"))
    return facts


def _as_policy(version: object, facts: list[dict[str, object]]) -> None:
    version.template, version.facts = "T2_policy_change", facts  # type: ignore[attr-defined]
    version.evidence_state = "reported"  # type: ignore[attr-defined]


def test_a_policy_page_shows_the_rates_and_says_it_is_an_official_statement(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    (_, version) = seed_petrol(session, store, source)["NG-LA"]
    _as_policy(version, _policy_facts(announced=True))
    session.flush()
    page = client.get("/s/price-pms_litre-ng-la").text
    plain = text_of(page)
    assert "Official statement" in plain and "badge-reported" in page
    assert "reports what an official document says (NERC order)" in plain
    assert "not a measure of what customers are being charged" in plain
    assert "Rate in force (from 3 June 2024)" in plain and "₦209.50 per kWh" in plain
    assert "Rate before" in plain and "₦68.00 per kWh" in plain and "+208.1%" in plain
    assert "Announced (from 1 January 2025)" in plain and "₦230.00 per kWh" in plain
    assert "Change vs last month" not in plain  # that is the price layout, not this one


def test_a_policy_page_without_an_earlier_rate_says_so(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    (_, version) = seed_petrol(session, store, source)["NG-LA"]
    _as_policy(version, _policy_facts(with_previous=False))
    session.flush()
    plain = text_of(client.get("/s/price-pms_litre-ng-la").text)
    assert "₦209.50 per kWh" in plain and "Rate before Change" in plain
    assert plain.count("Not available") == 2


def test_a_policy_page_with_only_an_announced_rate_shows_that(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    (_, version) = seed_petrol(session, store, source)["NG-LA"]
    facts = [f for f in _policy_facts(announced=True) if f["label"] == "Announced rate"]
    _as_policy(version, facts)
    session.flush()
    plain = text_of(client.get("/s/price-pms_litre-ng-la").text)
    assert "Announced (from 1 January 2025)" in plain and "₦230.00 per kWh" in plain
    assert "Rate in force" not in plain


def test_a_policy_page_with_insufficient_evidence_shows_no_rates(
    session: Session, store: S3Store, source: Source, client: TestClient
) -> None:
    (_, version) = seed_petrol(session, store, source)["NG-LA"]
    _as_policy(version, _policy_facts())
    version.evidence_state, version.severity = "insufficient", "none"
    session.flush()
    plain = text_of(client.get("/s/price-pms_litre-ng-la").text)
    assert "Not enough evidence" in plain and "Key facts" not in plain
