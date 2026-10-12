from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from africasignal.operations.commercial_measurement import (
    TOKEN_TTL_SECONDS,
    ClickTokenError,
    DeliveryEnvelope,
    issue_click_token,
    verify_click_token,
)


def test_click_token_is_purpose_bound_short_lived_and_has_random_nonce() -> None:
    now = datetime.now(UTC)
    first = issue_click_token(booking_id=7, creative_version_id=11, booking_revision=3, at=now)
    second = issue_click_token(booking_id=7, creative_version_id=11, booking_revision=3, at=now)
    claims = verify_click_token(first, at=now)

    assert claims.event_kind == "click"
    assert claims.booking_id == 7
    assert claims.creative_version_id == 11
    assert claims.booking_revision == 3
    assert claims.expires_at - claims.issued_at == TOKEN_TTL_SECONDS
    assert first != second


def test_click_token_rejects_tampering_expiry_oversize_and_naive_time() -> None:
    now = datetime.now(UTC)
    token = issue_click_token(booking_id=7, creative_version_id=11, booking_revision=3, at=now)
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(ClickTokenError):
        verify_click_token(tampered, at=now)
    with pytest.raises(ClickTokenError):
        verify_click_token(token, at=now + timedelta(seconds=TOKEN_TTL_SECONDS))
    with pytest.raises(ClickTokenError):
        verify_click_token("x" * 1025, at=now)
    with pytest.raises(ValueError, match="timezone-aware"):
        issue_click_token(
            booking_id=7,
            creative_version_id=11,
            booking_revision=3,
            at=datetime.now(),
        )


def test_delivery_envelope_rejects_unbounded_or_extra_inputs() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DeliveryEnvelope(event_schema_version=1, event_kind="click", token="x", target="https://x")
    with pytest.raises(ValidationError):
        DeliveryEnvelope(event_schema_version=1, event_kind="click", token="x" * 1025)
