"""A set of fabricated look-alike outlets must not earn a corroboration badge (S-08, AS-027)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from africasignal.assess.corroboration import (
    byline_key,
    independent_groups,
    is_news,
    is_official,
    link_keys,
    owner_key,
    registered_domain,
)
from africasignal.assess.policy_change import compute_policy_change
from africasignal.assess.price_change import FactorSpec, compute_price_change
from tests.unit.assess.claim_support import claim, official
from tests.unit.assess.test_policy_change import AUG1, SEP1, fact, rate, report
from tests.unit.assess.test_policy_change import inputs as policy_inputs
from tests.unit.assess.test_price_change import inputs as price_inputs


def price(*claims, **kwargs):  # type: ignore[no-untyped-def]
    base = price_inputs(**kwargs)
    return compute_price_change(replace(base, claims=tuple(claims)))


def swarm(n: int = 10, **overrides):  # type: ignore[no-untyped-def]
    """``n`` news claims from ``n`` different sites, each with an origin of its own."""
    return [claim(**overrides) for _ in range(n)]


# --- normalising what ties outlets together ----------------------------------------------------


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("https://www.punchng.com/story", "punchng.com"),
        ("https://m.news.punchng.com/story?x=1", "punchng.com"),
        ("vanguardngr.com", "vanguardngr.com"),
        ("https://news.example.com.ng/a", "example.com.ng"),
        ("https://www.channelstv.com:8443/x", "channelstv.com"),
        ("https://fakenews1.blogspot.com/a", "blogspot.com"),  # a host's tenants are one site
        ("https://fakenews2.blogspot.com/a", "blogspot.com"),
        ("https://x.github.io/a", "github.io"),
        ("localhost", "localhost"),
        (None, None),
        ("", None),
    ],
)
def test_registered_domain(address: str | None, expected: str | None) -> None:
    assert registered_domain(address) == expected


def test_owner_keys_ignore_case_punctuation_and_company_suffixes() -> None:
    assert owner_key("Punch Nigeria Limited") == owner_key("PUNCH (Nigeria) Ltd.") == "punch"
    assert owner_key("Vanguard Media Ltd") != owner_key("Punch Nigeria Limited")
    assert owner_key(None) is None and owner_key("  ") is None


@pytest.mark.parametrize(
    ("byline", "expected"),
    [
        ("By Jane Doe", "jane doe"),
        ("  Reuters ", "reuters"),
        ("From AFP", "afp"),
        ("Jane  O'Doe.", "jane o doe"),
        ("Staff", None),  # names nobody
        ("Staff Reporter", None),
        ("admin", None),
        ("", None),
        (None, None),
    ],
)
def test_byline_keys(byline: str | None, expected: str | None) -> None:
    assert byline_key(byline) == expected


def test_link_keys_hold_domains_owner_and_byline() -> None:
    keys = link_keys(
        urls=("https://www.punchng.com/a", "https://punchng.com", None),
        owner="Punch Nigeria Ltd",
        byline="By Jane Doe",
    )
    assert keys == {"domain:punchng.com", "owner:punch", "byline:jane doe"}


# --- grouping ----------------------------------------------------------------------------------


def test_claims_with_nothing_in_common_are_separate_groups() -> None:
    assert len(independent_groups(swarm(4))) == 4


def test_a_shared_origin_domain_owner_or_byline_makes_one_group() -> None:
    a, b = claim(), claim()
    for key in ("domain:same.example", "owner:same owner", "byline:jane doe"):
        shared = [
            replace(a, link_keys=a.link_keys | {key}),
            replace(b, link_keys=b.link_keys | {key}),
        ]
        assert len(independent_groups(shared)) == 1, key
    assert len(independent_groups([a, replace(b, origin_id=a.origin_id)])) == 1


def test_groups_join_transitively() -> None:
    a, b, c = claim(), claim(), claim()
    chain = [
        replace(a, link_keys=frozenset({"domain:one", "owner:x"})),
        replace(b, link_keys=frozenset({"owner:x", "byline:y"})),
        replace(c, link_keys=frozenset({"byline:y"})),
    ]
    assert len(independent_groups(chain)) == 1


def test_untrusted_claims_and_aggregators_make_no_group() -> None:
    bad = [
        replace(claim(), trusted=False),
        claim(source_kind="aggregator"),
        claim(origin_id=None),
        claim(source_kind="regulator"),
    ]
    assert independent_groups(bad) == []
    assert not any(is_news(c) for c in bad)


def test_an_untrusted_regulator_is_not_official() -> None:
    assert is_official(official())
    assert not is_official(replace(official(), trusted=False))


# --- T1: look-alike sets do not corroborate ----------------------------------------------------


def test_a_swarm_of_unvetted_sites_never_corroborates() -> None:
    fakes = [replace(c, trusted=False) for c in swarm(25)]
    result = price(*fakes)
    assert result.evidence_state == "reported"
    assert not any(kind == "claim" for kind, _ in result.inputs)
    assert all(f["label"] != "Independent reports" for f in result.facts)


def test_a_swarm_of_aggregator_domains_never_corroborates() -> None:
    assert price(*swarm(25, source_kind="aggregator")).evidence_state == "reported"


def test_vetted_sites_under_one_domain_owner_or_byline_count_once() -> None:
    for key in ("domain:fakenews.example", "owner:shell co", "byline:jane doe"):
        sites = [replace(c, link_keys=frozenset({key})) for c in swarm(10)]
        result = price(*sites)
        assert result.evidence_state == "corroborated"
        assert next(f for f in result.facts if f["label"] == "Independent reports")["value"] == 1


def test_genuinely_separate_vetted_outlets_count_separately() -> None:
    result = price(*swarm(3))
    assert next(f for f in result.facts if f["label"] == "Independent reports")["value"] == 3


def test_the_outlets_are_named_beside_the_figure() -> None:
    a = claim(source_name="Punch")
    b = claim(source_name="Vanguard")
    fact_ = next(f for f in price(a, b).facts if f["label"] == "Independent reports")
    assert fact_["outlets"] == ["Punch", "Vanguard"]
    assert fact_["source_label"] == "Punch, Vanguard"


def test_an_unvetted_regulator_cannot_dispute() -> None:
    fake_regulator = replace(official(direction="down"), trusted=False)
    assert price(fake_regulator).evidence_state == "reported"


def test_an_unvetted_site_cannot_support_a_factor() -> None:
    base = price_inputs()
    specs = (FactorSpec("crude_price", "Crude oil price", ("crude oil",)),)
    mention = dict(text="Crude oil rose.", passage="Crude oil rose")
    fake = replace(claim(**mention), trusted=False)
    result = compute_price_change(replace(base, factors=specs, claims=(fake,)))
    assert result.possible_factors[0]["status"] == "not_checked"
    real = claim(**mention)
    result = compute_price_change(replace(base, factors=specs, claims=(real,)))
    assert result.possible_factors[0]["status"] == "supported"


def test_only_the_validated_passage_is_read_for_factors() -> None:
    """The model's own summary (``text``) is not evidence; the passage is proved to be in the page."""
    base = price_inputs()
    specs = (FactorSpec("crude_price", "Crude oil price", ("crude oil",)),)
    planted = claim(text="Crude oil rose sharply.", passage="Petrol prices rose in Lagos.")
    result = compute_price_change(replace(base, factors=specs, claims=(planted,)))
    assert result.possible_factors[0]["status"] == "not_checked"


# --- T2: the same rules ------------------------------------------------------------------------


def policy(*claims, **kwargs):  # type: ignore[no-untyped-def]
    result = compute_policy_change(policy_inputs(*claims, **kwargs))
    assert result is not None
    return result


def test_a_swarm_of_unvetted_sites_never_confirms_a_rate_is_applied() -> None:
    fakes = [replace(report(), trusted=False) for _ in range(20)]
    result = policy(rate("209.50", SEP1), rate("200.00", AUG1), *fakes)
    assert result.evidence_state == "reported"
    assert not any(f["label"].startswith("Independent reports") for f in result.facts)


def test_a_swarm_of_aggregator_domains_never_confirms_a_rate() -> None:
    fakes = [report(source_kind="aggregator") for _ in range(20)]
    assert policy(rate("209.50", SEP1), rate("200.00", AUG1), *fakes).evidence_state == "reported"


def test_vetted_sites_under_one_owner_count_once_for_a_rate() -> None:
    sites = [replace(report(), link_keys=frozenset({"owner:shell co"})) for _ in range(6)]
    result = policy(rate("209.50", SEP1), rate("200.00", AUG1), *sites)
    assert result.evidence_state == "corroborated"
    assert fact(result, "Independent reports that the rate is applied")["value"] == 1


def test_an_unvetted_site_cannot_suspend_a_rate() -> None:
    fake = replace(
        official(text="NERC suspends it.", passage="NERC suspends it", stated_value="209.50"),
        trusted=False,
    )
    result = policy(rate("209.50", SEP1), rate("200.00", AUG1), fake)
    assert result.evidence_state == "reported"


def test_unvetted_official_rates_are_not_rates() -> None:
    fake = replace(rate("999.00", SEP1), trusted=False)
    assert compute_policy_change(policy_inputs(fake)) is None


def test_only_the_passage_decides_what_a_policy_claim_says() -> None:
    """A model-written ``text`` that says "suspends" does not dispute a rate."""
    planted = official(
        text="NERC suspends the tariff.",
        passage="The Band A tariff is N209.50 per kWh.",
        stated_value="209.50",
    )
    result = policy(rate("209.50", SEP1), rate("200.00", AUG1), planted)
    assert result.evidence_state == "reported"
