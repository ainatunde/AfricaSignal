"""The parser against the real NBS files (five publications, September and October 2024)."""

import datetime
from datetime import date
from decimal import Decimal

import pytest

from africasignal.catalog import load_items
from africasignal.sources.nbs_workbook import (
    NbsParseError,
    ParsedTable,
    ParsedWorkbook,
    TableRow,
    parse_workbook,
)
from tests.unit.sources.nbs_fixtures import edit, manifest, read

D = Decimal
SEP, OCT = date(2024, 9, 1), date(2024, 10, 1)

# file name -> (publication, reference month)
FILES = {
    "FUEL_SEPT_2024_REPORT.xlsx": ("pms", SEP),
    "PMS_OCT_2024_REPORT.xlsx": ("pms", OCT),
    "DIESEL_SEPT_2024_REPORT.xlsx": ("ago", SEP),
    "DIESEL_OCT_2024_REPORT.xlsx": ("ago", OCT),
    "HOUSEHOLD_KEROSENE_SEPT_2024.xlsx": ("dpk", SEP),
    "HOUSEHOLD_KEROSENE_OCT_2024.xlsx": ("dpk", OCT),
    "GAS_PRICE_WATCH_SEPT_2024.xlsx": ("lpg", SEP),
    "GAS_PRICE_WATCH_OCT_2024.xlsx": ("lpg", OCT),
    "selected_food_sept_2024.xlsx": ("food", SEP),
    "selected_food_oct_2024.xlsx": ("food", OCT),
}
STATE_TABLES = {"pms", "ago", "dpk", "lpg"}


def parse(name: str, publication: str | None = None) -> ParsedWorkbook:
    code = publication or FILES[name][0]
    return parse_workbook(read(name), load_items().publication(code))


def row(table: ParsedTable, name: str) -> TableRow:
    return next(r for r in table.rows if r.name == name)


def test_every_saved_file_is_listed_here_and_in_the_manifest() -> None:
    assert {f["file"] for f in manifest()["files"]} == set(FILES)


@pytest.mark.parametrize("name", FILES)
def test_reference_month_and_columns_are_read_from_the_headers(name: str) -> None:
    publication, month = FILES[name]
    parsed = parse(name)
    assert parsed.publication == publication and parsed.reference_month == month
    for table in parsed.tables:
        assert table.months == {
            "year_ago": date(month.year - 1, month.month, 1),
            "previous": date(month.year, month.month - 1, 1),
            "reference": month,
        }


@pytest.mark.parametrize("name", [n for n, (p, _) in FILES.items() if p in STATE_TABLES])
def test_state_tables_have_37_states_and_a_national_row_and_no_zones(name: str) -> None:
    for table in parse(name).tables:
        names = [r.name for r in table.rows]
        assert len(names) == len(set(names)) == 37, (name, table.block, len(names))
        assert table.national is not None
        assert not {n.lower() for n in names} & {"north central", "south south", "national"}
        assert "Abuja" in names  # the FCT is called Abuja
        assert all(v is not None for r in table.rows for v in r.values.values())


@pytest.mark.parametrize("name", [n for n, (p, _) in FILES.items() if p == "food"])
def test_food_workbook_is_national_items_only(name: str) -> None:
    (table,) = parse(name).tables
    assert table.national is None and len(table.rows) == 43
    assert len({r.name for r in table.rows}) == 43


def test_petrol_october_2024() -> None:
    (table,) = parse("PMS_OCT_2024_REPORT.xlsx").tables
    assert table.block is None and table.sheet == "Fuel_October 2024"
    assert row(table, "Lagos").values == {
        "year_ago": D("590.95"),
        "previous": D("1000.48"),
        "reference": D("1080.95"),
    }
    assert table.national is not None and table.national.name == "AVERAGE"
    assert table.national.values["reference"] == D("1184.83")  # the NBS summary says N1184.83


def test_petrol_workbook_also_holds_a_diesel_sheet_which_petrol_ignores() -> None:
    name = "PMS_OCT_2024_REPORT.xlsx"
    assert [t.sheet for t in parse(name).tables] == ["Fuel_October 2024"]
    (embedded,) = parse(name, "ago").tables
    (own,) = parse("DIESEL_OCT_2024_REPORT.xlsx").tables
    assert embedded.sheet == "DIESEL OCTOBER 2024"
    assert embedded.rows == own.rows and embedded.national == own.national


def test_diesel_skips_zone_rows_and_keeps_nbs_spelling() -> None:
    (table,) = parse("DIESEL_OCT_2024_REPORT.xlsx").tables
    assert "NORTH CENTRAL" not in {r.name for r in table.rows}
    assert "Nassarawa" in {r.name for r in table.rows}  # the other files say Nasarawa
    assert row(table, "Lagos").values["reference"] == D("1281.83")
    assert table.national is not None and table.national.values["reference"] == D("1441.28")


def test_the_diesel_header_dates_have_meaningless_days() -> None:
    # The header cells are 2024-10-14 and 2024-09-14; only the month counts.
    assert parse("DIESEL_OCT_2024_REPORT.xlsx").reference_month == OCT


def test_kerosene_has_litre_and_gallon_blocks() -> None:
    parsed = parse("HOUSEHOLD_KEROSENE_OCT_2024.xlsx")
    assert [t.block for t in parsed.tables] == ["LITRE", "GALLON"]
    litre, gallon = parsed.table("LITRE"), parsed.table("gallon")  # lookup ignores case
    assert litre and gallon
    assert row(litre, "Abuja").values["reference"] == D("2875.00")
    assert row(gallon, "Katsina").values["reference"] == D("8900.50")
    assert litre.national is not None and litre.national.values["reference"] == D("2017.46")
    assert gallon.national is not None and gallon.national.values["reference"] == D("6949.75")
    assert parsed.warnings == []


def test_cooking_gas_has_5kg_and_12_5kg_blocks() -> None:
    parsed = parse("GAS_PRICE_WATCH_OCT_2024.xlsx")
    assert [t.block for t in parsed.tables] == ["5KG", "12.5KG"]
    five, big = parsed.table("5KG"), parsed.table("12.5KG")
    assert five and big
    assert five.national is not None and five.national.values["reference"] == D("6915.69")
    assert big.national is not None and big.national.values["reference"] == D("16734.55")
    assert row(five, "Lagos").values["reference"] == D("7476.32")
    assert row(big, "Lagos").values["reference"] == D("16841.25")


@pytest.mark.parametrize(
    "name", ["GAS_PRICE_WATCH_SEPT_2024.xlsx", "GAS_PRICE_WATCH_OCT_2024.xlsx"]
)
def test_a_wrong_row_label_in_the_second_block_is_reported_not_believed(name: str) -> None:
    """NBS labels the Kebbi row "Taraba" in the 12.5kg block of both cooking-gas files. The blocks
    share rows, so the first block's name is used and the mismatch is a warning."""
    parsed = parse(name)
    big = parsed.table("12.5KG")
    assert big is not None
    names = [r.name for r in big.rows]
    assert names.count("Taraba") == 1 and "Kebbi" in names
    (warning,) = parsed.warnings
    assert "'Taraba'" in warning and "'Kebbi'" in warning
    kebbi = row(big, "Kebbi").values["reference"]
    assert kebbi == (D("16397.50") if "OCT" in name else D("15306.25"))


def test_food_values_are_rounded_to_two_places() -> None:
    (table,) = parse("selected_food_oct_2024.xlsx").tables
    rice = row(table, "Rice local sold loose")
    assert rice.values == {
        "year_ago": D("819.42"),
        "previous": D("1914.77"),
        "reference": D("1944.64"),
    }
    beans = row(table, "Beans brown,sold loose")
    assert beans.values["reference"] == D("2798.50")  # 2798.4962..., stated as N2,798.50


@pytest.mark.parametrize(
    ("september", "october"),
    [
        ("FUEL_SEPT_2024_REPORT.xlsx", "PMS_OCT_2024_REPORT.xlsx"),
        ("DIESEL_SEPT_2024_REPORT.xlsx", "DIESEL_OCT_2024_REPORT.xlsx"),
        ("HOUSEHOLD_KEROSENE_SEPT_2024.xlsx", "HOUSEHOLD_KEROSENE_OCT_2024.xlsx"),
        ("GAS_PRICE_WATCH_SEPT_2024.xlsx", "GAS_PRICE_WATCH_OCT_2024.xlsx"),
        ("selected_food_sept_2024.xlsx", "selected_food_oct_2024.xlsx"),
    ],
)
def test_october_repeats_septembers_values_so_the_real_files_hold_no_restatement(
    september: str, october: str
) -> None:
    """A property of the real data, and a cross-check of the parser on different layouts: each
    October file's previous-month column equals the September file's reference column."""
    for sep_table in parse(september).tables:
        oct_table = parse(october).table(sep_table.block)
        assert oct_table is not None
        sep_values = {r.name.lower(): r.values["reference"] for r in sep_table.rows}
        for r in oct_table.rows:
            assert sep_values[r.name.lower()] == r.values["previous"], (october, r.name)


def test_the_wrong_publication_is_refused() -> None:
    with pytest.raises(NbsParseError, match="no lpg price table"):
        parse("PMS_OCT_2024_REPORT.xlsx", "lpg")
    with pytest.raises(NbsParseError, match="no pms price table"):
        parse("selected_food_oct_2024.xlsx", "pms")


def test_bytes_that_are_not_a_workbook_are_refused() -> None:
    with pytest.raises(NbsParseError, match="not a readable Excel workbook"):
        parse_workbook(b"<html>maintenance</html>", load_items().publication("pms"))


def test_value_columns_that_are_not_last_year_previous_month_reference_are_refused() -> None:
    bad = edit(
        "PMS_OCT_2024_REPORT.xlsx",
        "Fuel_October 2024",
        {"C15": datetime.datetime(2024, 8, 1)},  # previous month says August in an October file
    )
    with pytest.raises(NbsParseError, match="expected the same month last year"):
        parse_workbook(bad, load_items().publication("pms"))


def test_a_state_listed_twice_is_refused() -> None:
    bad = edit("PMS_OCT_2024_REPORT.xlsx", "Fuel_October 2024", {"A17": "Abia"})  # was Abuja
    with pytest.raises(NbsParseError, match="appears twice"):
        parse_workbook(bad, load_items().publication("pms"))


def test_missing_values_stay_missing() -> None:
    edited = edit("PMS_OCT_2024_REPORT.xlsx", "Fuel_October 2024", {"D40": None})  # Lagos
    (table,) = parse_workbook(edited, load_items().publication("pms")).tables
    assert row(table, "Lagos").values["reference"] is None
