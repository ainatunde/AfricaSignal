"""Machine-readable inventory of console, code-owned and deployment-owned controls."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from africasignal import settings_store
from africasignal.jobs import handlers as handler_registry
from africasignal.jobs.policy import (
    CONTENT_JOB_KINDS,
    EXTERNAL_AGENT_JOB_KINDS,
    LLM_JOB_KINDS,
    REACH_JOB_KINDS,
)
from africasignal.operations import workloads

SETTING_CONSUMERS = {
    "anthropic_api_key": "africasignal.llm.adapter.RoutedProvider",
    "openai_api_key": "africasignal.llm.adapter.RoutedProvider",
    "llm_evidence_context_enabled": "africasignal.assess.explain.build_input",
    "editorial_drafting_enabled": "africasignal.jobs.handlers.editorial_draft",
    "llm_route_editorial_draft": "africasignal.llm.adapter.LlmAdapter.route_for",
    "agent_reach_endpoint": "africasignal.agent_reach.client.AgentReachClient",
    "agent_reach_api_key": "africasignal.agent_reach.client.AgentReachClient",
    "agent_reach_max_tasks_per_day": "africasignal.operations.agent_reach.submit",
    "gdelt_poll_minutes": "africasignal.jobs.scheduler.tick",
    "gdelt_windows_per_poll": "africasignal.jobs.handlers.gdelt_poll",
    "gdelt_max_lookback_hours": "africasignal.jobs.handlers.gdelt_poll",
    "llm_route_claim_extract": "africasignal.llm.adapter.LlmAdapter.route_for",
    "llm_route_explain": "africasignal.llm.adapter.LlmAdapter.route_for",
    "llm_daily_budget_usd": "africasignal.llm.adapter.LlmAdapter.daily_budget_usd",
    "llm_per_job_max_tokens": "africasignal.llm.adapter.LlmAdapter.per_job_max_tokens",
    "public_base_url": "africasignal.publish.email and website renderers",
    "operator_name": "africasignal.web.routes.legal",
    "contact_email": "africasignal.web.routes.legal and crawler page",
    "legal_review_confirmed": "africasignal.web.routes.legal",
    "trusted_proxy_hops": "africasignal.web.client_address",
    "email_provider": "africasignal.publish.email",
    "email_api_key": "africasignal.publish.email",
    "email_from": "africasignal.publish.email",
    "x_user_access_token": "africasignal.publish.x_api.publish_post",
    "x_publishing_enabled": "africasignal.publish.social.approve and dispatch",
    "facebook_publishing_enabled": "africasignal.publish.social.approve and dispatch",
    "instagram_publishing_enabled": "africasignal.publish.social.approve and dispatch",
    "telegram_publishing_enabled": "africasignal.publish.social.approve and dispatch",
    "youtube_publishing_enabled": "africasignal.publish.social.approve and dispatch",
    "social_media_retention_days": "africasignal.publish.social.dispatch cleanup",
    "x_daily_post_limit": "africasignal.publish.social.approve and dispatch",
    "facebook_page_id": "africasignal.publish.social.dispatch",
    "facebook_page_access_token": "africasignal.publish.social.dispatch",
    "instagram_professional_account_id": "africasignal.publish.social.dispatch",
    "instagram_access_token": "africasignal.publish.social.dispatch",
    "meta_graph_api_version": "africasignal.publish.social.dispatch",
    "telegram_channel_id": "africasignal.publish.social.dispatch",
    "telegram_bot_token": "africasignal.publish.social.dispatch",
    "youtube_channel_id": "africasignal.publish.social.dispatch",
    "youtube_oauth_client_id": "africasignal.publish.social.dispatch",
    "youtube_oauth_client_secret": "africasignal.publish.social.dispatch",
    "youtube_refresh_token": "africasignal.publish.social.dispatch",
    "youtube_video_privacy": "africasignal.publish.social.dispatch",
    "youtube_category_id": "africasignal.publish.social.dispatch",
    "facebook_daily_post_limit": "africasignal.publish.social.approve and dispatch",
    "instagram_daily_post_limit": "africasignal.publish.social.approve and dispatch",
    "telegram_daily_post_limit": "africasignal.publish.social.approve and dispatch",
    "youtube_daily_post_limit": "africasignal.publish.social.approve and dispatch",
    "x_app_bearer_token": "africasignal.publish.x_search.search_recent",
    "x_listening_enabled": "africasignal.jobs.scheduler.tick and social_listening.poll_queries",
    "x_listening_poll_minutes": "africasignal.operations.social_listening.poll_queries",
    "x_listening_daily_read_cap": "africasignal.operations.social_listening.poll_queries",
    "weekly_digest_weekday": "africasignal.jobs.scheduler.tick",
    "weekly_digest_hour": "africasignal.jobs.scheduler.tick",
    "s3_endpoint_url": "africasignal.storage",
    "s3_bucket": "africasignal.storage",
    "s3_access_key_id": "africasignal.storage",
    "s3_secret_access_key": "africasignal.storage",
    "backup_s3_endpoint_url": "backup host scripts",
    "backup_s3_bucket": "backup host scripts",
    "backup_s3_access_key_id": "backup host scripts",
    "backup_s3_secret_access_key": "backup host scripts",
    "backup_s3_prefix": "backup host scripts",
    "backup_retain_days": "backup host scripts",
    "feedback_retention_months": "africasignal.publish.retention",
    "backup_max_age_hours": "africasignal.backup_alerts",
}


JOB_OWNERS = {
    "extract_claims": ("AI workload", "AI switch, schedule and provider route"),
    "explain_version": ("AI workload", "AI switch, schedule and provider route"),
    "compose_editorial_draft": (
        "Draft-only editorial queue",
        "Default-off switch; LlmAdapter budget and token limit; operator review; never publishes",
    ),
    "explain_backfill": ("AI workload", "AI switch, schedule and provider route"),
    "agent_reach_search": (
        "Agent Reach workload",
        "Default-off switch, schedule and runner contract",
    ),
    "agent_reach_poll": (
        "Agent Reach workload",
        "Default-off switch, schedule and runner contract",
    ),
    "external_agent_submit": (
        "External-agent workload",
        "Default-off switch, schedule, profile quota and local cost reservation",
    ),
    "external_agent_poll": (
        "External-agent lifecycle",
        "Continuous status polling; no new task admission",
    ),
    "external_agent_cancel": (
        "External-agent lifecycle",
        "Continuous remote cancellation and outcome visibility",
    ),
    "external_agent_reconcile": (
        "External-agent lifecycle",
        "Idempotency lookup before retrying uncertain submissions",
    ),
    "external_agent_expire": (
        "External-agent cleanup",
        "Daily task and result retention",
    ),
    "process_document": (
        "Document processing workload",
        "Switch and schedule; capture/OCR remain coupled",
    ),
    "gdelt_fetch_article": (
        "Document processing workload",
        "Switch and schedule; source permission is rechecked",
    ),
    "import_nbs_file": (
        "Document processing workload",
        "Switch and schedule; upload is admin initiated",
    ),
    "resolve_places": ("Document processing workload", "Switch and schedule"),
    "fetch_source": ("Source registry", "Source active state, interval and request ceiling"),
    "gdelt_poll": (
        "Sources and discovery settings",
        "Poll interval, per-poll windows and replay lookback",
    ),
    "weekly_digest": ("Email settings", "Configured weekday/hour in Africa/Lagos"),
    "dispatch_outbox": (
        "Code-owned delivery service",
        "One-minute dispatch; sign-in stays prompt, while correction, digest and reviewed-insight "
        "mail follow publication gates",
    ),
    "dispatch_social_publication": (
        "Operator-approved social publishing",
        "One-minute dispatch; per-platform caps, publication suspension, outcome reconciliation, "
        "and media retention",
    ),
    "social_listen_poll": (
        "X social listening",
        "Default-off switch, per-query cadence, daily read reservation and ID-only retention",
    ),
    "social_listen_expire": (
        "X social listening privacy cleanup",
        "Daily deletion of expired post IDs and poll ledger retention",
    ),
    "mirror_deletions": (
        "Code-owned privacy service",
        "Deletion mirroring is deadline-driven and continuous",
    ),
    "apply_retention": (
        "Privacy settings",
        "Daily job reads feedback retention; deletion deadlines remain continuous",
    ),
    "purge_expired_evidence": (
        "Code-owned privacy service",
        "Delete content after the permitted retention window",
    ),
    "discard_import_upload": ("Code-owned cleanup", "Temporary upload expiry is deadline-driven"),
    "agent_reach_reconcile": (
        "Agent Reach cleanup",
        "Continuous lookup before any retry of an uncertain submission",
    ),
    "agent_reach_cancel": (
        "Agent Reach cleanup",
        "Continuous cancellation and remote-outcome visibility",
    ),
    "agent_reach_deadline": ("Agent Reach cleanup", "Continuous 30-minute deadline enforcement"),
    "agent_reach_expire": ("Agent Reach cleanup", "Daily candidate/task retention"),
    "check_backups": (
        "Backup monitoring",
        "Continuous schedule; host owns dump execution and restore",
    ),
    "check_health": (
        "Application health monitoring",
        "Runs every 15 minutes; alert destinations are deployment-owned",
    ),
    "expire_assessments": ("Publication safety", "Hourly expiry remains deadline-driven"),
    "release_held_versions": (
        "Publication safety",
        "Minute-level release policy remains deadline-driven",
    ),
    "prune_events": ("Privacy retention", "Daily event retention job"),
    "assess_situation": ("Assessment pipeline", "Triggered when evidence or claims change"),
    "invalidate": ("Publication safety", "Triggered invalidation runs promptly"),
    "notify_followers": (
        "Publication safety",
        "Notification work follows publication policy and suppression",
    ),
}


BUSINESS_CONTROLS = [
    {
        "name": "Source permissions and pause/resume",
        "owner": "Source registry and versioned permission service",
        "control": "/admin/sources/{id}",
        "apply_timing": "Next acquisition; capture rechecks active permission",
        "scope": "Collection rights, owner, source active state and permission review date",
    },
    {
        "name": "Source interval and per-source request ceiling",
        "owner": "Source registry runtime pacing override",
        "control": "/admin/sources/{id} -> Collection pacing",
        "apply_timing": "Interval applies after due reset; request ceiling applies on next fetch",
        "scope": "Per-source bounds; shared-domain pacing across replicas is not enforced",
    },
    {
        "name": "AI providers, credentials, routes and spend bounds",
        "owner": "LLM adapter and reviewed model catalogue",
        "control": "/admin/settings#llm",
        "apply_timing": "Next uncached model call",
        "scope": "Anthropic/OpenAI keys, purpose routes, daily USD budget and per-job token limit",
    },
    {
        "name": "External-agent integrations",
        "owner": "External-agent HTTPS profile and task protocol",
        "control": "/admin/agents and /admin/automation",
        "apply_timing": "New task admission; polling and cancellation stay continuous",
        "scope": (
            "Default-off profiles, encrypted credentials, purposes, domains, bounded output "
            "and cost reservations"
        ),
    },
    {
        "name": "Agent Reach discovery and candidate review",
        "owner": "Isolated external runner bridge",
        "control": "/admin/agent-reach and /admin/automation",
        "apply_timing": "New task admission; candidates flow through approved RSS capture",
        "scope": "Default-off, scheduled, bounded search with cancellation, review and retention",
    },
    {
        "name": "Draft-only editorial synthesis",
        "owner": "Accounted LLM adapter and editorial review queue",
        "control": "/admin/insights and /admin/settings#llm",
        "apply_timing": "New published/held assessments; approval only records review",
        "scope": (
            "Private version-linked drafts, source freshness checks, audited decisions; "
            "no direct publication, email, or social authority"
        ),
    },
    {
        "name": "X listening and operator-approved social publishing",
        "owner": "X Recent Search lead inbox and durable social publication queue",
        "control": (
            "/admin/social-listening, /admin/channel-posts and /admin/settings#social_publishing"
        ),
        "apply_timing": (
            "Listening settings apply next poll; publishing requires approval and dispatch"
        ),
        "scope": (
            "Listening is default-off, metered, and retains only expiring post IDs; leads are "
            "unverified and never evidence. X publishing has a daily cap and never auto-publishes."
        ),
    },
    {
        "name": "Publication suspend, release, withdraw and withhold",
        "owner": "Publication policy service",
        "control": "/admin/publication and /admin/assessments",
        "apply_timing": "Immediate; publication suspension remains distinct from workload switches",
        "scope": "No model or agent capability can publish directly",
    },
    {
        "name": "Policy series",
        "owner": "Versioned operator policy registry",
        "control": "/admin/policies",
        "apply_timing": "New assessments; structural catalogue changes remain reviewed imports",
        "scope": "Supported series, topics and primary sources",
    },
    {
        "name": "GDELT/source discovery cadence",
        "owner": "Sources and discovery settings",
        "control": "/admin/settings#sources and /admin/domains",
        "apply_timing": "Next scheduler tick/poll",
        "scope": "GDELT cadence/lookback bounds; new domains remain inactive until rights review",
    },
    {
        "name": "Evidence storage and backup settings",
        "owner": "Storage client and backup host scripts",
        "control": "/admin/settings#storage and #backup",
        "apply_timing": "Next use; bucket migration and backup schedules are infrastructure-owned",
        "scope": "Console holds settings; infrastructure owns bucket migration, dump and restore",
    },
    {
        "name": "Reader identity, operator bootstrap, database and encryption root",
        "owner": "Application security and deployment",
        "control": "Code, host CLI and environment; not web-editable",
        "apply_timing": "Reviewed release or infrastructure change",
        "scope": "Roles, bootstrap, DB, root key, trusted origins and TLS stay host-managed",
    },
]


DEPLOYMENT_OWNED = [
    {
        "name": "DATABASE_URL, SECRET_KEY and ENV",
        "owner": "secret manager/deployment",
        "reason": "Needed before database access; protect encrypted console secrets.",
    },
    {
        "name": "ADMIN_TRUSTED_ORIGINS and TLS termination",
        "owner": "edge/deployment",
        "reason": "Trust boundary for authenticated browser actions.",
    },
    {
        "name": "External-agent deny switch (EXTERNAL_AGENTS_DENY)",
        "owner": "application deployment",
        "reason": (
            "Blocks all external-agent task admissions regardless of dashboard profile or "
            "workload settings."
        ),
    },
    {
        "name": "Agent Reach deny switch (AGENT_REACH_DENY)",
        "owner": "application deployment",
        "reason": "Deployment block: fails closed before admission and cancels known remote work.",
    },
    {
        "name": "Agent Reach runner and provider deployment",
        "owner": "isolated runner deployment",
        "reason": "Image pin, API key, bridge token, TLS and egress stay deployment-owned.",
    },
    {
        "name": "Worker CPU/RAM/process limits and scaling",
        "owner": "container/orchestrator",
        "reason": "Dashboard concurrency cannot enforce host CPU, RAM or process ceilings.",
    },
    {
        "name": "Shared-domain pacing across worker replicas",
        "owner": "network/deployment architecture",
        "reason": "Current politeness tokens are process-local.",
    },
    {
        "name": "Nightly backup execution, bucket account separation and restore drill",
        "owner": "backup host/scheduler and operator",
        "reason": "Console stores settings; the host owns backup execution and restore.",
    },
    {
        "name": "Independent heartbeat delivery target",
        "owner": "monitoring provider/deployment",
        "reason": "Monitor code is included; heartbeat targets and delivery are deployment-owned.",
    },
    {
        "name": "Provider/human sign-off: rights, privacy, editorial, accessibility and indexing",
        "owner": "provider and human acceptance",
        "reason": "These approvals require live provider evidence and accountable human review.",
    },
]


def manifest(session: Session) -> dict[str, Any]:
    """Return sanitized dashboard coverage, never secret values or queue payloads."""
    handler_registry.load_all()
    group_titles = dict(settings_store.GROUPS)
    settings = []
    settings_gaps: list[str] = []
    for key, definition in sorted(settings_store.REGISTRY.items()):
        resolved = settings_store.resolve(session, key)
        consumer = SETTING_CONSUMERS.get(key)
        if consumer is None:
            settings_gaps.append(key)
            consumer = "UNMAPPED: consumer ownership must be recorded"
        settings.append(
            {
                "key": key,
                "label": definition.label,
                "group": definition.group,
                "group_title": group_titles.get(definition.group, definition.group),
                "kind": definition.kind,
                "default": definition.default,
                "minimum": definition.minimum,
                "maximum": definition.maximum,
                "choices": list(definition.choices),
                "secret": definition.secret,
                "configured": resolved.value is not None,
                "effective_source": resolved.source,
                "environment_fallback": True,
                "console_route": f"/admin/settings#{definition.group}",
                "consumer": consumer,
                "apply_timing": "Read by its runtime consumer on the next call/tick",
                "editable": True,
            }
        )

    job_kinds = []
    job_gaps: list[str] = []
    for kind in sorted(handler_registry.HANDLERS):
        owner, control = JOB_OWNERS.get(
            kind, ("UNMAPPED", "Classification and runtime owner must be recorded")
        )
        if owner == "UNMAPPED":
            job_gaps.append(kind)
        if kind in LLM_JOB_KINDS:
            workload = "ai"
        elif kind in REACH_JOB_KINDS:
            workload = "agent_reach"
        elif kind in EXTERNAL_AGENT_JOB_KINDS:
            workload = "external_agents"
        elif kind in CONTENT_JOB_KINDS:
            workload = "processing"
        else:
            workload = None
        job_kinds.append(
            {
                "kind": kind,
                "owner": owner,
                "control": control,
                "workload": workload,
                "configuration": "/admin/automation"
                if workload
                else "See owner/control disposition",
                "registered_handler": True,
            }
        )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "settings": settings,
        "workloads": workloads.current(session),
        "business_controls": BUSINESS_CONTROLS,
        "job_kinds": job_kinds,
        "deployment_owned": DEPLOYMENT_OWNED,
        "coverage_gaps": {
            "unmapped_settings": settings_gaps,
            "unmapped_job_kinds": job_gaps,
        },
    }
