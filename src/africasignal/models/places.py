"""B3.3 Places."""

from __future__ import annotations

from geoalchemy2 import Geometry
from sqlalchemy import ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from africasignal.models.base import Base, CreatedMixin, pg_enum

place_kind = pg_enum("place_kind", "country", "state", "lga", "city", "neighbourhood_alias")


class Place(CreatedMixin, Base):
    __tablename__ = "place"

    kind: Mapped[str] = mapped_column(place_kind, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    code: Mapped[str | None] = mapped_column(Text, unique=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("place.id"))
    geom = mapped_column(Geometry("MULTIPOLYGON", srid=4326, spatial_index=True), nullable=True)
    point = mapped_column(Geometry("POINT", srid=4326, spatial_index=True), nullable=True)
    boundary_version: Mapped[str | None] = mapped_column(Text)
    population: Mapped[int | None] = mapped_column(Integer)


class PlaceAlias(CreatedMixin, Base):
    __tablename__ = "place_alias"
    __table_args__ = (
        Index("ix_place_alias_alias_norm", "alias_norm"),
        UniqueConstraint("place_id", "alias_norm"),
    )

    place_id: Mapped[int] = mapped_column(ForeignKey("place.id"), nullable=False)
    alias: Mapped[str] = mapped_column(Text, nullable=False)
    alias_norm: Mapped[str] = mapped_column(Text, nullable=False)  # lowercase, no punctuation
