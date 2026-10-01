"""Upload an NBS file (spec B6.3 fallback): when discovery or parsing has failed, an operator
downloads the workbook from NBS and uploads it here with the address it came from. The file goes
to object storage under a name the server chooses and the existing ``import_nbs_file`` job runs
the same parser as for a fetched file. Security note S-19: the key is generated here, the handler
only reads ``uploads/<name>.xlsx``, the size is capped and the content must be an xlsx (or a ZIP
holding one)."""

from __future__ import annotations

import io
import secrets
import zipfile
from datetime import UTC, date, datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal import audit
from africasignal.catalog import load_items
from africasignal.jobs import queue
from africasignal.models import Operator, Source
from africasignal.sources.nbs_workbook import NbsParseError, check_zip_size
from africasignal.sources.permissions import current_permission
from africasignal.storage import ObjectStore

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_UPLOAD_BYTES = 20_000_000
NBS_HOST = "nigerianstat.gov.ng"
MAX_TITLE = 200


class UploadError(ValueError):
    """A refusal the operator should see."""


def nbs_sources(session: Session) -> list[Source]:
    return list(
        session.scalars(select(Source).where(Source.adapter == "nbs").order_by(Source.slug))
    )


def publication_codes() -> list[str]:
    return [p.code for p in load_items().nbs_publications]


def check_content(data: bytes) -> None:
    """The file must be an Excel workbook, or a ZIP with one inside (NBS publishes both)."""
    if not data:
        raise UploadError("the file is empty")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError(f"the file is larger than {MAX_UPLOAD_BYTES // 1_000_000} MB")
    if not data.startswith(b"PK\x03\x04"):
        raise UploadError("that is not an Excel (.xlsx) file or a ZIP holding one")
    try:
        check_zip_size(data)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            names = archive.namelist()
    except NbsParseError as exc:
        raise UploadError(str(exc)) from exc
    except zipfile.BadZipFile as exc:
        raise UploadError("the file is damaged: it cannot be opened as a ZIP") from exc
    if "xl/workbook.xml" not in names and not any(n.lower().endswith(".xlsx") for n in names):
        raise UploadError("the file holds no Excel workbook")


def check_original_url(url: str) -> str:
    url = url.strip()
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError as exc:
        raise UploadError("the original address is not a valid URL") from exc
    if parts.scheme not in ("http", "https") or parts.username or parts.password:
        raise UploadError("the original address must be a plain http or https URL")
    if not (host == NBS_HOST or host.endswith("." + NBS_HOST)):
        raise UploadError(
            f"the original address must be on {NBS_HOST}: it is recorded as the evidence"
        )
    return url


def queue_upload(
    session: Session,
    store: ObjectStore,
    operator: Operator,
    *,
    source_id: int,
    publication: str,
    published_on: str,
    original_url: str,
    title: str,
    data: bytes,
    now: datetime | None = None,
) -> int:
    """Store the file and queue the import. Returns the job id."""
    now = now or datetime.now(UTC)
    source = session.get(Source, source_id)
    if source is None or source.adapter != "nbs":
        raise UploadError("choose one of the NBS sources")
    permission = current_permission(session, source.id)
    if permission is None or not permission.may_collect:
        raise UploadError(
            "this source has no approved permission to collect yet: approve it on the Sources "
            "page first"
        )
    if publication not in publication_codes():
        raise UploadError("choose a publication from the list")
    try:
        vintage = date.fromisoformat(published_on.strip())
    except ValueError as exc:
        raise UploadError("enter the release date as YYYY-MM-DD") from exc
    if vintage > now.date() or vintage.year < 2015:
        raise UploadError("the release date must be a real past date")
    url = check_original_url(original_url)
    title = " ".join(title.split())
    if not title or len(title) > MAX_TITLE:
        raise UploadError(
            "give the release title as NBS shows it, with the month, for example "
            '"Premium Motor Spirit (Petrol) Price Watch (October 2024)"'
        )
    check_content(data)

    key = f"uploads/nbs-{secrets.token_hex(16)}.xlsx"
    store.put(key, data, XLSX_MIME)
    job_id = queue.enqueue(
        session,
        "import_nbs_file",
        {
            "source_id": source.id,
            "storage_key": key,
            "original_url": url,
            "publication": publication,
            "published_on": vintage.isoformat(),
            "title": title,
        },
        dedupe_key=f"import_nbs_file:{key}",
        max_attempts=2,
    )
    assert job_id is not None  # the key is new, so the dedupe key is too
    audit.record(
        session,
        operator,
        "nbs_upload.queue",
        "job",
        job_id,
        after={
            "source": source.slug,
            "publication": publication,
            "published_on": vintage.isoformat(),
            "original_url": url,
            "title": title,
            "bytes": len(data),
        },
    )
    return job_id
