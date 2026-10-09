"""Compose one private editorial draft for an assessment version."""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from africasignal import audit, settings_store
from africasignal.assess.editorial import (
    PROMPT_VERSION,
    PURPOSE,
    EditorialDraftError,
    compose,
)
from africasignal.jobs import queue
from africasignal.jobs.handlers import JobContext, register
from africasignal.llm import (
    BudgetExhausted,
    ProviderRefused,
    SchemaValidationError,
    build_adapter,
)
from africasignal.llm.budget import defer_until_next_day

log = logging.getLogger("africasignal.editorial_draft")


def enqueue_editorial_draft(session: Session, version_id: int) -> int | None:
    if settings_store.get(session, "editorial_drafting_enabled") != "yes":
        return None
    return queue.enqueue(
        session,
        "compose_editorial_draft",
        {"assessment_version_id": version_id},
        dedupe_key=f"editorial_draft:{version_id}:{PROMPT_VERSION}",
    )


@register("compose_editorial_draft")
def compose_editorial_draft(ctx: JobContext) -> None:
    if settings_store.get(ctx.session, "editorial_drafting_enabled") != "yes":
        return
    adapter = build_adapter(ctx.session)
    route = adapter.route_for(PURPOSE)
    provider = route.partition("/")[0] if "/" in route else "anthropic"
    key_name = "openai_api_key" if provider == "openai" else "anthropic_api_key"
    if not settings_store.get(ctx.session, key_name):
        log.info("editorial draft skipped: selected provider key is not configured")
        return
    version_id = int(ctx.job.payload["assessment_version_id"])
    try:
        draft = compose(ctx.session, adapter, version_id, job_id=ctx.job.id)
    except (EditorialDraftError, ProviderRefused, SchemaValidationError) as exc:
        audit.record_system(
            ctx.session,
            "editorial_draft.refused",
            "assessment_version",
            version_id,
            after={"reason": str(exc)[:300]},
        )
        log.warning("editorial draft refused for assessment version %s", version_id)
        return
    except BudgetExhausted as exc:
        defer_until_next_day(
            ctx.session,
            "compose_editorial_draft",
            ctx.job.payload,
            dedupe_key=f"editorial_draft:{version_id}:budget",
            retry_at=exc.retry_at,
        )
        log.info("editorial draft deferred to %s", exc.retry_at.isoformat())
        return
    log.info(
        "editorial draft %s for assessment version %s",
        "created" if draft is not None else "skipped",
        version_id,
        extra={"job_id": ctx.job.id},
    )
