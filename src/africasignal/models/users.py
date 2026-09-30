"""B3.7 Users, follows, notifications."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import CITEXT
from sqlalchemy.orm import Mapped, mapped_column

from africasignal.models.base import Base, CreatedMixin, pg_enum

notification_kind = pg_enum("notification_kind", "new_version", "correction", "withdrawal")


class AppUser(CreatedMixin, Base):
    __tablename__ = "app_user"

    email: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    digest_opt_in: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    digest_opt_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class LoginToken(CreatedMixin, Base):
    __tablename__ = "login_token"

    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    token_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UserSession(CreatedMixin, Base):
    __tablename__ = "session"

    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    token_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Preference(Base):
    __tablename__ = "preference"

    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), primary_key=True)
    place_ids: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), nullable=False, server_default="{}"
    )
    topics: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default="{}")


class Follow(CreatedMixin, Base):
    __tablename__ = "follow"
    __table_args__ = (UniqueConstraint("user_id", "situation_id"),)

    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    situation_id: Mapped[int] = mapped_column(ForeignKey("situation.id"), nullable=False)


class Notification(CreatedMixin, Base):
    __tablename__ = "notification"

    user_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False)
    assessment_version_id: Mapped[int] = mapped_column(
        ForeignKey("assessment_version.id"), nullable=False
    )
    kind: Mapped[str] = mapped_column(notification_kind, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AccountDeletion(CreatedMixin, Base):
    """A ledger of accounts their holders deleted, so a restored backup can delete them again.

    Holds a keyed fingerprint of the address (HMAC-SHA256 under a key derived from ``SECRET_KEY``),
    never the address. Rows are kept only as long as a backup that still holds the account could
    be restored. ``mirrored_at`` is set once the entry is also in object storage, which a database
    restore does not roll back."""

    __tablename__ = "account_deletion"

    email_hmac: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    mirrored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
