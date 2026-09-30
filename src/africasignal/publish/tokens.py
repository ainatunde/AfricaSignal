"""Signed, purpose-bound tokens for links that need no stored state (one-click unsubscribe)."""

from __future__ import annotations

import base64
import hashlib
import hmac

from africasignal.config import get_settings

_DEV_SECRET = "development-only-secret"


def _key() -> bytes:
    settings = get_settings()
    secret = settings.secret_key or (_DEV_SECRET if settings.env == "development" else "")
    if not secret:
        raise RuntimeError("SECRET_KEY is not set")
    return secret.encode()


def _mac(purpose: str, value: str) -> str:
    digest = hmac.new(_key(), f"{purpose}:{value}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:16]).decode().rstrip("=")


def sign(purpose: str, value: str) -> str:
    """``value.mac``. The purpose is part of the MAC, so a token for one use is useless for
    another. Tokens do not expire."""
    return f"{value}.{_mac(purpose, value)}"


def verify(purpose: str, token: str) -> str | None:
    """The signed value, or ``None`` when the token is malformed or has been altered."""
    value, _, mac = token.rpartition(".")
    if not value or not hmac.compare_digest(mac, _mac(purpose, value)):
        return None
    return value
