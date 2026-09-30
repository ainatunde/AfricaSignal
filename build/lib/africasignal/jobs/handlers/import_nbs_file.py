"""``import_nbs_file``: import an Excel file an operator uploaded (spec B6.3 fallback).

Used when discovery or parsing has failed and the source is degraded: the operator downloads the
workbook from NBS, uploads it in the console, and types the NBS URL it came from. The console
(AS-022) puts the bytes in object storage under a temporary key and enqueues this job:

    {"source_id": 1, "storage_key": "uploads/....xlsx", "original_url": "https://nigerianstat.gov.ng/resource/....xlsx",
     "publication": "pms", "published_on": "2024-11-19", "title": "... (October 2024)"}

The same parser runs as for a fetched file, and the upload is recorded as an evidence document
with the operator's original URL.
"""

from __future__ import annotations

import logging
from datetime import date

from africasignal.catalog import load_items
from africasignal.evidence.capture import SourceNotApproved, record_document
from africasignal.jobs.handlers import JobContext, register
from africasignal.models import Source
from africasignal.publish.situations import request_assessments
from africasignal.sources.nbs import import_workbook, title_month
from africasignal.sources.permissions import current_permission
from africasignal.storage import get_store

log = logging.getLogger("africasignal.import_nbs_file")

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@register("import_nbs_file")
def import_nbs_file(ctx: JobContext) -> None:
    session = ctx.session
    payload = ctx.job.payload
    source = session.get(Source, payload["source_id"])
    if source is None:
        raise ValueError(f"source {payload['source_id']} does not exist")
    permission = current_permission(session, source.id)
    if permission is None or not permission.may_collect:
        raise SourceNotApproved(f"source {source.slug!r} has no approved permission to collect")

    publication = load_items().publication(payload["publication"])
    vintage = date.fromisoformat(payload["published_on"])
    title = payload.get("title")
    store = get_store()
    upload_key = payload["storage_key"]

    document = record_document(
        session,
        store,
        source,
        permission,
        url=payload["original_url"],
        content=store.get(upload_key),
        content_type=XLSX_MIME,
        title=title,
    )
    result = import_workbook(
        session,
        store,
        source,
        document,
        publication,
        vintage=vintage,
        expected_month=title_month(title) if title else None,
    )
    queued = request_assessments(session, result.touched, document.id)
    if upload_key != document.storage_key:
        store.delete(upload_key)  # the evidence copy lives under its own hash-based key
    log.info(
        "imported %s: %d measurements, %d assessments queued",
        payload["original_url"],
        result.measurements,
        len(queued),
        extra={"job_id": ctx.job.id},
    )
    for note in result.notes:
        log.info("%s: %s", payload["original_url"], note, extra={"job_id": ctx.job.id})
