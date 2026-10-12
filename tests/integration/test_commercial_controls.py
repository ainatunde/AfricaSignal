from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session

from africasignal.models import CommercialControl
from africasignal.operations import commercial_controls


def test_explore_effectiveness_requires_all_controls_and_privacy_acceptance(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = session.get(CommercialControl, 1)
    assert row is not None
    assert row.global_enabled is False
    assert row.explore_sponsorship_enabled is False
    row.global_enabled = True
    row.explore_sponsorship_enabled = True
    session.flush()

    monkeypatch.setattr(
        commercial_controls,
        "get_settings",
        lambda: SimpleNamespace(commercial_deny=False),
    )
    monkeypatch.setattr(commercial_controls, "publication_suspended", lambda _session: False)
    monkeypatch.setattr(
        commercial_controls.settings_store,
        "get",
        lambda _session, key: {
            "legal_review_confirmed": "yes",
            "commercial_privacy_review_confirmed": "no",
        }.get(key),
    )

    blocked = commercial_controls.current(session, at=datetime.now(UTC))
    assert blocked["explore_desired"] is True
    assert blocked["explore_effective"] is False
    assert any(
        "privacy and measurement review is not confirmed" in reason
        for reason in blocked["explore_blockers"]
    )

    monkeypatch.setattr(
        commercial_controls.settings_store,
        "get",
        lambda _session, _key: "yes",
    )
    effective = commercial_controls.current(session, at=datetime.now(UTC))
    assert effective["explore_effective"] is True
