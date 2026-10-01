"""The sentences that explain a correction or a withdrawal (spec B9)."""

from datetime import date
from decimal import Decimal

import pytest

from africasignal.assess.invalidation import (
    Revision,
    correction_for_invalid_claim,
    correction_for_revisions,
    correction_for_withdrawn_document,
    withdrawal_headline,
)

D = Decimal


def test_one_restated_figure_reads_like_the_spec_example() -> None:
    text = correction_for_revisions("NBS", [Revision(date(2024, 7, 1), D("1000.48"), D("1010.48"))])
    assert text == "Corrected: NBS revised the July 2024 figure from ₦1,000.48 to ₦1,010.48"


def test_several_restated_figures_are_counted_and_the_newest_month_is_the_example() -> None:
    text = correction_for_revisions(
        "NBS",
        [
            Revision(date(2024, 8, 1), D("100"), D("110")),
            Revision(date(2024, 9, 1), D("1000"), D("1200.5")),
            Revision(date(2024, 9, 1), D("900"), D("950")),
        ],
    )
    assert text == (
        "Corrected: NBS revised 3 figures used in this assessment, "
        "for example the September 2024 figure from ₦1,000.00 to ₦1,200.50"
    )


def test_the_example_does_not_depend_on_the_order_of_the_revisions() -> None:
    a = Revision(date(2024, 8, 1), D("100"), D("110"))
    b = Revision(date(2024, 9, 1), D("1000"), D("1200"))
    assert correction_for_revisions("NBS", [a, b]) == correction_for_revisions("NBS", [b, a])


def test_a_correction_needs_a_revision() -> None:
    with pytest.raises(ValueError):
        correction_for_revisions("NBS", [])


def test_other_corrections_and_the_withdrawal_headline() -> None:
    assert correction_for_withdrawn_document().startswith("Corrected: a source document")
    assert correction_for_invalid_claim().startswith("Corrected: a report")
    assert (
        withdrawal_headline("the evidence was withdrawn") == "Withdrawn: the evidence was withdrawn"
    )
