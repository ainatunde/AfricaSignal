"""initial

Revision ID: 0001
Revises:
Create Date: 2026-09-30 13:41:36.378718
"""

import geoalchemy2
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    postgresql.ENUM("queued", "running", "done", "failed", "dead", name="job_status").create(
        op.get_bind(), checkfirst=False
    )
    postgresql.ENUM("admin", "editor", name="operator_role").create(op.get_bind(), checkfirst=False)
    postgresql.ENUM("email_login", "email_digest", "email_correction", name="outbox_kind").create(
        op.get_bind(), checkfirst=False
    )
    postgresql.ENUM("pending", "sent", "failed", "dead", name="outbox_status").create(
        op.get_bind(), checkfirst=False
    )
    postgresql.ENUM(
        "country", "state", "lga", "city", "neighbourhood_alias", name="place_kind"
    ).create(op.get_bind(), checkfirst=False)
    postgresql.ENUM(
        "primary_document",
        "official_dataset",
        "outlet_report",
        "wire_report",
        "unknown",
        name="origin_kind",
    ).create(op.get_bind(), checkfirst=False)
    postgresql.ENUM(
        "official_statistics",
        "regulator",
        "government",
        "company",
        "news_outlet",
        "aggregator",
        name="source_kind",
    ).create(op.get_bind(), checkfirst=False)
    postgresql.ENUM(
        "nbs", "nerc", "price_announcement", "rss", "gdelt", name="source_adapter"
    ).create(op.get_bind(), checkfirst=False)
    postgresql.ENUM("healthy", "degraded", "failing", name="source_health").create(
        op.get_bind(), checkfirst=False
    )
    postgresql.ENUM("active", "withdrawn", "expired", name="evidence_status").create(
        op.get_bind(), checkfirst=False
    )
    postgresql.ENUM("energy", "food", name="topic").create(op.get_bind(), checkfirst=False)
    postgresql.ENUM("monthly", "adhoc", name="series_frequency").create(
        op.get_bind(), checkfirst=False
    )
    postgresql.ENUM("price_series", "policy", name="situation_kind").create(
        op.get_bind(), checkfirst=False
    )
    postgresql.ENUM("active", "dormant", "closed", name="situation_status").create(
        op.get_bind(), checkfirst=False
    )
    postgresql.ENUM("T1_price_change", "T2_policy_change", name="assessment_template").create(
        op.get_bind(), checkfirst=False
    )
    postgresql.ENUM(
        "draft",
        "published",
        "withheld",
        "stale",
        "superseded",
        "withdrawn",
        name="assessment_status",
    ).create(op.get_bind(), checkfirst=False)
    postgresql.ENUM(
        "reported", "corroborated", "disputed", "insufficient", name="evidence_state"
    ).create(op.get_bind(), checkfirst=False)
    postgresql.ENUM("none", "low", "medium", "high", name="severity").create(
        op.get_bind(), checkfirst=False
    )
    postgresql.ENUM("price_statement", "policy_statement", "other", name="claim_type").create(
        op.get_bind(), checkfirst=False
    )
    postgresql.ENUM("up", "down", "unchanged", "unknown", name="claim_direction").create(
        op.get_bind(), checkfirst=False
    )
    postgresql.ENUM("day", "month", "year", "unknown", name="time_precision").create(
        op.get_bind(), checkfirst=False
    )
    postgresql.ENUM("national", "state", "lga", "city", "unknown", name="place_precision").create(
        op.get_bind(), checkfirst=False
    )
    postgresql.ENUM(
        "measurement", "claim", "evidence_document", name="assessment_input_kind"
    ).create(op.get_bind(), checkfirst=False)
    postgresql.ENUM("useful_yes", "useful_no", "error_report", name="feedback_kind").create(
        op.get_bind(), checkfirst=False
    )
    postgresql.ENUM(
        "received",
        "triaged",
        "investigating",
        "resolved_updated",
        "resolved_no_change",
        "resolved_insufficient",
        "closed",
        name="feedback_status",
    ).create(op.get_bind(), checkfirst=False)
    postgresql.ENUM("new_version", "correction", "withdrawal", name="notification_kind").create(
        op.get_bind(), checkfirst=False
    )

    op.create_table(
        "app_user",
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("digest_opt_in", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("digest_opt_in_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_user")),
        sa.UniqueConstraint("email", name=op.f("uq_app_user_email")),
    )
    op.create_table(
        "job",
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("dedupe_key", sa.Text(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "queued", "running", "done", "failed", "dead", name="job_status", create_type=False
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column(
            "run_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="5", nullable=False),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_job_dedupe_key")),
    )
    op.create_index("ix_job_status_run_at", "job", ["status", "run_at"], unique=False)
    op.create_table(
        "operator",
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("totp_secret_enc", sa.Text(), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM("admin", "editor", name="operator_role", create_type=False),
            nullable=False,
        ),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operator")),
        sa.UniqueConstraint("email", name=op.f("uq_operator_email")),
    )
    op.create_table(
        "outbox",
        sa.Column(
            "kind",
            postgresql.ENUM(
                "email_login",
                "email_digest",
                "email_correction",
                name="outbox_kind",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "pending", "sent", "failed", "dead", name="outbox_status", create_type=False
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("provider_message_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_outbox_dedupe_key")),
    )
    op.create_table(
        "place",
        sa.Column(
            "kind",
            postgresql.ENUM(
                "country",
                "state",
                "lga",
                "city",
                "neighbourhood_alias",
                name="place_kind",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("code", sa.Text(), nullable=True),
        sa.Column("parent_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "geom",
            geoalchemy2.types.Geometry(
                geometry_type="MULTIPOLYGON",
                srid=4326,
                dimension=2,
                from_text="ST_GeomFromEWKT",
                name="geometry",
            ),
            nullable=True,
        ),
        sa.Column(
            "point",
            geoalchemy2.types.Geometry(
                geometry_type="POINT",
                srid=4326,
                dimension=2,
                from_text="ST_GeomFromEWKT",
                name="geometry",
            ),
            nullable=True,
        ),
        sa.Column("boundary_version", sa.Text(), nullable=True),
        sa.Column("population", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(["parent_id"], ["place.id"], name=op.f("fk_place_parent_id_place")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_place")),
        sa.UniqueConstraint("code", name=op.f("uq_place_code")),
    )
    op.create_table(
        "reporting_origin",
        sa.Column(
            "kind",
            postgresql.ENUM(
                "primary_document",
                "official_dataset",
                "outlet_report",
                "wire_report",
                "unknown",
                name="origin_kind",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reporting_origin")),
    )
    op.create_table(
        "setting",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_setting")),
    )
    op.create_table(
        "source",
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "kind",
            postgresql.ENUM(
                "official_statistics",
                "regulator",
                "government",
                "company",
                "news_outlet",
                "aggregator",
                name="source_kind",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "adapter",
            postgresql.ENUM(
                "nbs",
                "nerc",
                "price_announcement",
                "rss",
                "gdelt",
                name="source_adapter",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("home_url", sa.Text(), nullable=True),
        sa.Column("feed_url", sa.Text(), nullable=True),
        sa.Column("owner", sa.Text(), nullable=True),
        sa.Column("languages", sa.ARRAY(sa.Text()), server_default="{en}", nullable=False),
        sa.Column("coverage_note", sa.Text(), nullable=True),
        sa.Column("schedule_minutes", sa.Integer(), nullable=False),
        sa.Column(
            "next_due_at", sa.DateTime(timezone=True), server_default="now()", nullable=False
        ),
        sa.Column("max_requests_per_hour", sa.Integer(), server_default="60", nullable=False),
        sa.Column("active", sa.Boolean(), server_default="true", nullable=False),
        sa.Column(
            "health",
            postgresql.ENUM(
                "healthy", "degraded", "failing", name="source_health", create_type=False
            ),
            server_default="healthy",
            nullable=False,
        ),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source")),
        sa.UniqueConstraint("slug", name=op.f("uq_source_slug")),
    )
    op.create_table(
        "audit_log",
        sa.Column("operator_id", sa.BigInteger(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_kind", sa.Text(), nullable=False),
        sa.Column("target_id", sa.BigInteger(), nullable=True),
        sa.Column("before", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("after", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["operator_id"], ["operator.id"], name=op.f("fk_audit_log_operator_id_operator")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_table(
        "evidence_document",
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("mime", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("text_content", sa.Text(), nullable=True),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), server_default="en", nullable=False),
        sa.Column("simhash", sa.BigInteger(), nullable=True),
        sa.Column("origin_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "active", "withdrawn", "expired", name="evidence_status", create_type=False
            ),
            server_default="active",
            nullable=False,
        ),
        sa.Column("withdrawn_reason", sa.Text(), nullable=True),
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["origin_id"],
            ["reporting_origin.id"],
            name=op.f("fk_evidence_document_origin_id_reporting_origin"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["source.id"], name=op.f("fk_evidence_document_source_id_source")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evidence_document")),
        sa.UniqueConstraint(
            "source_id",
            "canonical_url",
            "content_sha256",
            name=op.f("uq_evidence_document_source_id"),
        ),
    )
    op.create_table(
        "llm_call",
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=10, scale=5), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("cache_hit", sa.Boolean(), server_default="false", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["job.id"], name=op.f("fk_llm_call_job_id_job")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_call")),
    )
    op.create_table(
        "login_token",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("token_sha256", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name=op.f("fk_login_token_user_id_app_user")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_login_token")),
    )
    op.create_table(
        "place_alias",
        sa.Column("place_id", sa.BigInteger(), nullable=False),
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("alias_norm", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["place_id"], ["place.id"], name=op.f("fk_place_alias_place_id_place")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_place_alias")),
    )
    op.create_index("ix_place_alias_alias_norm", "place_alias", ["alias_norm"], unique=False)
    op.create_table(
        "preference",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("place_ids", sa.ARRAY(sa.BigInteger()), server_default="{}", nullable=False),
        sa.Column("topics", sa.ARRAY(sa.Text()), server_default="{}", nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name=op.f("fk_preference_user_id_app_user")
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_preference")),
    )
    op.create_table(
        "series",
        sa.Column("item_code", sa.Text(), nullable=False),
        sa.Column(
            "topic",
            postgresql.ENUM("energy", "food", name="topic", create_type=False),
            nullable=False,
        ),
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("currency", sa.Text(), server_default="NGN", nullable=False),
        sa.Column(
            "frequency",
            postgresql.ENUM("monthly", "adhoc", name="series_frequency", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_id"], ["source.id"], name=op.f("fk_series_source_id_source")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_series")),
        sa.UniqueConstraint("item_code", "source_id", name=op.f("uq_series_item_code")),
    )
    op.create_table(
        "session",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("token_sha256", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name=op.f("fk_session_user_id_app_user")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session")),
    )
    op.create_table(
        "situation",
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column(
            "kind",
            postgresql.ENUM("price_series", "policy", name="situation_kind", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "topic",
            postgresql.ENUM("energy", "food", name="topic", create_type=False),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("item_code", sa.Text(), nullable=True),
        sa.Column("policy_series", sa.Text(), nullable=True),
        sa.Column("place_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "active", "dormant", "closed", name="situation_status", create_type=False
            ),
            server_default="active",
            nullable=False,
        ),
        sa.Column("current_version_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["place_id"], ["place.id"], name=op.f("fk_situation_place_id_place")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_situation")),
        sa.UniqueConstraint("slug", name=op.f("uq_situation_slug")),
    )
    op.create_table(
        "source_permission",
        sa.Column("source_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("may_collect", sa.Boolean(), nullable=False),
        sa.Column("may_store_full_text", sa.Boolean(), nullable=False),
        sa.Column("max_quote_chars", sa.Integer(), nullable=True),
        sa.Column("may_republish_numbers", sa.Boolean(), nullable=False),
        sa.Column("link_required", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("retention_days", sa.Integer(), nullable=True),
        sa.Column("terms_url", sa.Text(), nullable=True),
        sa.Column("terms_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terms_snapshot_sha256", sa.Text(), nullable=True),
        sa.Column("rights_basis", sa.Text(), nullable=True),
        sa.Column("approved_by_operator_id", sa.BigInteger(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["approved_by_operator_id"],
            ["operator.id"],
            name=op.f("fk_source_permission_approved_by_operator_id_operator"),
        ),
        sa.ForeignKeyConstraint(
            ["source_id"], ["source.id"], name=op.f("fk_source_permission_source_id_source")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_permission")),
        sa.UniqueConstraint("source_id", "version", name=op.f("uq_source_permission_source_id")),
    )
    op.create_table(
        "assessment_version",
        sa.Column("situation_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "template",
            postgresql.ENUM(
                "T1_price_change", "T2_policy_change", name="assessment_template", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("template_version", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column("prompt_version", sa.Text(), nullable=True),
        sa.Column("inputs_hash", sa.Text(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "draft",
                "published",
                "withheld",
                "stale",
                "superseded",
                "withdrawn",
                name="assessment_status",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "evidence_state",
            postgresql.ENUM(
                "reported",
                "corroborated",
                "disputed",
                "insufficient",
                name="evidence_state",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "severity",
            postgresql.ENUM("none", "low", "medium", "high", name="severity", create_type=False),
            nullable=False,
        ),
        sa.Column("headline", sa.Text(), nullable=False),
        sa.Column(
            "facts", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column(
            "possible_factors",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column(
            "unknowns", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.Column("scope_label", sa.Text(), nullable=False),
        sa.Column("period_label", sa.Text(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("change_summary", sa.Text(), nullable=True),
        sa.Column(
            "withheld_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("supersedes_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["situation_id"],
            ["situation.id"],
            name=op.f("fk_assessment_version_situation_id_situation"),
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_id"],
            ["assessment_version.id"],
            name=op.f("fk_assessment_version_supersedes_id_assessment_version"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assessment_version")),
        sa.UniqueConstraint(
            "situation_id", "version", name=op.f("uq_assessment_version_situation_id")
        ),
    )
    op.create_foreign_key(
        "fk_situation_current_version",
        "situation",
        "assessment_version",
        ["current_version_id"],
        ["id"],
    )
    op.create_table(
        "claim",
        sa.Column("evidence_document_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "claim_type",
            postgresql.ENUM(
                "price_statement", "policy_statement", "other", name="claim_type", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("passage", sa.Text(), nullable=False),
        sa.Column("passage_start", sa.Integer(), nullable=True),
        sa.Column("passage_end", sa.Integer(), nullable=True),
        sa.Column("item_code", sa.Text(), nullable=True),
        sa.Column("policy_series", sa.Text(), nullable=True),
        sa.Column("stated_value", sa.Numeric(), nullable=True),
        sa.Column("stated_unit", sa.Text(), nullable=True),
        sa.Column(
            "direction",
            postgresql.ENUM(
                "up", "down", "unchanged", "unknown", name="claim_direction", create_type=False
            ),
            server_default="unknown",
            nullable=False,
        ),
        sa.Column("occurred_from", sa.Date(), nullable=True),
        sa.Column("occurred_to", sa.Date(), nullable=True),
        sa.Column(
            "time_precision",
            postgresql.ENUM(
                "day", "month", "year", "unknown", name="time_precision", create_type=False
            ),
            server_default="unknown",
            nullable=False,
        ),
        sa.Column(
            "place_candidates",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="[]",
            nullable=False,
        ),
        sa.Column("place_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "place_precision",
            postgresql.ENUM(
                "national",
                "state",
                "lga",
                "city",
                "unknown",
                name="place_precision",
                create_type=False,
            ),
            server_default="unknown",
            nullable=False,
        ),
        sa.Column("extractor_version", sa.Text(), nullable=False),
        sa.Column("valid", sa.Boolean(), nullable=False),
        sa.Column("invalid_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["evidence_document_id"],
            ["evidence_document.id"],
            name=op.f("fk_claim_evidence_document_id_evidence_document"),
        ),
        sa.ForeignKeyConstraint(["place_id"], ["place.id"], name=op.f("fk_claim_place_id_place")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim")),
    )
    op.create_table(
        "event",
        sa.Column(
            "ts", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("anon_id", sa.Text(), nullable=True),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("situation_id", sa.BigInteger(), nullable=True),
        sa.Column("ref", sa.Text(), nullable=True),
        sa.Column(
            "props", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["situation_id"], ["situation.id"], name=op.f("fk_event_situation_id_situation")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name=op.f("fk_event_user_id_app_user")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event")),
    )
    op.create_index("ix_event_name_ts", "event", ["name", "ts"], unique=False)
    op.create_table(
        "follow",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("situation_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["situation_id"], ["situation.id"], name=op.f("fk_follow_situation_id_situation")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name=op.f("fk_follow_user_id_app_user")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_follow")),
        sa.UniqueConstraint("user_id", "situation_id", name=op.f("uq_follow_user_id")),
    )
    op.create_table(
        "gdelt_discovery",
        sa.Column("global_event_id", sa.BigInteger(), nullable=False),
        sa.Column("mention_identifier", sa.Text(), nullable=False),
        sa.Column("mention_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action_geo_country", sa.Text(), nullable=True),
        sa.Column("action_geo_adm1", sa.Text(), nullable=True),
        sa.Column("event_root_code", sa.Text(), nullable=True),
        sa.Column("evidence_document_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["evidence_document_id"],
            ["evidence_document.id"],
            name=op.f("fk_gdelt_discovery_evidence_document_id_evidence_document"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_gdelt_discovery")),
        sa.UniqueConstraint(
            "global_event_id", "mention_identifier", name=op.f("uq_gdelt_discovery_global_event_id")
        ),
    )
    op.create_table(
        "measurement",
        sa.Column("series_id", sa.BigInteger(), nullable=False),
        sa.Column("place_id", sa.BigInteger(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("value", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("vintage", sa.Date(), nullable=False),
        sa.Column("evidence_document_id", sa.BigInteger(), nullable=False),
        sa.Column("superseded_by_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["evidence_document_id"],
            ["evidence_document.id"],
            name=op.f("fk_measurement_evidence_document_id_evidence_document"),
        ),
        sa.ForeignKeyConstraint(
            ["place_id"], ["place.id"], name=op.f("fk_measurement_place_id_place")
        ),
        sa.ForeignKeyConstraint(
            ["series_id"], ["series.id"], name=op.f("fk_measurement_series_id_series")
        ),
        sa.ForeignKeyConstraint(
            ["superseded_by_id"],
            ["measurement.id"],
            name=op.f("fk_measurement_superseded_by_id_measurement"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_measurement")),
        sa.UniqueConstraint(
            "series_id",
            "place_id",
            "period_start",
            "vintage",
            name=op.f("uq_measurement_series_id"),
        ),
    )
    op.create_table(
        "assessment_input",
        sa.Column("assessment_version_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "input_kind",
            postgresql.ENUM(
                "measurement",
                "claim",
                "evidence_document",
                name="assessment_input_kind",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("input_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["assessment_version_id"],
            ["assessment_version.id"],
            name=op.f("fk_assessment_input_assessment_version_id_assessment_version"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assessment_input")),
    )
    op.create_index(
        "ix_assessment_input_kind_id", "assessment_input", ["input_kind", "input_id"], unique=False
    )
    op.create_table(
        "feedback",
        sa.Column("assessment_version_id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=True),
        sa.Column("anon_id", sa.Text(), nullable=True),
        sa.Column(
            "kind",
            postgresql.ENUM(
                "useful_yes", "useful_no", "error_report", name="feedback_kind", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("text", sa.Text(), nullable=True),
        sa.Column("contact_email", sa.Text(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "received",
                "triaged",
                "investigating",
                "resolved_updated",
                "resolved_no_change",
                "resolved_insufficient",
                "closed",
                name="feedback_status",
                create_type=False,
            ),
            server_default="received",
            nullable=False,
        ),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["assessment_version_id"],
            ["assessment_version.id"],
            name=op.f("fk_feedback_assessment_version_id_assessment_version"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name=op.f("fk_feedback_user_id_app_user")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feedback")),
    )
    op.create_table(
        "notification",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("assessment_version_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "kind",
            postgresql.ENUM(
                "new_version",
                "correction",
                "withdrawal",
                name="notification_kind",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.ForeignKeyConstraint(
            ["assessment_version_id"],
            ["assessment_version.id"],
            name=op.f("fk_notification_assessment_version_id_assessment_version"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["app_user.id"], name=op.f("fk_notification_user_id_app_user")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_notification_dedupe_key")),
    )


def downgrade() -> None:
    op.drop_table("notification")
    op.drop_table("feedback")
    op.drop_index("ix_assessment_input_kind_id", table_name="assessment_input")
    op.drop_table("assessment_input")
    op.drop_table("measurement")
    op.drop_table("gdelt_discovery")
    op.drop_table("follow")
    op.drop_index("ix_event_name_ts", table_name="event")
    op.drop_table("event")
    op.drop_table("claim")
    op.drop_constraint("fk_situation_current_version", "situation", type_="foreignkey")
    op.drop_table("assessment_version")
    op.drop_table("source_permission")
    op.drop_table("situation")
    op.drop_table("session")
    op.drop_table("series")
    op.drop_table("preference")
    op.drop_index("ix_place_alias_alias_norm", table_name="place_alias")
    op.drop_table("place_alias")
    op.drop_table("login_token")
    op.drop_table("llm_call")
    op.drop_table("evidence_document")
    op.drop_table("audit_log")
    op.drop_table("source")
    op.drop_table("setting")
    op.drop_table("reporting_origin")
    op.drop_table("place")
    op.drop_table("outbox")
    op.drop_table("operator")
    op.drop_index("ix_job_status_run_at", table_name="job")
    op.drop_table("job")
    op.drop_table("app_user")
    postgresql.ENUM(name="notification_kind").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="feedback_status").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="feedback_kind").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="assessment_input_kind").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="place_precision").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="time_precision").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="claim_direction").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="claim_type").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="severity").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="evidence_state").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="assessment_status").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="assessment_template").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="situation_status").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="situation_kind").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="series_frequency").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="topic").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="evidence_status").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="source_health").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="source_adapter").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="source_kind").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="origin_kind").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="place_kind").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="outbox_status").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="outbox_kind").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="operator_role").drop(op.get_bind(), checkfirst=False)
    postgresql.ENUM(name="job_status").drop(op.get_bind(), checkfirst=False)
    # Extensions are left installed: other databases objects may depend on them.
