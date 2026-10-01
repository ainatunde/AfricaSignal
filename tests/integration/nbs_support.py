"""Shared setup for the NBS import tests: places, a source, and real fixture files as evidence."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import boto3
import yaml
from moto import mock_aws
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.catalog import load_items
from africasignal.config import config_dir
from africasignal.evidence.capture import record_document
from africasignal.models import (
    EvidenceDocument,
    Measurement,
    Place,
    Series,
    Source,
    SourcePermission,
)
from africasignal.places.load import add_alias
from africasignal.sources.nbs import NbsImport, import_workbook, title_month
from africasignal.storage import S3Store
from tests.unit.sources.nbs_fixtures import manifest, read

BUCKET = "africasignal-test"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# ISO 3166-2 codes of the 36 states and the FCT, with the names NBS files resolve to.
STATES = {
    "NG-AB": "Abia", "NG-AD": "Adamawa", "NG-AK": "Akwa Ibom", "NG-AN": "Anambra",
    "NG-BA": "Bauchi", "NG-BY": "Bayelsa", "NG-BE": "Benue", "NG-BO": "Borno",
    "NG-CR": "Cross River", "NG-DE": "Delta", "NG-EB": "Ebonyi", "NG-ED": "Edo",
    "NG-EK": "Ekiti", "NG-EN": "Enugu", "NG-FC": "Federal Capital Territory", "NG-GO": "Gombe",
    "NG-IM": "Imo", "NG-JI": "Jigawa", "NG-KD": "Kaduna", "NG-KN": "Kano", "NG-KT": "Katsina",
    "NG-KE": "Kebbi", "NG-KO": "Kogi", "NG-KW": "Kwara", "NG-LA": "Lagos", "NG-NA": "Nasarawa",
    "NG-NI": "Niger", "NG-OG": "Ogun", "NG-ON": "Ondo", "NG-OS": "Osun", "NG-OY": "Oyo",
    "NG-PL": "Plateau", "NG-RI": "Rivers", "NG-SO": "Sokoto", "NG-TA": "Taraba",
    "NG-YO": "Yobe", "NG-ZA": "Zamfara",
}  # fmt: skip


def make_store() -> Iterator[S3Store]:
    """An in-memory S3 (moto) standing in for MinIO. A generator: use it in a fixture."""
    with mock_aws():
        client = boto3.client(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
        client.create_bucket(Bucket=BUCKET)
        yield S3Store(client, BUCKET)


def add_places(session: Session) -> dict[str, int]:
    """The country and 37 states with the aliases from ``config/place_aliases.yaml``. Returns
    place id by code. No geometry: the import only needs names."""
    ids: dict[str, int] = {}
    country = Place(kind="country", name="Nigeria", code="NG")
    session.add(country)
    session.flush()
    ids["NG"] = country.id
    add_alias(session, country.id, "Nigeria")
    for code, name in STATES.items():
        state = Place(kind="state", name=name, code=code, parent_id=country.id)
        session.add(state)
        session.flush()
        ids[code] = state.id
        add_alias(session, state.id, name)
    aliases = yaml.safe_load((config_dir() / "place_aliases.yaml").read_text())["states"]
    for code, names in aliases.items():
        for alias in names:
            add_alias(session, ids[code], alias)
    return ids


def add_source(session: Session, *, approved: bool = True) -> Source:
    source = Source(
        slug="nbs-elibrary",
        name="National Bureau of Statistics (eLibrary)",
        kind="official_statistics",
        adapter="nbs",
        home_url="https://nigerianstat.gov.ng/elibrary",
        schedule_minutes=1440,
        max_requests_per_hour=30,
    )
    session.add(source)
    session.flush()
    session.add(
        SourcePermission(
            source_id=source.id,
            version=1,
            may_collect=True,
            may_store_full_text=True,
            may_republish_numbers=True,
            approved_at=datetime.now(UTC) if approved else None,
        )
    )
    session.flush()
    return source


def release(file: str) -> dict[str, Any]:
    """The manifest entry of a saved file: title, release date, URL."""
    return next(f for f in manifest()["files"] if f["file"] == file)


def release_date(file: str) -> date:
    return datetime.strptime(release(file)["listing_release_date"], "%a %b %d %Y").date()


def import_bytes(
    session: Session,
    store: S3Store,
    source: Source,
    file: str,
    content: bytes | None = None,
    *,
    vintage: date | None = None,
) -> tuple[EvidenceDocument, NbsImport]:
    """Record a workbook as evidence and import it, as ``process_document`` does. ``content``
    replaces the file's bytes (an edited copy); the URL and title stay the real ones."""
    info = release(file)
    vintage = vintage or release_date(file)
    permission = session.scalars(
        select(SourcePermission).where(SourcePermission.source_id == source.id)
    ).one()
    document = record_document(
        session,
        store,
        source,
        permission,
        url=info["source_url"],
        content=content if content is not None else read(file),
        content_type=XLSX,
        published_at=datetime(vintage.year, vintage.month, vintage.day, tzinfo=UTC),
        title=info["listing_title"],
    )
    publication = load_items().publication_for_title(info["listing_title"])
    assert publication is not None
    result = import_workbook(
        session,
        store,
        source,
        document,
        publication,
        vintage=vintage,
        expected_month=title_month(info["listing_title"]),
    )
    return document, result


def current_values(session: Session) -> dict[tuple[str, str, date], Any]:
    """(item, place code, month) -> the current value: the row nothing supersedes."""
    rows = session.execute(
        select(Series.item_code, Place.code, Measurement.period_start, Measurement.value)
        .join(Series, Series.id == Measurement.series_id)
        .join(Place, Place.id == Measurement.place_id)
        .where(Measurement.superseded_by_id.is_(None))
    ).all()
    return {(r[0], r[1], r[2]): r[3] for r in rows}
