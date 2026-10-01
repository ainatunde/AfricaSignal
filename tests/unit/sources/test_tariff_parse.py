from datetime import date
from decimal import Decimal

from africasignal.sources.tariff_parse import (
    BandATable,
    TableRejected,
    band_a_statements,
    band_a_table,
    effective_date,
    find_amounts,
    parse_periods,
    tariff_values,
)
from tests.unit.sources.nerc_fixtures import needs_tesseract, tariff_ocr

HEADER = "Tariff Class Apr 2024 | May - Jul 2024 | Aug 2024 - Sep 2026"
ROWS = """\
Lifeline 4.00 4.00 4.00
A - Non-MD 225.00 206.80 209.50
A-MD1 225.00 206.80 209.50
A - MD2 225.00 206.80 209.50
A - MD2 Special 225.00 206.80 209.50
B - Non-MD 62.48 62.48 62.48
"""


def table(
    header: str = HEADER,
    rows: str = ROWS,
    title: str = "Table - 3: Approved Allowed Tariffs (&/kWh) for the YTTS under IE",
) -> str:
    return f"18. Something before\n\n{title}\n{header}\n{rows}\n18. Service Delivery Commitments\n"


def test_periods_are_read_in_the_three_shapes_the_heading_uses() -> None:
    assert parse_periods(HEADER) == [
        (date(2024, 4, 1), date(2024, 4, 30)),
        (date(2024, 5, 1), date(2024, 7, 31)),
        (date(2024, 8, 1), date(2026, 9, 30)),
    ]


def test_a_year_that_ends_before_it_starts_is_not_a_period() -> None:
    assert parse_periods("Sep 2026 - Aug 2024") == []


def test_the_band_a_row_becomes_one_value_per_period() -> None:
    result = band_a_table(table())
    assert isinstance(result, BandATable)
    assert [c.value for c in result.columns] == [
        Decimal("225.00"),
        Decimal("206.80"),
        Decimal("209.50"),
    ]
    assert result.columns[-1].period_end == date(2026, 9, 30)
    assert result.passage == "A - Non-MD 225.00 206.80 209.50"


def test_the_naira_sign_is_not_needed_because_ocr_does_not_keep_it() -> None:
    for title in (
        "Table 3: Allowed Tariffs (₦/kWh)",
        "Table 3: Allowed Tariffs (8/kWh)",
        "Table 3: Allowed Tariffs (/kWh)",
    ):
        assert isinstance(band_a_table(table(title=title)), BandATable)


def test_text_without_a_tariff_table_gives_nothing() -> None:
    assert (
        band_a_table("The Commission approved the order.\nTariff Class A is explained below.")
        is None
    )


def test_a_table_of_something_other_than_band_a_is_ignored() -> None:
    assert (
        band_a_table(table(rows="Lifeline 4.00 4.00 4.00\nB - Non-MD 62.48 62.48 62.48\n")) is None
    )


def test_a_row_with_a_digit_read_with_doubt_is_refused() -> None:
    result = band_a_table(table(), doubtful_lines={"A - Non-MD 225.00 206.80 209.50"})
    assert isinstance(result, TableRejected)
    assert "doubt" in result.reason


def test_values_that_do_not_line_up_with_the_columns_are_refused() -> None:
    result = band_a_table(table(rows="A - Non-MD 206.80 209.50\n"))
    assert isinstance(result, TableRejected)
    assert "2 values for 3 columns" in result.reason


def test_band_a_rows_that_disagree_are_refused_as_a_misread() -> None:
    rows = "A - Non-MD 225.00 206.80 209.50\nA-MD1 225.00 206.80 209.05\n"
    result = band_a_table(table(rows=rows))
    assert isinstance(result, TableRejected)
    assert "disagree" in result.reason


def test_an_implausible_value_is_refused() -> None:
    result = band_a_table(table(rows="A - Non-MD 225.00 206.80 20950.00\n"))
    assert isinstance(result, TableRejected)


def test_headings_that_are_not_periods_are_refused() -> None:
    result = band_a_table(table(header="Tariff Class Class Name Rate"))
    assert isinstance(result, TableRejected)


def test_amounts_need_a_naira_sign_and_a_kwh_unit() -> None:
    text = (
        "₦209.50/kWh, N 1,234.5 per kWh, NGN 62.48/kWh, 209.50/kWh, ₦5, a 10 MW plant, N30 per MWh"
    )
    assert [a.value for a in find_amounts(text)] == [
        Decimal("209.50"),
        Decimal("1234.5"),
        Decimal("62.48"),
    ]


def test_an_n_inside_a_word_is_not_naira() -> None:
    assert find_amounts("BAND 5 per kWh and HTN 5/kWh") == []


def test_sentences_with_one_amount_on_band_a_alone_are_statements() -> None:
    text = (
        "The tariff for Band A customers shall be ₦209.50/kWh. "
        "Band B customers pay N63.17 per kWh. "
        "Band A and Band B both rose, from N60 per kWh to N70 per kWh."
    )
    found = band_a_statements(text)
    assert [(s.value, s.passage) for s in found] == [
        (Decimal("209.50"), "The tariff for Band A customers shall be ₦209.50/kWh.")
    ]
    assert text[found[0].start :].startswith(found[0].passage)


def test_a_band_a_sentence_with_two_different_amounts_is_not_a_statement() -> None:
    assert band_a_statements("Band A moves from ₦225/kWh to ₦209.50/kWh.") == []


def test_the_effective_date_survives_the_ocr_garbling_the_ordinal() -> None:
    assert effective_date("This Order shall take effect on 1* September 2026 and") == date(
        2026, 9, 1
    )
    assert effective_date("This Order shall take effect from the 4th September 2026") == date(
        2026, 9, 4
    )
    assert effective_date("no date here") is None


def test_tariff_values_are_the_table_and_the_prose_together() -> None:
    text = table() + "\nBand A customers pay ₦209.50/kWh and lifeline N4 per kWh."
    assert tariff_values(text) == {
        Decimal("225.00"),
        Decimal("206.80"),
        Decimal("209.50"),
        Decimal("4"),
    }


@needs_tesseract
def test_the_real_september_2026_schedule_of_ikeja_electric_gives_a_band_a_tariff_of_209_50() -> (
    None
):
    ocr = tariff_ocr()
    result = band_a_table(ocr.text, ocr.doubtful_lines)
    assert isinstance(result, BandATable)
    current = result.columns[-1]
    assert current.value == Decimal("209.50")
    assert (current.period_start, current.period_end) == (date(2024, 8, 1), date(2026, 9, 30))
    assert [c.value for c in result.columns[:2]] == [Decimal("225.00"), Decimal("206.80")]
    assert effective_date(ocr.text) == date(2026, 9, 1)
