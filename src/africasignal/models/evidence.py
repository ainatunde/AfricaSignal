"""B3.2 Evidence."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from africasignal.models.base import Base, CreatedMixin, pg_enum

origin_kind = pg_enum(
    "origin_kind", "primary_document", "official_dataset", "outlet_report", "wire_report", "unknown"
)
evidence_status = pg_enum("evidence_status", "active", "withdrawn", "expired")


class ReportingOrigin(CreatedMixin, Base):
    __tablename__ = "reporting_origin"

    kind: Mapped[str] = mapped_column(origin_kind, nullable=False)
    label: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EvidenceDocument(CreatedMixin, Base):
    __tablename__ = "evidence_document"
    __table_args__ = (UniqueConstraint("source_id", "canonical_url", "content_sha256"),)

    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_url: Mapped[str] = mapped_column(Text, nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    content_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)  # relative object key
    mime: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    # Author or wire credit as the feed gave it (untrusted text, capped). Used only to tell whether
    # two outlets are one voice (AS-027, security review S-08).
    byline: Mapped[str | None] = mapped_column(Text)
    text_content: Mapped[str | None] = mapped_column(Text)
    excerpt: Mapped[str | None] = mapped_column(Text)
    language: Mapped[str] = mapped_column(Text, nullable=False, server_default="en")
    simhash: Mapped[int | None] = mapped_column(BigInteger)
    origin_id: Mapped[int | None] = mapped_column(ForeignKey("reporting_origin.id"))
    status: Mapped[str] = mapped_column(evidence_status, nullable=False, server_default="active")
    withdrawn_reason: Mapped[str | None] = mapped_column(Text)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GdeltDiscovery(CreatedMixin, Base):
    __tablename__ = "gdelt_discovery"
    __table_args__ = (UniqueConstraint("global_event_id", "mention_identifier"),)

    global_event_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mention_identifier: Mapped[str] = mapped_column(Text, nullable=False)  # the source URL
    mention_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    action_geo_country: Mapped[str | None] = mapped_column(Text)
    action_geo_adm1: Mapped[str | None] = mapped_column(Text)
    event_root_code: Mapped[str | None] = mapped_column(Text)
    evidence_document_id: Mapped[int | None] = mapped_column(ForeignKey("evidence_document.id"))


__all__ = ["EvidenceDocument", "GdeltDiscovery", "ReportingOrigin"]
