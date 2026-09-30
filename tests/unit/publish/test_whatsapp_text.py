"""Channel posts (AS-033): length, content, and the rule that no number outside the facts appears."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from africasignal.models import AssessmentVersion, Situation
from africasignal.publish.whatsapp_text import (
    MAX_CHARS,
    PostError,
    badge_words,
    check_numbers,
    situation_link,
    write_post,
)

BASE = "https://africasignal.example"


def facts() -> list[dict[str, Any]]:
    cur = {"unit": "NGN/litre", "period": "August 2026", "place_code": "NG-LA", "evidence_ids": [1]}
    return [
        {"label": "Current price", "value": 1005.47, **cur},
        {"label": "Previous month", "value": 974.22, **cur, "period": "July 2026"},
        {"label": "Month-on-month change", "value": 3.2, **cur, "unit": "%"},
        {"label": "Year-on-year change", "value": 18.6, **cur, "unit": "%"},
    ]


def version(**overrides: Any) -> AssessmentVersion:
    fields: dict[str, Any] = {
        "version": 1,
        "template": "T1_price_change",
        "evidence_state": "reported",
        "severity": "low",
        "headline": "Average petrol (PMS) price in Lagos State rose 3.2% in August 2026 "
        "to ₦1,005.47 (NBS)",
        "facts": facts(),
        "scope_label": "Lagos State (state average, NBS)",
        "period_label": "August 2026",
        "change_summary": None,
        "published_at": datetime(2026, 9, 10, tzinfo=UTC),
    }
    return AssessmentVersion(**{**fields, **overrides})


def situation() -> Situation:
    return Situation(slug="price-pms_litre-ng-la", title="Petrol (PMS) price in Lagos State")


def test_a_whatsapp_post_has_headline_scope_key_number_badge_and_tagged_link() -> None:
    post = write_post(version(), situation(), base_url=BASE, channel="wa")
    lines = post.text.split("\n")
    assert lines[0].startswith("Average petrol (PMS) price in Lagos State rose 3.2%")
    assert lines[1] == "Lagos State (state average, NBS) · August 2026"
    assert lines[2] == "Latest: ₦1,005.47 per litre (August 2026)"
    assert lines[3] == "Evidence: Official figure, no independent report yet"
    assert lines[4] == f"{BASE}/s/price-pms_litre-ng-la?ref=wa" == post.link
    assert len(post.text) <= 600


def test_the_x_post_is_at_most_270_characters_and_tagged_for_x() -> None:
    post = write_post(version(), situation(), base_url=BASE + "/", channel="x")
    assert len(post.text) <= 270 and post.text.endswith("/s/price-pms_litre-ng-la?ref=x")
    assert "Evidence: Official figure" in post.text and "Average petrol" in post.text


def test_optional_lines_are_dropped_before_the_post_gets_too_long() -> None:
    start = "Average petrol (PMS) price in Lagos State rose 3.2% in August 2026 to ₦1,005.47 (NBS) "
    long = start + "and so on " * ((420 - len(start)) // 10)  # too long with the key number line
    wa = write_post(version(headline=long.strip()), situation(), base_url=BASE, channel="wa")
    assert len(wa.text) <= 600
    assert "Latest:" not in wa.text  # the key number line went first
    with pytest.raises(PostError, match="alone need"):
        write_post(version(headline="x " * 400), situation(), base_url=BASE, channel="wa")


def test_the_one_key_number_is_dropped_on_insufficient_evidence() -> None:
    post = write_post(
        version(evidence_state="insufficient", severity="none"),
        situation(),
        base_url=BASE,
        channel="wa",
    )
    assert "Latest:" not in post.text and "Not enough evidence yet" in post.text


@pytest.mark.parametrize(
    ("state", "words"),
    [
        ("reported", "Official figure, no independent report yet"),
        ("corroborated", "Official figure, confirmed by an independent report"),
        ("disputed", "Disputed: sources disagree"),
        ("insufficient", "Not enough evidence yet"),
    ],
)
def test_the_badge_is_written_in_words(state: str, words: str) -> None:
    assert badge_words(version(evidence_state=state)) == words


def test_policy_statements_are_attributed_not_presented_as_prices_on_the_ground() -> None:
    policy = version(template="T2_policy_change")
    assert badge_words(policy) == "Official statement, no independent report yet"


def test_a_correction_is_labelled() -> None:
    post = write_post(
        version(change_summary="Corrected: NBS revised the figure"),
        situation(),
        base_url=BASE,
        channel="wa",
    )
    assert post.text.startswith("Correction: Average petrol")
    assert "NBS revised" not in post.text  # the summary's own numbers are not facts, so not used


def test_a_number_the_facts_do_not_state_is_refused() -> None:
    bad = version(headline="Average petrol price in Lagos State rose 9.9% in August 2026")
    with pytest.raises(PostError, match="9.9"):
        write_post(bad, situation(), base_url=BASE, channel="wa")
    link = situation_link(BASE, "s-1", "wa")
    assert check_numbers(f"Up 3.2% to ₦1,005.47 in 2026 {link}", facts(), link=link) == []
    assert check_numbers("Up 4.2% to ₦1,005.47", facts(), link=link) == ["4.2"]
    # digits in the link (a slug) are not claims
    assert check_numbers(f"Up 3.2% {situation_link(BASE, 'price-lpg_12-5kg-2026', 'x')}",
                         facts(), link=situation_link(BASE, "price-lpg_12-5kg-2026", "x")) == []  # fmt: skip


def test_limits_are_the_ones_the_spec_gives() -> None:
    assert MAX_CHARS == {"wa": 600, "x": 270}
