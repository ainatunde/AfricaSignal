"""Place loading and resolution against a small fixture cut from the real boundaries."""

import io
import json
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session, sessionmaker

from africasignal.net.fetch import FetchResult
from africasignal.places import load as places_load
from africasignal.places.load import (
    BOUNDARY_FILES,
    PlacesDataError,
    add_alias,
    download_boundaries,
    download_geonames,
    load_aliases,
    load_boundaries,
    load_cities,
)
from africasignal.places.normalise import normalise, slugify, strip_qualifiers
from africasignal.places.resolve import (
    resolve_candidates,
    resolve_place,
    resolve_point,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "places"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text())


def fixture_files() -> dict[str, Any]:
    return {
        "ADM0": _fixture("adm0.geojson"),
        "ADM1": _fixture("adm1.geojson"),
        "ADM2": _fixture("adm2.geojson"),
    }


@pytest.fixture(scope="module")
def loaded(engine: Engine) -> Iterator[sessionmaker[Session]]:
    """The fixture places, loaded and committed once for the module."""
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as s:
        load_boundaries(s, fixture_files(), "fixture", expected_counts=None)
        load_aliases(s)
        s.commit()
    yield factory


@pytest.fixture
def db(loaded: sessionmaker[Session]) -> Iterator[Session]:
    with loaded() as s:
        yield s
        s.rollback()


def _scalar(db: Session, sql: str, **params: object) -> Any:
    return db.execute(text(sql), params).scalar_one()


# --- normalisation --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Lagos", "lagos"),
        ("  Eti-Osa ", "eti osa"),
        ("Oshodi/Isolo", "oshodi isolo"),
        ("Jama'Are", "jamaare"),
        ("Jama’are", "jamaare"),
        ("F.C.T.", "f c t"),
        ("Ibàdan", "ibadan"),
        ("Akwa   Ibom", "akwa ibom"),
        ("Rivers & Bayelsa", "rivers and bayelsa"),
        ("", ""),
    ],
)
def test_normalise(raw: str, expected: str) -> None:
    assert normalise(raw) == expected


@pytest.mark.parametrize(
    ("norm", "expected"),
    [
        ("lagos state", "lagos"),
        ("ikeja lga", "ikeja"),
        ("ikeja local government area", "ikeja"),
        ("the federal capital territory", "federal capital territory"),
        ("kano city", "kano"),
        ("state", "state"),  # nothing left to strip
    ],
)
def test_strip_qualifiers(norm: str, expected: str) -> None:
    assert strip_qualifiers(norm) == expected


def test_slugify() -> None:
    assert slugify("Ajeromi/Ifelodun") == "ajeromi-ifelodun"


# --- loading ---------------------------------------------------------------------------------


def test_loader_creates_country_states_lgas_and_neighbourhoods(db: Session) -> None:
    kinds = dict(
        db.execute(text("SELECT kind::text, count(*) FROM place GROUP BY kind")).all()  # type: ignore[arg-type]
    )
    assert kinds == {"country": 1, "state": 17, "lga": 22, "neighbourhood_alias": 7}


def test_lgas_are_attached_to_the_state_they_lie_in(db: Session) -> None:
    rows = dict(
        db.execute(
            text(
                "SELECT l.code, s.code FROM place l JOIN place s ON s.id = l.parent_id "
                "WHERE l.kind = 'lga'"
            )
        ).all()  # type: ignore[arg-type]
    )
    assert rows["NG-LA-ikeja"] == "NG-LA"
    assert rows["NG-LA-surulere"] == "NG-LA"
    assert rows["NG-OY-surulere"] == "NG-OY"
    assert rows["NG-KO-bassa"] == "NG-KO" and rows["NG-PL-bassa"] == "NG-PL"
    assert rows["NG-FC-municipal-area-council"] == "NG-FC"


def test_states_hang_off_the_country_and_the_fct_is_named_properly(db: Session) -> None:
    assert (
        _scalar(
            db,
            "SELECT count(*) FROM place WHERE kind='state' AND parent_id = "
            "(SELECT id FROM place WHERE code='NG')",
        )
        == 17
    )
    assert _scalar(db, "SELECT name FROM place WHERE code='NG-FC'") == "Federal Capital Territory"


def test_places_carry_the_boundary_version_and_geometry(db: Session) -> None:
    assert _scalar(db, "SELECT boundary_version FROM place WHERE code='NG-LA'") == "fixture"
    assert _scalar(db, "SELECT ST_GeometryType(geom) FROM place WHERE code='NG-LA-ikeja'") == (
        "ST_MultiPolygon"
    )
    assert _scalar(db, "SELECT ST_SRID(geom) FROM place WHERE code='NG-LA-ikeja'") == 4326


def test_loading_twice_changes_nothing(loaded: sessionmaker[Session]) -> None:
    def snapshot(s: Session) -> tuple[int, int, int]:
        return (
            _scalar(s, "SELECT count(*) FROM place"),
            _scalar(s, "SELECT count(*) FROM place_alias"),
            _scalar(s, "SELECT sum(id) FROM place"),
        )

    with loaded() as s:
        before = snapshot(s)
        load_boundaries(s, fixture_files(), "fixture", expected_counts=None)
        load_aliases(s)
        s.commit()
        assert snapshot(s) == before


def test_a_repeated_lga_name_within_a_state_gets_a_numbered_code(
    engine: Engine, loaded: sessionmaker[Session]
) -> None:
    files = fixture_files()
    ikeja = next(f for f in files["ADM2"]["features"] if f["properties"]["shapeName"] == "Ikeja")
    twin = json.loads(json.dumps(ikeja))
    twin["properties"]["shapeID"] = "zzz-twin"
    files["ADM2"]["features"].append(twin)
    with loaded() as s:
        try:
            load_boundaries(s, files, "fixture", expected_counts=None)
            codes = set(
                s.execute(text("SELECT code FROM place WHERE code LIKE 'NG-LA-ikeja%'")).scalars()
            )
            assert codes == {"NG-LA-ikeja", "NG-LA-ikeja-2"}
        finally:
            s.rollback()


def test_expected_counts_guard_rejects_a_truncated_file(loaded: sessionmaker[Session]) -> None:
    with loaded() as s, pytest.raises(PlacesDataError, match="ADM1: expected 37"):
        load_boundaries(s, fixture_files(), "fixture", expected_counts=(1, 37, 774))
    # rolled back by the context manager exit: nothing was half-loaded


def test_alias_lookup_data_is_normalised_and_deduplicated(db: Session) -> None:
    place_id = _scalar(db, "SELECT id FROM place WHERE code='NG-LA'")
    assert add_alias(db, place_id, "Lagos") is False  # already there as the official name
    assert add_alias(db, place_id, "LAGOS!!") is False  # same after normalising
    assert add_alias(db, place_id, "Eko") is True
    assert add_alias(db, place_id, "  ") is False


def test_slash_names_are_also_known_by_each_part(db: Session) -> None:
    n = _scalar(
        db,
        "SELECT count(*) FROM place_alias a JOIN place p ON p.id = a.place_id "
        "WHERE p.code = 'NG-LA-oshodi-isolo' AND a.alias_norm IN ('oshodi', 'isolo', 'oshodi isolo')",
    )
    assert n == 3


def test_alias_file_with_unknown_code_is_rejected(tmp_path: Path, db: Session) -> None:
    bad = tmp_path / "aliases.yaml"
    bad.write_text(yaml.safe_dump({"states": {"NG-XX": ["Nowhere"]}}))
    with pytest.raises(PlacesDataError, match="NG-XX"):
        load_aliases(db, bad)


def test_neighbourhood_outside_its_lga_is_rejected(tmp_path: Path, db: Session) -> None:
    bad = tmp_path / "aliases.yaml"
    bad.write_text(
        yaml.safe_dump(
            {
                "neighbourhoods": [
                    {"name": "Lost", "state": "NG-LA", "lga": "Ikeja", "lat": 9.0, "lon": 7.4}
                ]
            }
        )
    )
    with pytest.raises(PlacesDataError, match="outside"):
        load_aliases(db, bad)


def test_shipped_alias_file_is_valid(db: Session) -> None:
    assert load_aliases(db)["neighbourhoods"] == 7  # config/place_aliases.yaml


# The lines below follow the GeoNames dump format (tab-separated; see the GeoNames readme).
# They are hand-written, not real GeoNames rows: the GeoNames host is not reachable from the
# environment this was built in, so the real NG.zip has not been checked against this loader.
GEONAMES = "\n".join(
    [
        "\t".join(["1001", "Ikeja City", "Ikeja City", "", "6.6018", "3.3515", "P", "PPLA",
                   "NG", "", "05", "", "", "", "313196", "", "", "Africa/Lagos", "2020-01-01"]),
        "\t".join(["1002", "Tinyville", "Tinyville", "", "6.5", "3.3", "P", "PPL",
                   "NG", "", "05", "", "", "", "1200", "", "", "Africa/Lagos", "2020-01-01"]),
        "\t".join(["1003", "Lagos Lagoon", "Lagos Lagoon", "", "6.4", "3.4", "H", "LGN",
                   "NG", "", "05", "", "", "", "0", "", "", "Africa/Lagos", "2020-01-01"]),
        "not a data line",
    ]
)  # fmt: skip


def test_cities_need_population_and_populated_place_class(db: Session) -> None:
    assert load_cities(db, GEONAMES) == 1
    row = db.execute(
        text(
            "SELECT c.code, c.population, l.code FROM place c JOIN place l ON l.id = c.parent_id "
            "WHERE c.kind = 'city'"
        )
    ).one()
    assert tuple(row) == ("city:1001", 313196, "NG-LA-ikeja")
    assert load_cities(db, GEONAMES) == 1  # idempotent
    assert _scalar(db, "SELECT count(*) FROM place WHERE kind = 'city'") == 1


# --- GeoNames download ------------------------------------------------------------------------


def _zip_of(name: str, text_: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, text_)
    return buffer.getvalue()


def test_geonames_is_downloaded_unzipped_and_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fake(url: str) -> FetchResult:
        calls.append(url)
        return FetchResult(url=url, status_code=200, content=_zip_of("NG.txt", GEONAMES))

    monkeypatch.setattr(places_load, "_fetch_politely", fake)
    assert download_geonames(tmp_path) == GEONAMES
    assert (tmp_path / "NG.txt").read_text() == GEONAMES
    assert download_geonames(tmp_path) == GEONAMES  # second run reads the cache
    assert calls == [places_load.GEONAMES_URL]


def test_a_failed_or_wrong_geonames_download_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        places_load, "_fetch_politely", lambda url: FetchResult(url=url, status_code=503)
    )
    with pytest.raises(PlacesDataError, match="could not download"):
        download_geonames(tmp_path)
    monkeypatch.setattr(
        places_load,
        "_fetch_politely",
        lambda url: FetchResult(url=url, status_code=200, content=_zip_of("other.txt", "x")),
    )
    with pytest.raises(PlacesDataError, match="not a GeoNames NG.zip"):
        download_geonames(tmp_path)
    assert not (tmp_path / "NG.txt").exists()


# --- boundary download check ------------------------------------------------------------------


def test_downloaded_files_must_match_the_pinned_hash(tmp_path: Path) -> None:
    (tmp_path / "ADM0.geojson").write_bytes(b'{"type": "FeatureCollection", "features": []}')
    with pytest.raises(PlacesDataError, match="does not match the pinned"):
        download_boundaries(tmp_path)


def test_three_boundary_files_are_pinned() -> None:
    assert [b.level for b in BOUNDARY_FILES] == ["ADM0", "ADM1", "ADM2"]
    assert all(len(b.sha256) == 64 for b in BOUNDARY_FILES)


# --- resolver ---------------------------------------------------------------------------------

# (text, status, precision, code of the resolved place or None)
CASES = [
    # states, exact and with qualifiers
    ("Lagos", "resolved", "state", "NG-LA"),
    ("lagos", "resolved", "state", "NG-LA"),
    ("LAGOS STATE", "resolved", "state", "NG-LA"),
    ("Lagos State, Nigeria", "unknown", "unknown", None),  # a compound string is not one name
    ("Oyo", "resolved", "state", "NG-OY"),
    ("Kano", "resolved", "state", "NG-KN"),
    ("Akwa Ibom", "resolved", "state", "NG-AK"),
    ("Akwa-Ibom", "resolved", "state", "NG-AK"),
    ("Akwaibom", "resolved", "state", "NG-AK"),
    ("Cross River", "resolved", "state", "NG-CR"),
    ("Cross-River State", "resolved", "state", "NG-CR"),
    ("Niger", "resolved", "state", "NG-NI"),
    ("Niger State", "resolved", "state", "NG-NI"),
    # the FCT under its many names
    ("FCT", "resolved", "state", "NG-FC"),
    ("F.C.T.", "resolved", "state", "NG-FC"),
    ("Abuja", "resolved", "state", "NG-FC"),
    ("Federal Capital Territory", "resolved", "state", "NG-FC"),
    ("Abuja Federal Capital Territory", "resolved", "state", "NG-FC"),
    # a spelling variant that is an alias, and misspellings that are fuzzy-matched
    ("Nassarawa", "resolved", "state", "NG-NA"),
    ("Anambara", "resolved", "state", "NG-AN"),
    ("Akwa Ibomm", "resolved", "state", "NG-AK"),
    # country
    ("Nigeria", "resolved", "national", "NG"),
    ("Federal Republic of Nigeria", "resolved", "national", "NG"),
    # LGAs: never coarser than named, and named coarser places are not narrowed
    ("Ikeja", "resolved", "lga", "NG-LA-ikeja"),
    ("Ikeja LGA", "resolved", "lga", "NG-LA-ikeja"),
    ("Ikeja Local Government Area", "resolved", "lga", "NG-LA-ikeja"),
    ("Lagos Island", "resolved", "lga", "NG-LA-lagos-island"),
    ("Lagos Mainland", "resolved", "lga", "NG-LA-lagos-mainland"),
    ("Eti Osa", "resolved", "lga", "NG-LA-eti-osa"),
    ("Eti-Osa", "resolved", "lga", "NG-LA-eti-osa"),
    ("Oshodi", "resolved", "lga", "NG-LA-oshodi-isolo"),
    ("Isolo", "resolved", "lga", "NG-LA-oshodi-isolo"),
    ("Oshodi/Isolo", "resolved", "lga", "NG-LA-oshodi-isolo"),
    ("Jama'are", "resolved", "lga", "NG-BA-jamaare"),
    ("Port Harcourt", "resolved", "lga", "NG-RI-port-harcourt"),
    ("PH", "resolved", "lga", "NG-RI-port-harcourt"),
    ("AMAC", "resolved", "lga", "NG-FC-municipal-area-council"),
    ("Kano Municipal", "resolved", "lga", "NG-KN-kano-municipal"),
    # neighbourhoods resolve to their LGA
    ("Lekki", "resolved", "lga", "NG-LA-eti-osa"),
    ("Victoria Island", "resolved", "lga", "NG-LA-eti-osa"),
    ("Yaba", "resolved", "lga", "NG-LA-lagos-mainland"),
    ("Wuse", "resolved", "lga", "NG-FC-municipal-area-council"),
    # LGA names that exist in more than one state are ambiguous without context
    ("Surulere", "ambiguous", "unknown", None),
    ("Bassa", "ambiguous", "unknown", None),
    ("Obi", "ambiguous", "unknown", None),
    ("Ifelodun", "ambiguous", "unknown", None),
    ("Irepodun", "ambiguous", "unknown", None),
    # nothing found
    ("Atlantis", "unknown", "unknown", None),
    ("", "unknown", "unknown", None),
    ("   ", "unknown", "unknown", None),
    ("12345", "unknown", "unknown", None),
    # LGAs are never fuzzy-matched
    ("Ikejaa", "unknown", "unknown", None),
]


@pytest.mark.parametrize(("raw", "status", "precision", "code"), CASES)
def test_resolve_place(
    db: Session, raw: str, status: str, precision: str, code: str | None
) -> None:
    result = resolve_place(db, raw)
    assert (result.status, result.precision) == (status, precision)
    assert (result.place.code if result.place else None) == code


def test_a_misspelt_lga_is_not_guessed(db: Session) -> None:
    assert resolve_place(db, "Ikejaa").status == "unknown"


def test_ambiguous_result_lists_the_candidates(db: Session) -> None:
    result = resolve_place(db, "Surulere")
    assert {c.code for c in result.candidates} == {"NG-LA-surulere", "NG-OY-surulere"}
    assert result.place is None and result.place_id is None


def test_context_state_breaks_a_tie(db: Session) -> None:
    oyo = _scalar(db, "SELECT id FROM place WHERE code='NG-OY'")
    result = resolve_place(db, "Surulere", context_state_ids=frozenset({oyo}))
    assert result.status == "resolved" and result.method == "context"
    assert result.place and result.place.code == "NG-OY-surulere"


def test_context_that_fits_neither_candidate_stays_ambiguous(db: Session) -> None:
    kano = _scalar(db, "SELECT id FROM place WHERE code='NG-KN'")
    assert resolve_place(db, "Surulere", context_state_ids=frozenset({kano})).status == "ambiguous"


def test_state_named_text_is_never_narrowed_to_an_lga(db: Session) -> None:
    """ "Lagos" must be the state even though "Lagos Island" and "Lagos Mainland" exist."""
    result = resolve_place(db, "Lagos")
    assert result.precision == "state" and result.place and result.place.kind == "state"


def test_niger_is_the_state_not_the_country(db: Session) -> None:
    result = resolve_place(db, "Niger")
    assert result.place and result.place.code == "NG-NI"


@pytest.mark.parametrize(
    ("candidates", "doc_state", "status", "code"),
    [
        (["Surulere", "Lagos"], None, "resolved", "NG-LA-surulere"),
        (["Surulere", "Oyo"], None, "resolved", "NG-OY-surulere"),
        (["Surulere"], "NG-OY", "resolved", "NG-OY-surulere"),  # the document's dominant state
        (["Surulere"], None, "ambiguous", None),
        (["Lagos", "Ikeja"], None, "resolved", "NG-LA-ikeja"),  # the finest place wins
        (["Nigeria", "Lagos"], None, "resolved", "NG-LA"),
        (["Nigeria"], None, "resolved", "NG"),
        (["Lagos", "Oyo"], None, "ambiguous", None),  # two states: not one place
        (["Atlantis", "Kano"], None, "resolved", "NG-KN"),
        (["Atlantis"], None, "unknown", None),
        ([], None, "unknown", None),
    ],
)
def test_resolve_candidates(
    db: Session, candidates: list[str], doc_state: str | None, status: str, code: str | None
) -> None:
    state_id = _scalar(db, "SELECT id FROM place WHERE code=:c", c=doc_state) if doc_state else None
    result = resolve_candidates(db, candidates, document_state_id=state_id)
    assert result.status == status
    assert (result.place.code if result.place else None) == code


# --- "use my location" --------------------------------------------------------------------------


def test_a_point_in_ikeja_resolves_to_ikeja_lga_and_lagos_state(db: Session) -> None:
    found = resolve_point(db, 6.6018, 3.3515)
    assert found is not None
    assert found.lga.code == "NG-LA-ikeja" and found.state.code == "NG-LA"


def test_a_point_in_the_gulf_of_guinea_is_outside(db: Session) -> None:
    assert resolve_point(db, 0.0, 0.0) is None


@pytest.mark.parametrize(("lat", "lon"), [(91, 0), (-91, 0), (0, 181), (0, -181)])
def test_impossible_coordinates_are_rejected(db: Session, lat: float, lon: float) -> None:
    with pytest.raises(ValueError):
        resolve_point(db, lat, lon)


def test_looking_up_a_point_stores_nothing(db: Session) -> None:
    before = _scalar(db, "SELECT count(*) FROM place")
    resolve_point(db, 6.6018, 3.3515)
    assert _scalar(db, "SELECT count(*) FROM place") == before
