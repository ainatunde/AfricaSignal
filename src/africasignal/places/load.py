"""Import boundaries, cities and aliases into the ``place`` tables (spec B10).

The import is idempotent: places are upserted by ``code`` and aliases by ``(place, alias_norm)``,
so running it twice changes nothing.

Boundaries: geoBoundaries gbOpen for Nigeria (ADM0 country, ADM1 states, ADM2 LGAs), licence
CC BY 4.0, downloaded through ``fetch_document`` and checked against the pinned SHA-256 below.
If upstream republishes a file the check fails loudly with the new hash, so a person reviews the
change before it goes in.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

from africasignal.config import config_dir
from africasignal.net.fetch import FetchResult, fetch_document
from africasignal.places.normalise import normalise, slugify

log = logging.getLogger("africasignal.places.load")

GB_BASE = (
    "https://media.githubusercontent.com/media/wmgeolab/geoBoundaries/main/releaseData/gbOpen/NGA"
)
BOUNDARY_ATTRIBUTION = "geoBoundaries gbOpen NGA (William & Mary geoLab, CC BY 4.0)"


@dataclass(frozen=True)
class BoundaryFile:
    level: str
    url: str
    sha256: str
    expected_features: int


# Verified 2026-09-30: 1 country, 37 states (36 + FCT), 774 LGAs.
BOUNDARY_FILES = (
    BoundaryFile(
        "ADM0",
        f"{GB_BASE}/ADM0/geoBoundaries-NGA-ADM0.geojson",
        "cb2407fde9f81c67a3637fbc13bd1c7bcfe20c469eba77148b3e4895ee33acaa",
        1,
    ),
    BoundaryFile(
        "ADM1",
        f"{GB_BASE}/ADM1/geoBoundaries-NGA-ADM1.geojson",
        "64fa218ac3d453cc1e66412ff461c5dfa1a4a1ade0da93b239a9891b587d28f9",
        37,
    ),
    BoundaryFile(
        "ADM2",
        f"{GB_BASE}/ADM2/geoBoundaries-NGA-ADM2.geojson",
        "bef7f2cfa45e012f4772eaa61c7b99e5188aeba4d5c6badae7e9f9aae8c02fcd",
        774,
    ),
)

COUNTRY_CODE = "NG"
# The dataset calls the FCT "Abuja Federal Capital Territory".
STATE_NAME_FIXES = {"Abuja Federal Capital Territory": "Federal Capital Territory"}
MIN_CITY_POPULATION = 50_000


class PlacesDataError(Exception):
    """A boundary file did not match what the loader expects."""


def boundary_version(files: dict[str, bytes]) -> str:
    """A label that pins the exact files: attribution plus a short hash of each."""
    hashes = ",".join(
        f"{lvl}:{hashlib.sha256(b).hexdigest()[:12]}" for lvl, b in sorted(files.items())
    )
    return f"{BOUNDARY_ATTRIBUTION} [{hashes}]"


def _fetch_politely(url: str) -> FetchResult:
    """Fetch ``url``, waiting out the per-domain rate limit (the three files share one host)."""
    result = fetch_document(url, max_bytes=60_000_000, timeout=120.0)
    for _ in range(3):
        if not result.abstained:
            break
        time.sleep(1.2)
        result = fetch_document(url, max_bytes=60_000_000, timeout=120.0)
    return result


def download_boundaries(cache_dir: Path | None = None) -> dict[str, bytes]:
    """Fetch the three boundary files (or read them from ``cache_dir``), verifying SHA-256."""
    out: dict[str, bytes] = {}
    for spec in BOUNDARY_FILES:
        cached = cache_dir / f"{spec.level}.geojson" if cache_dir else None
        if cached is not None and cached.exists():
            content = cached.read_bytes()
        else:
            result = _fetch_politely(spec.url)
            if not result.success:
                raise PlacesDataError(
                    f"could not download {spec.url}: {result.error or result.status_code}"
                )
            content = result.content
            if cached is not None:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        if digest != spec.sha256:
            raise PlacesDataError(
                f"{spec.level} file hash {digest} does not match the pinned {spec.sha256}; "
                "the upstream dataset changed. Review it and update BOUNDARY_FILES."
            )
        out[spec.level] = content
    return out


GEONAMES_URL = "https://download.geonames.org/export/dump/NG.zip"
GEONAMES_ATTRIBUTION = "GeoNames (geonames.org, CC BY 4.0)"


def download_geonames(cache_dir: Path | None = None) -> str:
    """The text of GeoNames' ``NG.txt``: from ``cache_dir/NG.txt`` if there, else downloaded from
    the NG.zip dump. The dump changes daily, so it cannot be pinned by hash like the boundaries;
    ``load_cities`` keeps only populated places of 50,000 people or more."""
    cached = cache_dir / "NG.txt" if cache_dir else None
    if cached is not None and cached.exists():
        return cached.read_text(encoding="utf-8")
    result = _fetch_politely(GEONAMES_URL)
    if not result.success:
        raise PlacesDataError(
            f"could not download {GEONAMES_URL}: {result.error or result.status_code}"
        )
    try:
        with zipfile.ZipFile(io.BytesIO(result.content)) as archive:
            text_ = archive.read("NG.txt").decode("utf-8")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise PlacesDataError(f"{GEONAMES_URL} is not a GeoNames NG.zip: {exc}") from exc
    if cached is not None:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(text_, encoding="utf-8")
    return text_


def _features(
    data: bytes | dict[str, Any], expected: int | None, level: str
) -> list[dict[str, Any]]:
    collection = json.loads(data) if isinstance(data, bytes) else data
    features: list[dict[str, Any]] = collection["features"]
    if expected is not None and len(features) != expected:
        raise PlacesDataError(f"{level}: expected {expected} features, found {len(features)}")
    return features


_GEOM = "ST_Multi(ST_MakeValid(ST_SetSRID(ST_GeomFromGeoJSON(:geom), 4326)))"


def _upsert_place(
    session: Session,
    *,
    kind: str,
    name: str,
    code: str,
    parent_id: int | None,
    geom_json: str | None = None,
    point: tuple[float, float] | None = None,
    version: str | None = None,
    population: int | None = None,
) -> int:
    """Insert or update the place with this ``code``. Returns its id."""
    params: dict[str, Any] = {
        "kind": kind,
        "name": name,
        "code": code,
        "parent": parent_id,
        "geom": geom_json,
        "lon": point[0] if point else None,
        "lat": point[1] if point else None,
        "version": version,
        "population": population,
    }
    geom_sql = _GEOM if geom_json is not None else "NULL"
    point_sql = "ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)" if point else "NULL"
    row = session.execute(
        text(
            f"""
            INSERT INTO place
                (kind, name, code, parent_id, geom, point, boundary_version, population)
            VALUES (CAST(:kind AS place_kind), :name, :code, :parent, {geom_sql}, {point_sql},
                    :version, :population)
            ON CONFLICT (code) DO UPDATE SET
                kind = EXCLUDED.kind, name = EXCLUDED.name, parent_id = EXCLUDED.parent_id,
                geom = EXCLUDED.geom, point = EXCLUDED.point,
                boundary_version = EXCLUDED.boundary_version, population = EXCLUDED.population
            RETURNING id
            """
        ),
        params,
    ).scalar_one()
    return int(row)


def add_alias(session: Session, place_id: int, alias: str) -> bool:
    """Add an alias if that normalised text is not already an alias of the place."""
    norm = normalise(alias)
    if not norm:
        return False
    result = session.execute(
        text(
            "INSERT INTO place_alias (place_id, alias, alias_norm) VALUES (:p, :a, :n) "
            "ON CONFLICT (place_id, alias_norm) DO NOTHING"
        ),
        {"p": place_id, "a": alias.strip(), "n": norm},
    )
    return bool(result.rowcount)  # type: ignore[attr-defined]


def _add_name_aliases(session: Session, place_id: int, name: str) -> None:
    add_alias(session, place_id, name)
    if "/" in name:  # "Oshodi/Isolo" is also known as "Oshodi" and as "Isolo"
        for part in name.split("/"):
            add_alias(session, place_id, part)


def _state_code_for(session: Session, geom_json: str) -> str | None:
    """The state holding most of this LGA's area, by intersection."""
    row = session.execute(
        text(
            f"""
            WITH g AS (SELECT {_GEOM} AS geom)
            SELECT s.code FROM place s, g
            WHERE s.kind = 'state' AND ST_Intersects(s.geom, g.geom)
            ORDER BY ST_Area(ST_Intersection(s.geom, g.geom)) DESC LIMIT 1
            """
        ),
        {"geom": geom_json},
    ).first()
    return None if row is None else str(row[0])


def load_boundaries(
    session: Session,
    files: dict[str, bytes | dict[str, Any]],
    version: str,
    expected_counts: tuple[int, int, int] | None = (1, 37, 774),
) -> dict[str, int]:
    """Load country, states and LGAs. Returns the counts by kind.

    ``expected_counts`` (country, states, LGAs) guards against a truncated or wrong file; tests
    with small fixtures pass None."""
    counts = {"country": 0, "state": 0, "lga": 0}
    want = expected_counts or (None, None, None)

    (country,) = _features(files["ADM0"], want[0], "ADM0")
    country_id = _upsert_place(
        session,
        kind="country",
        name="Nigeria",
        code=COUNTRY_CODE,
        parent_id=None,
        geom_json=json.dumps(country["geometry"]),
        version=version,
    )
    counts["country"] = 1

    state_ids: dict[str, int] = {}
    for feature in _features(files["ADM1"], want[1], "ADM1"):
        props = feature["properties"]
        name = STATE_NAME_FIXES.get(props["shapeName"], props["shapeName"])
        code = props["shapeISO"]
        state_id = _upsert_place(
            session,
            kind="state",
            name=name,
            code=code,
            parent_id=country_id,
            geom_json=json.dumps(feature["geometry"]),
            version=version,
        )
        state_ids[code] = state_id
        _add_name_aliases(session, state_id, name)
        add_alias(session, state_id, props["shapeName"])
        counts["state"] += 1

    # LGA codes are "<state code>-<name slug>"; a repeated name within a state gets -2, -3, ...
    # in shapeID order so the codes are the same on every run.
    lga_features = sorted(
        _features(files["ADM2"], want[2], "ADM2"), key=lambda f: f["properties"]["shapeID"]
    )
    used: dict[str, int] = {}
    for feature in lga_features:
        props = feature["properties"]
        geom_json = json.dumps(feature["geometry"])
        state_code = _state_code_for(session, geom_json)
        if state_code is None:
            raise PlacesDataError(f"LGA {props['shapeName']!r} does not intersect any state")
        base = f"{state_code}-{slugify(props['shapeName'])}"
        used[base] = used.get(base, 0) + 1
        code = base if used[base] == 1 else f"{base}-{used[base]}"
        lga_id = _upsert_place(
            session,
            kind="lga",
            name=props["shapeName"],
            code=code,
            parent_id=state_ids[state_code],
            geom_json=geom_json,
            version=version,
        )
        _add_name_aliases(session, lga_id, props["shapeName"])
        counts["lga"] += 1

    add_alias(session, country_id, "Nigeria")
    return counts


def load_cities(session: Session, geonames_text: str, version: str = "GeoNames NG") -> int:
    """Load populated places with population >= 50,000 from a GeoNames ``NG.txt`` dump.

    Columns are tab-separated as documented in the GeoNames readme: 0 geonameid, 1 name,
    2 asciiname, 4 latitude... see indexes below. Each city is attached to the LGA that covers its
    point (or the nearest LGA when the point falls just outside the simplified boundaries).
    """
    loaded = 0
    for line in geonames_text.splitlines():
        cols = line.split("\t")
        if len(cols) < 15 or cols[6] != "P":  # feature class P = populated place
            continue
        population = int(cols[14] or 0)
        if population < MIN_CITY_POPULATION:
            continue
        geoname_id, name, lat, lon = cols[0], cols[1], float(cols[4]), float(cols[5])
        parent = session.execute(
            text(
                """
                SELECT id FROM place WHERE kind = 'lga'
                ORDER BY ST_Covers(geom, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)) DESC,
                         ST_Distance(geom, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326))
                LIMIT 1
                """
            ),
            {"lon": lon, "lat": lat},
        ).scalar_one_or_none()
        city_id = _upsert_place(
            session,
            kind="city",
            name=name,
            code=f"city:{geoname_id}",
            parent_id=parent,
            point=(lon, lat),
            version=version,
            population=population,
        )
        add_alias(session, city_id, name)
        add_alias(session, city_id, cols[2])
        loaded += 1
    return loaded


def load_aliases(session: Session, path: Path | None = None) -> dict[str, int]:
    """Apply ``config/place_aliases.yaml``: extra names for the country, states and LGAs, and
    neighbourhoods that resolve to an LGA."""
    config = yaml.safe_load((path or config_dir() / "place_aliases.yaml").read_text())
    counts = {"aliases": 0, "neighbourhoods": 0}

    def place_id(code: str) -> int:
        row = session.execute(
            text("SELECT id FROM place WHERE code = :c"), {"c": code}
        ).scalar_one_or_none()
        if row is None:
            raise PlacesDataError(f"place_aliases.yaml refers to unknown place code {code!r}")
        return int(row)

    def lga_id(state_code: str, name: str) -> int:
        row = session.execute(
            text(
                "SELECT l.id FROM place l JOIN place s ON s.id = l.parent_id "
                "WHERE l.kind = 'lga' AND s.code = :s AND lower(l.name) = lower(:n)"
            ),
            {"s": state_code, "n": name},
        ).scalar_one_or_none()
        if row is None:
            raise PlacesDataError(
                f"place_aliases.yaml refers to unknown LGA {name!r} in {state_code}"
            )
        return int(row)

    for code, aliases in (config.get("countries") or {}).items():
        for alias in aliases:
            counts["aliases"] += add_alias(session, place_id(code), alias)
    for code, aliases in (config.get("states") or {}).items():
        for alias in aliases:
            counts["aliases"] += add_alias(session, place_id(code), alias)
    for entry in config.get("lgas") or []:
        for alias in entry["aliases"]:
            counts["aliases"] += add_alias(session, lga_id(entry["state"], entry["name"]), alias)
    for entry in config.get("neighbourhoods") or []:
        parent = lga_id(entry["state"], entry["lga"])
        code = f"hood:{entry['state']}-{slugify(entry['name'])}"
        covered = session.execute(
            text(
                "SELECT ST_Covers(geom, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)) "
                "FROM place WHERE id = :id"
            ),
            {"lon": entry["lon"], "lat": entry["lat"], "id": parent},
        ).scalar_one()
        if not covered:
            raise PlacesDataError(
                f"neighbourhood {entry['name']!r} lies outside LGA {entry['lga']!r}"
            )
        hood_id = _upsert_place(
            session,
            kind="neighbourhood_alias",
            name=entry["name"],
            code=code,
            parent_id=parent,
            point=(entry["lon"], entry["lat"]),
        )
        add_alias(session, hood_id, entry["name"])
        counts["neighbourhoods"] += 1
    return counts


def main() -> None:
    """``python -m africasignal.places.load [--cache DIR] [--cities NG.txt | --geonames]``"""
    import argparse

    from africasignal.db import session_scope
    from africasignal.jobs.log import configure_logging

    parser = argparse.ArgumentParser(description="Load Nigerian places into the database.")
    parser.add_argument("--cache", type=Path, default=Path("data/places"), help="download cache")
    parser.add_argument("--cities", type=Path, help="a GeoNames NG.txt file (optional)")
    parser.add_argument(
        "--geonames",
        action="store_true",
        help="download GeoNames' NG.zip and load cities of 50,000 or more (CC BY 4.0)",
    )
    args = parser.parse_args()
    configure_logging()

    files = download_boundaries(args.cache)
    version = boundary_version(files)
    with session_scope() as session:
        counts = load_boundaries(session, dict(files), version)
        log.info("boundaries loaded: %s", counts)
        cities = (
            args.cities.read_text(encoding="utf-8")
            if args.cities
            else download_geonames(args.cache)
            if args.geonames
            else None
        )
        if cities is not None:
            log.info("cities loaded: %d (%s)", load_cities(session, cities), GEONAMES_ATTRIBUTION)
        log.info("aliases loaded: %s", load_aliases(session))


if __name__ == "__main__":
    main()
