"""``gdelt_fetch_article``: fetch one article GDELT pointed to, for an approved outlet (B6.7)."""

from __future__ import annotations

import logging

from africasignal.jobs.handlers import JobContext, register
from africasignal.sources import gdelt
from africasignal.storage import get_store

log = logging.getLogger("africasignal.gdelt_fetch_article")


@register("gdelt_fetch_article")
def gdelt_fetch_article(ctx: JobContext) -> None:
    payload = ctx.job.payload
    # A failed fetch raises gdelt.ArticleFetchError: the job is retried with backoff. The outlet
    # source's health is not touched, because one article failing says nothing about its feed.
    outcome = gdelt.fetch_article(ctx.session, get_store(), payload["url"], payload["source_id"])
    log.info(
        "article %s: %s",
        payload["url"],
        outcome.status,
        extra={"job_id": ctx.job.id},
    )
