from __future__ import annotations

import pytest
from pydantic import ValidationError

from africasignal.operations.commercial_campaigns import CampaignDraft, CreativeDraft


def test_campaign_fee_is_integer_nonnegative_kobo_and_extra_fields_are_rejected() -> None:
    campaign = CampaignDraft(sponsor_id=1, internal_name="Q4 pilot", agreed_fee_minor=250_000)
    assert campaign.agreed_fee_minor == 250_000
    with pytest.raises(ValidationError):
        CampaignDraft(sponsor_id=1, internal_name="bad", agreed_fee_minor=-0.5)
    with pytest.raises(ValidationError):
        CampaignDraft(sponsor_id=1, internal_name="bad", currency="USD")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "destination",
    [
        "http://example.org",
        "https://user@example.org",
        "https://127.0.0.1",
        "https://example.org/?q=reader",
        "https://example.org:444",
    ],
)
def test_creative_rejects_unsafe_destination(destination: str) -> None:
    with pytest.raises(ValidationError):
        CreativeDraft(body_text="Reviewed copy", destination_url=destination)


def test_creative_accepts_only_digest_named_local_asset_with_alt_text() -> None:
    key = "commercial/creative/" + "a" * 64 + ".webp"
    draft = CreativeDraft(
        body_text="Reviewed copy",
        destination_url="https://example.org/offer",
        asset_key=key,
        alt_text="A reviewed solar lantern",
    )
    assert draft.asset_key == key
    with pytest.raises(ValidationError):
        CreativeDraft(
            body_text="Reviewed copy",
            destination_url="https://example.org",
            asset_key="../remote.jpg",
        )
