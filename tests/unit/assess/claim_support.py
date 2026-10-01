"""Builders for ``ClaimPoint`` in the T1 and T2 evidence tests."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import count
from typing import Any

from africasignal.assess.corroboration import ClaimPoint

_ids = count(100)


def claim(**overrides: Any) -> ClaimPoint:
    """A valid news claim: petrol rose in October 2024, reported by an outlet of its own."""
    n = next(_ids)
    fields: dict[str, Any] = {
        "claim_id": n,
        "evidence_document_id": 1000 + n,
        "origin_id": 5000 + n,
        "origin_label": f"Outlet {n} report, 12 Nov 2024",
        "source_name": f"Outlet {n}",
        "source_short": f"Outlet {n}",
        "source_kind": "news_outlet",
        "text": "Petrol prices rose in Lagos.",
        "passage": "Petrol prices rose in Lagos.",
        "stated_value": None,
        "direction": "up",
        "occurred_from": date(2024, 10, 1),
        "occurred_to": date(2024, 10, 31),
        "time_precision": "month",
        "published_at": datetime(2024, 11, 12, 9, 0, tzinfo=UTC),
        "trusted": True,
        "link_keys": frozenset({f"domain:outlet{n}.example"}),  # each outlet has its own site
    }
    fields.update(overrides)
    if isinstance(fields["stated_value"], str):
        fields["stated_value"] = Decimal(fields["stated_value"])
    return ClaimPoint(**fields)


def official(**overrides: Any) -> ClaimPoint:
    """A claim from a regulator's own document."""
    fields: dict[str, Any] = {
        "source_kind": "regulator",
        "source_name": "Nigerian Electricity Regulatory Commission",
        "source_short": "NERC",
    }
    fields.update(overrides)
    return claim(**fields)
