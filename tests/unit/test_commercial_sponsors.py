from __future__ import annotations

import pytest
from pydantic import ValidationError

from africasignal.operations.commercial_sponsors import SponsorDraft


def _draft(**overrides: str) -> dict[str, str]:
    return {
        "public_name": "  Example Sponsor  ",
        "website_url": "HTTPS://WWW.Example.org/",
        "contact_email": " Sales@Example.org ",
        **overrides,
    }


def test_sponsor_draft_normalizes_and_restricts_public_destination() -> None:
    draft = SponsorDraft.model_validate(_draft())

    assert draft.public_name == "Example Sponsor"
    assert draft.website_url == "https://www.example.org"
    assert draft.contact_email == "sales@example.org"


@pytest.mark.parametrize(
    "website_url",
    [
        "http://example.org",
        "https://user:password@example.org",
        "https://127.0.0.1/",
        "https://localhost/",
        "https://example.org/?tracking=1",
        "https://example.org/#fragment",
        "https://example.org:8443/",
    ],
)
def test_sponsor_draft_rejects_unsafe_or_unreviewed_destination(website_url: str) -> None:
    with pytest.raises(ValidationError):
        SponsorDraft.model_validate(_draft(website_url=website_url))


def test_sponsor_draft_rejects_extra_fields_and_invalid_contact() -> None:
    with pytest.raises(ValidationError):
        SponsorDraft.model_validate({**_draft(), "status": "approved"})
    with pytest.raises(ValidationError):
        SponsorDraft.model_validate(_draft(contact_email="not-an-email"))
