"""B3.4 Measurements."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Numeric, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from africasignal.models.base import Base, CreatedMixin, pg_enum, topic_enum

series_frequency = pg_enum("series_frequency", "monthly", "adhoc")


class Series(CreatedMixin, Base):
    __tablename__ = "series"
    __table_args__ = (UniqueConstraint("item_code", "source_id"),)

    item_code: Mapped[str] = mapped_column(Text, nullable=False)
    topic: Mapped[str] = mapped_column(topic_enum, nullable=False)
    source_id: Mapped[int] = mapped_column(ForeignKey("source.id"), nullable=False)
    unit: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False, server_default="NGN")
    frequency: Mapped[str] = mapped_column(series_frequency, nullable=False)


class Measurement(CreatedMixin, Base):
    """The current value for a period is the row with the latest vintage and no successor."""

    __tablename__ = "measurement"
    __table_args__ = (UniqueConstraint("series_id", "place_id", "period_start", "vintage"),)

    series_id: Mapped[int] = mapped_column(ForeignKey("series.id"), nullable=False)
    place_id: Mapped[int] = mapped_column(ForeignKey("place.id"), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    vintage: Mapped[date] = mapped_column(Date, nullable=False)  # publication date of the release
    evidence_document_id: Mapped[int] = mapped_column(
        ForeignKey("evidence_document.id"), nullable=False
    )
    superseded_by_id: Mapped[int | None] = mapped_column(ForeignKey("measurement.id"))
