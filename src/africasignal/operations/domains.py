"""Discovered domains (spec B6.7, B11.5): domains GDELT linked to that no approved outlet covers.
Nothing from them is fetched. An operator can reject one (it leaves the report) or add it as a
source: that creates an inactive news-outlet source with a feed address and a named owner, and no
permission. It is then approved on the Sources page like any other, where the same rule applies:
a news outlet cannot be approved without an owner (security review S-08)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal import audit
from africasignal.models import DiscoveredDomainDecision, Operator, Source
from africasignal.operations.assessments import MAX_REASON
from africasignal.sources import gdelt
from africasignal.sources.gdelt import DiscoveredDomain

_HOST = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
MAX_NAME = 120
MAX_OWNER = 200
REPORT_LIMIT = 100


class DomainError(ValueError):
    """A refusal the operator should see."""


def normalise(domain: str) -> str:
    return domain.strip().lower().removeprefix("www.")


def report(
    session: Session, *, since: datetime | None = None, limit: int = REPORT_LIMIT
) -> list[DiscoveredDomain]:
    """Undecided domains, most-linked first."""
    decided = set(session.scalars(select(DiscoveredDomainDecision.domain)))
    # Over-fetch, so decided domains do not push undecided ones out of the list.
    rows = gdelt.discovered_domains(session, since=since, limit=limit + len(decided))
    return [r for r in rows if r.domain not in decided][:limit]


def decided(session: Session, limit: int = 30) -> list[DiscoveredDomainDecision]:
    return list(
        session.scalars(
            select(DiscoveredDomainDecision)
            .order_by(
                DiscoveredDomainDecision.decided_at.desc(), DiscoveredDomainDecision.id.desc()
            )
            .limit(limit)
        )
    )


def _check_in_report(session: Session, domain: str) -> str:
    domain = normalise(domain)
    if not _HOST.match(domain):
        raise DomainError("that is not a domain name")
    if session.scalars(
        select(DiscoveredDomainDecision.id).where(DiscoveredDomainDecision.domain == domain)
    ).first():
        raise DomainError("that domain has already been decided")
    if not any(r.domain == domain for r in gdelt.discovered_domains(session, limit=100_000)):
        raise DomainError("that domain is not in the discovered-domains report")
    return domain


def reject(
    session: Session, operator: Operator, domain: str, note: str = ""
) -> DiscoveredDomainDecision:
    note = " ".join(note.split())[:MAX_REASON]
    domain = _check_in_report(session, domain)
    row = DiscoveredDomainDecision(
        domain=domain, status="rejected", note=note or None, decided_by_operator_id=operator.id
    )
    session.add(row)
    session.flush()
    audit.record(
        session,
        operator,
        "discovered_domain.reject",
        "discovered_domain",
        row.id,
        after={"domain": domain, "note": note or None},
    )
    return row


@dataclass(frozen=True)
class NewSource:
    name: str
    owner: str
    feed_url: str


def _slug(session: Session, domain: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", domain).strip("-") + "-rss"
    slug, n = base, 2
    while session.scalars(select(Source.id).where(Source.slug == slug)).first():
        slug, n = f"{base}-{n}", n + 1
    return slug


def _feed_on_domain(feed_url: str, domain: str) -> str:
    try:
        parts = urlsplit(feed_url.strip())
        host = (parts.hostname or "").lower().removeprefix("www.")
    except ValueError as exc:
        raise DomainError("the feed address is not a valid URL") from exc
    if parts.scheme not in ("http", "https") or parts.username or parts.password:
        raise DomainError("the feed address must be a plain http or https URL")
    if not (host == domain or host.endswith("." + domain)):
        raise DomainError(f"the feed address must be on {domain}")
    return feed_url.strip()


def add_as_source(session: Session, operator: Operator, domain: str, new: NewSource) -> Source:
    """Create the source for a discovered domain: inactive, no permission."""
    name, owner = new.name.strip(), new.owner.strip()
    if not name or len(name) > MAX_NAME:
        raise DomainError(f"give the outlet's name (up to {MAX_NAME} characters)")
    if not owner or len(owner) > MAX_OWNER:
        raise DomainError(
            "name the owner of the outlet (the company or group behind it); corroboration "
            "counts outlets with the same owner as one voice"
        )
    domain = _check_in_report(session, domain)
    feed_url = _feed_on_domain(new.feed_url, domain)
    source = Source(
        slug=_slug(session, domain),
        name=name,
        kind="news_outlet",
        adapter="rss",
        home_url=f"https://{domain}",
        feed_url=feed_url,
        owner=owner,
        schedule_minutes=30,
        max_requests_per_hour=60,
        active=False,
        coverage_note="Added from the GDELT discovered-domains report.",
    )
    session.add(source)
    session.flush()
    row = DiscoveredDomainDecision(
        domain=domain,
        status="added",
        decided_by_operator_id=operator.id,
        source_id=source.id,
    )
    session.add(row)
    session.flush()
    audit.record(
        session,
        operator,
        "discovered_domain.add_source",
        "source",
        source.id,
        after={
            "domain": domain,
            "slug": source.slug,
            "name": name,
            "owner": owner,
            "feed_url": feed_url,
            "active": False,
        },
    )
    return source
