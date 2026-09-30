"""Publication policy pp-1 (spec B8.5): one test per rule, and how the rules combine."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from africasignal.assess.publication_policy import (
    HOLD_MINUTES,
    POLICY_VERSION,
    FactDraft,
    VersionDraft,
    decide,
    precision_of,
)

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def draft(**changes: object) -> VersionDraft:
    """A routine, publishable T1 version at state scope: evidence 10 and 11 are active."""
    base = VersionDraft(
        template="T1_price_change",
        evidence_state="reported",
        severity="low",
        scope_precision="state",
        facts=(
            FactDraft("Current price", (10,), "state"),
            FactDraft("Month-on-month change", (10, 11), "state"),
        ),
        inputs_hash="new",
        now=NOW,
        active_evidence_ids=frozenset({10, 11}),
        ever_published=True,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def test_the_policy_version_is_recorded() -> None:
    assert POLICY_VERSION == "pp-1"


def test_a_routine_version_is_published() -> None:
    decision = decide(draft())
    assert (decision.status, decision.reasons, decision.insufficient_card) == (
        "published",
        (),
        False,
    )


# R1 ---------------------------------------------------------------------------------------------


def test_r1_the_kill_switch_withholds_everything() -> None:
    assert decide(draft(publication_suspended=True)).status == "withheld"
    assert decide(draft(publication_suspended=True)).reasons == ("R1",)


def test_r1_also_withholds_insufficient_cards_and_high_severity_versions() -> None:
    assert decide(draft(publication_suspended=True, evidence_state="insufficient")).status == (
        "withheld"
    )
    assert decide(draft(publication_suspended=True, severity="high")).status == "withheld"


# R2 ---------------------------------------------------------------------------------------------


def test_r2_a_fact_without_evidence_withholds() -> None:
    facts = (FactDraft("Current price", (10,), "state"), FactDraft("Orphan", (), "state"))
    decision = decide(draft(facts=facts))
    assert (decision.status, decision.reasons) == ("withheld", ("R2",))


def test_r2_a_fact_whose_evidence_is_no_longer_active_withholds() -> None:
    facts = (FactDraft("Current price", (10, 99), "state"),)  # 99 was withdrawn
    assert decide(draft(facts=facts)).reasons == ("R2",)


def test_r2_a_version_without_any_facts_withholds() -> None:
    assert decide(draft(facts=())).reasons == ("R2",)


def test_r2_all_evidence_active_passes() -> None:
    facts = (FactDraft("Current price", (10, 11), "state"),)
    assert decide(draft(facts=facts)).status == "published"


# R3 ---------------------------------------------------------------------------------------------


def test_r3_insufficient_evidence_is_published_as_an_insufficient_card() -> None:
    decision = decide(draft(evidence_state="insufficient", severity="none"))
    assert (decision.status, decision.reasons, decision.insufficient_card) == (
        "published",
        ("R3",),
        True,
    )


def test_r3_is_not_hidden_even_when_the_data_is_stale() -> None:
    """Stale official data is shown as an insufficient-evidence card, not withheld."""
    assert decide(draft(evidence_state="insufficient")).status == "published"


@pytest.mark.parametrize("state", ["reported", "corroborated", "disputed"])
def test_r3_other_evidence_states_are_not_insufficient_cards(state: str) -> None:
    assert decide(draft(evidence_state=state)).insufficient_card is False


# R4 ---------------------------------------------------------------------------------------------


def test_r4_a_value_waiting_for_range_review_withholds_t1() -> None:
    decision = decide(draft(range_failure_pending=True))
    assert (decision.status, decision.reasons) == ("withheld", ("R4",))


def test_r4_does_not_apply_to_t2() -> None:
    assert decide(draft(template="T2_policy_change", range_failure_pending=True)).status == (
        "published"
    )


def test_r4_an_approved_value_no_longer_blocks() -> None:
    assert decide(draft(range_failure_pending=False)).status == "published"


# R5 ---------------------------------------------------------------------------------------------


def test_r5_a_policy_assessment_without_a_primary_document_is_insufficient() -> None:
    decision = decide(draft(template="T2_policy_change", has_primary_document=False))
    assert (decision.status, decision.reasons, decision.insufficient_card) == (
        "published",
        ("R5",),
        True,
    )


def test_r5_with_a_primary_document_publishes_normally() -> None:
    assert decide(draft(template="T2_policy_change")).insufficient_card is False


def test_r5_does_not_apply_to_t1() -> None:
    assert decide(draft(has_primary_document=False)).status == "published"


# R6 ---------------------------------------------------------------------------------------------


def test_r6_a_fact_finer_than_the_scope_withholds() -> None:
    facts = (FactDraft("Current price", (10,), "state"), FactDraft("LGA price", (10,), "lga"))
    decision = decide(draft(facts=facts))
    assert (decision.status, decision.reasons) == ("withheld", ("R6",))


def test_r6_a_national_scope_cannot_carry_state_facts() -> None:
    facts = (FactDraft("Current price", (10,), "state"),)
    assert decide(draft(scope_precision="national", facts=facts)).reasons == ("R6",)


def test_r6_a_broader_fact_is_allowed_and_keeps_its_broader_label() -> None:
    facts = (FactDraft("National average", (10,), "national"),)
    assert decide(draft(facts=facts)).status == "published"


def test_r6_unknown_precision_is_treated_as_finer_than_any_scope() -> None:
    facts = (FactDraft("Somewhere", (10,), "unknown"),)
    assert decide(draft(scope_precision="city", facts=facts)).reasons == ("R6",)


@pytest.mark.parametrize(
    ("code", "precision"),
    [
        ("NG", "national"),
        ("NG-LA", "state"),
        ("NG-FC", "state"),
        ("NG-LA-IKE", "lga"),
        ("hood:NG-LA-lekki", "lga"),
        ("city:2332459", "city"),
        ("", "unknown"),
        ("FR", "unknown"),
    ],
)
def test_place_code_precision(code: str, precision: str) -> None:
    assert precision_of(code) == precision


# R7 ---------------------------------------------------------------------------------------------


def test_r7_a_first_high_severity_version_is_held_for_60_minutes() -> None:
    decision = decide(draft(severity="high", ever_published=False))
    assert decision.status == "held" and decision.reasons == ("R7",)
    assert decision.hold_until == NOW + timedelta(minutes=HOLD_MINUTES) == NOW + timedelta(hours=1)


@pytest.mark.parametrize("severity", ["none", "low", "medium"])
def test_r7_lower_severities_publish_immediately(severity: str) -> None:
    assert decide(draft(severity=severity, ever_published=False)).status == "published"


def test_r7_a_later_high_severity_version_is_not_held() -> None:
    assert decide(draft(severity="high", ever_published=True)).status == "published"


def test_r7_does_not_hold_an_insufficient_card() -> None:
    decision = decide(draft(severity="high", evidence_state="insufficient", ever_published=False))
    assert decision.status == "published" and decision.insufficient_card


# R8 ---------------------------------------------------------------------------------------------


def test_r8_the_same_inputs_as_the_published_version_make_no_new_version() -> None:
    decision = decide(draft(inputs_hash="same", current_published_hash="same"))
    assert (decision.status, decision.reasons) == ("unchanged", ("R8",))


def test_r8_different_inputs_publish() -> None:
    assert decide(draft(inputs_hash="new", current_published_hash="old")).status == "published"


def test_r8_nothing_published_yet_is_not_a_duplicate() -> None:
    assert decide(draft(current_published_hash=None)).status == "published"


# combinations -----------------------------------------------------------------------------------


def test_every_applicable_withholding_rule_is_reported_together() -> None:
    facts = (FactDraft("Orphan", (), "lga"),)
    decision = decide(draft(publication_suspended=True, range_failure_pending=True, facts=facts))
    assert decision.status == "withheld" and decision.reasons == ("R1", "R2", "R4", "R6")


def test_withholding_beats_publishing_rules() -> None:
    assert (
        decide(
            draft(publication_suspended=True, inputs_hash="same", current_published_hash="same")
        ).status
        == "withheld"
    )
    assert decide(draft(facts=(), severity="high", ever_published=False)).status == "withheld"


def test_decide_does_not_read_the_clock() -> None:
    later = NOW + timedelta(days=400)
    assert decide(draft(now=later, severity="high", ever_published=False)).hold_until == (
        later + timedelta(minutes=60)
    )
