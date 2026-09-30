"""B3.1 Sources and permissions."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from africasignal.models.base import Base, CreatedMixin, pg_enum

source_kind = pg_enum(
    "source_kind",
    "official_statistics",
    "regulator",
    "government",
    "company",
    "news_outlet",
    "aggregator",
)
source_adapter = pg_enum("source_adapter", "nbs", "nerc", "price_announcement", "rss", "gdelt")
source_health = pg_enum("source_health", "healthy", "degraded", "failing")


class Source(CreatedMixin, Base):
    __tablename__ = "source"

    slug: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(source_kind, nullable=False)
    adapter: Mapped[str] = mapped_column(source_adapter, nullable=False)
    home_url: Mapped[str | None] = mapped_column(Text)
    feed_url: Mapped[str | None] = mapped_column(Text)
    owner: Mapped[str | None] = mapped_column(Text)
    languages: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{en}")
    coverage_note: Mapped[str | None] = mapped_column(Text)
    schedule_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    next_due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default="now()"
    )
    max_requests_per_hour: Mapped[int] = mapped_column(Integer, nullable=False, server_default="60")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    health: Mapped[str] = mapped_column(source_health, nullable=False, server_default="healthy")
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class SourcePermission(CreatedMixin, Base):
    """Versioned; the newest row with ``approved_at`` set is in force."""

    __tablename__ = "source_permission"
    __table_args__ = (UniqueConstraint("source_id", "version"),)

    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    may_collect: Mapped[bool] = mapped_column(Boolean, nullable=False)
    may_store_full_text: Mapped[bool] = mapped_column(Boolean, nullable=False)
    max_quote_chars: Mapped[int | None] = mapped_column(Integer)  # null = unlimited
    may_republish_numbers: Mapped[bool] = mapped_column(Boolean, nullable=False)
    link_required: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    retention_days: Mapped[int | None] = mapped_column(Integer)  # null = keep
    terms_url: Mapped[str | None] = mapped_column(Text)
    terms_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    terms_snapshot_sha256: Mapped[str | None] = mapped_column(Text)
    rights_basis: Mapped[str | None] = mapped_column(Text)
    approved_by_operator_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("operator.id")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
