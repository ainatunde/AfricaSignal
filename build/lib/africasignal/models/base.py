"""Declarative base, shared column helpers and enum factory."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, Identity, MetaData, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class IdMixin:
    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)


class CreatedMixin(IdMixin):
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


def pg_enum(name: str, *values: str) -> Enum:
    """A native PostgreSQL enum. Values are stored by their string value."""
    return Enum(*values, name=name, native_enum=True, create_constraint=False)


# Enums used by more than one table are defined once so the migration creates each type once.
topic_enum = pg_enum("topic", "energy", "food")
