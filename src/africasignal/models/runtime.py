"""Shared ephemeral limits and compact job history."""

from datetime import date, datetime
from typing import Any

from sqlalchemy import BigInteger, Date, DateTime, Float, Index, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from africasignal.models.base import Base


class RateLimitState(Base):
    __tablename__ = "rate_limit_state"
    __table_args__ = (Index("ix_rate_limit_state_expires_at", "expires_at"),)
    scope: Mapped[str] = mapped_column(Text, primary_key=True)
    key_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    tokens: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    rate: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    capacity: Mapped[float] = mapped_column(Float, nullable=False, server_default="0")
    touched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    hits: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, server_default="[]")


class JobDeduplication(Base):
    __tablename__ = "job_deduplication"
    dedupe_key: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class JobArchive(Base):
    __tablename__ = "job_archive"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, primary_key=True)
    jobs: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attempts: Mapped[int] = mapped_column(BigInteger, nullable=False)
