"""GDELT file parsing and filtering, against the real files in tests/fixtures/gdelt (AS-024)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from africasignal.sources import gdelt

FIXTURES = Path(__file__).parents[2] / "fixtures" / "gdelt"


def unzipped(name: str) -> str:
    return gdelt._unzip_single((FIXTURES / name).read_bytes(), name)


def export_row(**cells: str) -> str:
    """One Events row with only the named columns set (by their gdelt.EXP_* names)."""
    row = [""] * gdelt.EXPORT_COLUMNS
    for name, value in cells.items():
        row[getattr(gdelt, name)] = value
    return "\t".join(row)


def test_lastupdate_lists_the_newest_window_and_upgrades_http_to_https() -> None:
    update = gdelt.parse_lastupdate((FIXTURES / "lastupdate.txt").read_text())
    assert update.timestamp == "20260930180000"
    assert (
        update.export.url == "https://data.gdeltproject.org/gdeltv2/20260930180000.export.CSV.zip"
    )
    assert update.export.size == 74059
    assert update.export.md5 == "6c0ec09d6cf01c2f5102742f7f28af38"
    assert update.mentions.url.startswith("https://data.gdeltproject.org/")
    assert update.mentions.url.endswith(".mentions.CSV.zip")


def test_the_files_lastupdate_lists_match_its_sizes_and_checksums() -> None:
    import hashlib

    update = gdelt.parse_lastupdate((FIXTURES / "lastupdate.txt").read_text())
    for ref, name in ((update.export, "export"), (update.mentions, "mentions")):
        data = (FIXTURES / f"20260930180000.{name}.CSV.zip").read_bytes()
        assert len(data) == ref.size
        assert hashlib.md5(data).hexdigest() == ref.md5  # noqa: S324


@pytest.mark.parametrize(
    "body",
    [
        "",
        "74059 6c0e http://evil.example/gdeltv2/20260930180000.export.CSV.zip\n"
        "134701 813a http://data.gdeltproject.org/gdeltv2/20260930180000.mentions.CSV.zip\n",
        "74059 6c0e http://data.gdeltproject.org:8080/gdeltv2/20260930180000.export.CSV.zip\n"
        "134701 813a http://data.gdeltproject.org/gdeltv2/20260930180000.mentions.CSV.zip\n",
        "74059 6c0e http://data.gdeltproject.org/gdeltv2/20260930180000.export.CSV.zip\n"
        "134701 813a http://data.gdeltproject.org/gdeltv2/20260930174500.mentions.CSV.zip\n",
        "74059 6c0e http://data.gdeltproject.org/gdeltv2/20260930180000.export.CSV.zip\n",
    ],
    ids=["empty", "other host", "other port", "different windows", "no mentions file"],
)
def test_a_lastupdate_that_is_wrong_or_points_elsewhere_is_refused(body: str) -> None:
    with pytest.raises(gdelt.GdeltError):
        gdelt.parse_lastupdate(body)


def test_windows_between_steps_in_fifteen_minutes() -> None:
    assert gdelt.windows_between("20260930174500", "20260930184500") == [
        "20260930180000",
        "20260930181500",
        "20260930183000",
        "20260930184500",
    ]
    assert gdelt.windows_between("20260930234500", "20261001001500") == [
        "20261001000000",
        "20261001001500",
    ]
    assert gdelt.windows_between("20260930180000", "20260930180000") == []


def test_the_window_file_names_follow_gdelts_scheme() -> None:
    assert gdelt.window_urls("20260930180000") == (
        "https://data.gdeltproject.org/gdeltv2/20260930180000.export.CSV.zip",
        "https://data.gdeltproject.org/gdeltv2/20260930180000.mentions.CSV.zip",
    )


def test_events_keep_nigeria_by_action_location_or_nigerian_actor() -> None:
    events = gdelt.parse_events(unzipped("20260930180000.export.CSV.zip"))
    # The manifest counts 38 events acted in Nigeria and 33 with a Nigerian actor; 46 rows are
    # either (most overlap).
    assert len(events) == 46
    assert sum(e.action_country == "NI" for e in events.values()) == 38
    assert all(e.event_id > 0 for e in events.values())


def test_the_second_fixture_window_keeps_nigeria_and_no_niger_row() -> None:
    body = unzipped("20260930154500.export.CSV.zip")
    events = gdelt.parse_events(body)
    assert sum(e.action_country == "NI" for e in events.values()) == 70
    # 12 real rows place an actor in Niger (FIPS "NG"); their actors are Burkinabe, Malian or
    # Nigerien (BFA, MLI, NER), never NGA, and the action is elsewhere. A filter that read "NG" as
    # Nigeria would have kept them.
    niger = [r for r in body.splitlines() if r.split("\t")[37] == "NG" or r.split("\t")[45] == "NG"]
    assert len(niger) == 12
    assert not {int(r.split("\t")[0]) for r in niger} & events.keys()


def test_an_event_acted_in_niger_is_not_nigeria() -> None:
    niger_action = export_row(EXP_EVENT_ID="1", EXP_ACTION_COUNTRY="NG", EXP_ACTOR1_COUNTRY="NER")
    nigeria_action = export_row(EXP_EVENT_ID="2", EXP_ACTION_COUNTRY="NI")
    nigerian_actor = export_row(EXP_EVENT_ID="3", EXP_ACTION_COUNTRY="FR", EXP_ACTOR2_COUNTRY="NGA")
    nigerien_actor = export_row(EXP_EVENT_ID="4", EXP_ACTION_COUNTRY="FR", EXP_ACTOR1_COUNTRY="NER")
    events = gdelt.parse_events(
        "\n".join([niger_action, nigeria_action, nigerian_actor, nigerien_actor])
    )
    assert sorted(events) == [2, 3]
    assert events[2].action_country == "NI"


def test_mentions_keep_web_mentions_with_a_url() -> None:
    mentions = gdelt.parse_mentions(unzipped("20260930180000.mentions.CSV.zip"))
    assert len(mentions) == 4413  # every row in the fixture is a web mention
    first = mentions[0]
    assert first.event_id == 1325667209
    assert first.url.startswith("https://www.timesandstar.co.uk/")
    assert first.mention_ts.isoformat() == "2026-09-30T18:00:00+00:00"


def test_mentions_that_are_not_web_urls_are_ignored() -> None:
    def row(kind: str, identifier: str) -> str:
        cells = [""] * gdelt.MENTION_COLUMNS
        cells[gdelt.MEN_EVENT_ID], cells[gdelt.MEN_MENTION_TS] = "9", "20260930180000"
        cells[gdelt.MEN_TYPE], cells[gdelt.MEN_IDENTIFIER] = kind, identifier
        return "\t".join(cells)

    body = "\n".join(
        [
            row("1", "https://example.ng/a"),
            row("3", "10.1000/journal-doi"),  # a citation, not a URL
            row("1", "ftp://example.ng/b"),
            row("1", ""),
            row("1", "https://example.ng/" + "x" * gdelt.MAX_URL_CHARS),
        ]
    )
    assert [m.url for m in gdelt.parse_mentions(body)] == ["https://example.ng/a"]


def test_a_file_with_the_wrong_number_of_columns_is_an_error_not_silence() -> None:
    with pytest.raises(gdelt.GdeltError, match="61 columns"):
        gdelt.parse_events("a\tb\tc\n1\t2\t3\n")
    with pytest.raises(gdelt.GdeltError, match="16 columns"):
        gdelt.parse_mentions("a\tb\tc\n")
    assert gdelt.parse_events("") == {}


def test_one_malformed_row_is_skipped() -> None:
    good = export_row(EXP_EVENT_ID="5", EXP_ACTION_COUNTRY="NI")
    assert sorted(gdelt.parse_events(good + "\nshort\trow\n")) == [5]


def test_quote_characters_in_titles_do_not_swallow_rows() -> None:
    good = [export_row(EXP_EVENT_ID=str(i), EXP_ACTION_COUNTRY="NI") for i in (1, 2, 3)]
    good[0] = good[0].replace("\t\t", '\t"\t', 1)  # a lone quote in a text column
    assert sorted(gdelt.parse_events("\n".join(good))) == [1, 2, 3]


def test_unzip_refuses_archives_that_are_not_one_csv() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.csv", "x")
        archive.writestr("b.csv", "y")
    with pytest.raises(gdelt.GdeltError, match="expected one file"):
        gdelt._unzip_single(buffer.getvalue(), "f")
    with pytest.raises(gdelt.GdeltError, match="not a zip"):
        gdelt._unzip_single(b"<html>Not Found</html>", "f")


@pytest.mark.parametrize(
    ("title", "hit"),
    [
        ("Petrol price rises to N900 per litre", True),
        ("NERC approves new Band A electricity tariff", True),
        ("Tomatoes, garri and beans: what market prices look like", True),
        ("Food inflation hits 30 per cent", True),
        ("Dangote refinery raises PMS ex-depot price", True),
        ("Subsidies: what the LPG cap means", True),
        ("Super Eagles win in Abuja", False),
        ("He left two years ago", False),  # "AGO" is matched in capitals only
        ("Senate rises for recess", False),  # "rise" is not a keyword and "price" is not "rice"
        ("Rice farmers demand support", True),
        ("Sacrifice and service", False),  # "rice" inside a word
        ("", False),
        (None, False),
    ],
)
def test_the_topic_filter_matches_whole_keywords(title: str | None, hit: bool) -> None:
    assert gdelt.topic_matcher().matches(title) is hit


def test_the_acronym_ago_matches_in_capitals() -> None:
    assert gdelt.topic_matcher().matches("AGO price falls in Lagos")
