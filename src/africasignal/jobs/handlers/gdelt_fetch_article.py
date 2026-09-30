"""``gdelt_fetch_article``: fetch one article GDELT pointed to, for an approved outlet (B6.7)."""

from __future__ import annotations

import logging

from africasignal.evidence.origins import assign_origin
from africasignal.extract.jobs import enqueue_extraction
from africasignal.jobs.handlers import JobContext, register
from africasignal.models import EvidenceDocument
from africasignal.sources import gdelt
from africasignal.storage import get_store

log = logging.getLogger("africasignal.gdelt_fetch_article")


@register("gdelt_fetch_article")
def gdelt_fetch_article(ctx: JobContext) -> None:
    payload = ctx.job.payload
    # A failed fetch raises gdelt.ArticleFetchError: the job is retried with backoff. The outlet
    # source's health is not touched, because one article failing says nothing about its feed.
    outcome = gdelt.fetch_article(ctx.session, get_store(), payload["url"], payload["source_id"])
    if outcome.status == "captured" and outcome.document_id is not None:
        document = ctx.session.get(EvidenceDocument, outcome.document_id)
        if document is not None:
            assign_origin(ctx.session, document)  # a wire copy joins the origin it was copied from
            enqueue_extraction(ctx.session, document.id)
    log.info(
        "article %s: %s",
        payload["url"],
        outcome.status,
        extra={"job_id": ctx.job.id},
    )
