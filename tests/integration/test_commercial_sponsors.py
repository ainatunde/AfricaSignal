from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.models import AuditLog, Sponsor
from africasignal.operations.commercial_sponsors import (
    SponsorDraft,
    SponsorError,
    create_sponsor,
    get_sponsor,
    list_sponsors,
)
from africasignal.operators import create_operator


def _admin(session: Session):
    operator, _ = create_operator(
        session, "commercial-admin@example.org", "correct horse battery staple", "admin"
    )
    return operator


def test_create_sponsor_is_pending_review_and_private_contact_is_not_audited(
    session: Session,
) -> None:
    operator = _admin(session)
    contact = "private-contact@example.org"
    result = create_sponsor(
        session,
        operator,
        SponsorDraft(
            public_name="Reviewed Name",
            website_url="https://example.org",
            contact_email=contact,
        ),
    )

    assert result.status == "pending_review"
    assert result.revision == 1
    assert result.contact_email == contact
    stored = session.get(Sponsor, result.id)
    assert stored is not None and stored.status == "pending_review"
    action = session.scalars(
        select(AuditLog).where(AuditLog.action == "commercial.sponsor.create")
    ).one()
    assert action.after is not None
    assert "contact_email" not in action.after
    assert contact not in repr(action.after)
    assert get_sponsor(session, result.id) == result
    assert list_sponsors(session, status="pending_review") == (result,)


def test_sponsor_creation_requires_enabled_admin(session: Session) -> None:
    editor, _ = create_operator(
        session, "commercial-editor@example.org", "correct horse battery staple", "editor"
    )
    draft = SponsorDraft(
        public_name="Reviewed Name",
        website_url="https://example.org",
        contact_email="business@example.org",
    )

    with pytest.raises(SponsorError, match="administrator"):
        create_sponsor(session, editor, draft)
    assert session.scalar(select(Sponsor.id)) is None
