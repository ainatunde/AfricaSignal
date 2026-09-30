"""B3.5 Claims."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Date, ForeignKey, Integer, Numeric, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from africasignal.models.base import Base, CreatedMixin, pg_enum

claim_type_enum = pg_enum("claim_type", "price_statement", "policy_statement", "other")
claim_direction_enum = pg_enum("claim_direction", "up", "down", "unchanged", "unknown")
time_precision_enum = pg_enum("time_precision", "day", "month", "year", "unknown")
place_precision_enum = pg_enum("place_precision", "national", "state", "lga", "city", "unknown")


class Claim(CreatedMixin, Base):
    __tablename__ = "claim"

    evidence_document_id: Mapped[int] = mapped_column(
        ForeignKey("evidence_document.id"), nullable=False
    )
    claim_type: Mapped[str] = mapped_column(claim_type_enum, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    passage: Mapped[str] = mapped_column(Text, nullable=False)  # exact quote from the document
    passage_start: Mapped[int | None] = mapped_column(Integer)
    passage_end: Mapped[int | None] = mapped_column(Integer)
    item_code: Mapped[str | None] = mapped_column(Text)
    policy_series: Mapped[str | None] = mapped_column(Text)
    stated_value: Mapped[Decimal | None] = mapped_column(Numeric)
    stated_unit: Mapped[str | None] = mapped_column(Text)
    direction: Mapped[str] = mapped_column(
        claim_direction_enum, nullable=False, server_default="unknown"
    )
    occurred_from: Mapped[date | None] = mapped_column(Date)
    occurred_to: Mapped[date | None] = mapped_column(Date)
    time_precision: Mapped[str] = mapped_column(
        time_precision_enum, nullable=False, server_default="unknown"
    )
    place_candidates: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")
    place_id: Mapped[int | None] = mapped_column(ForeignKey("place.id"))
    place_precision: Mapped[str] = mapped_column(
        place_precision_enum, nullable=False, server_default="unknown"
    )
    extractor_version: Mapped[str] = mapped_column(Text, nullable=False)
    valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    invalid_reason: Mapped[str | None] = mapped_column(Text)
