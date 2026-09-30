"""The streams built separately fit together: what the worker registers, what the console shows,
what publishing queues, and what a fresh install needs to start."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.models import Job, Source, SourcePermission
from africasignal.publish.hooks import notify_published, register_publication_hook
from africasignal.publish.notify import queue_notifications
from africasignal.sources.seed import load_seed, seed_sources
from africasignal.web.app import create_app
from africasignal.web.deps import get_db
from tests.integration.email_support import add_place, add_situation, add_version
from tests.integration.test_admin_console import make_operator, signed_in


def run_python(code: str, **env: str) -> str:
    """Run ``code`` in a fresh interpreter whose environment is only PATH plus ``env``."""
    clean = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"), **env}
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=clean
    )
    return out.stdout.strip()


@pytest.fixture
def client(session: Session) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_db] = lambda: session
    with TestClient(app, follow_redirects=False) as c:
        yield c


def test_the_console_lists_every_seeded_source_each_awaiting_approval(
    client: TestClient, session: Session
) -> None:
    seed_sources(session, load_seed())
    signed_in(client, make_operator(session))
    page = client.get("/admin/sources").text
    slugs = [s.slug for s in session.scalars(select(Source))]
    assert len(slugs) >= 16
    for slug in slugs:
        assert slug in page
    # The adapters built since the first console: a feed source, NERC, NNPC, GDELT.
    for slug in ("nerc", "nnpc", "gdelt", "punch-rss"):
        assert slug in slugs
    approved = session.scalars(
        select(SourcePermission).where(SourcePermission.approved_at.is_not(None))
    )
    assert list(approved) == []  # seeding never approves anything


def test_every_seeded_source_has_an_adapter_or_a_job_that_reads_it(session: Session) -> None:
    """A source whose adapter nothing implements would be fetched into a dead job."""
    adapters = run_python(
        "from africasignal.jobs import handlers; from africasignal.sources import base; "
        "handlers.load_all(); print(' '.join(sorted(base.ADAPTERS)), '|', "
        "' '.join(sorted(handlers.HANDLERS)))"
    )
    names, kinds = (part.split() for part in adapters.split("|"))
    assert {"nbs", "nerc", "price_announcement", "rss"} <= set(names)
    assert {"gdelt_poll", "gdelt_fetch_article", "extract_claims", "resolve_places"} <= set(kinds)
    seeded = {s.adapter for s in load_seed()}
    for adapter in seeded - {"gdelt"}:  # GDELT is read by the gdelt_poll job, not fetch_source
        assert adapter in names


def test_publishing_queues_the_follower_notification_once(session: Session) -> None:
    register_publication_hook(queue_notifications)
    register_publication_hook(queue_notifications)  # registering twice must not queue twice
    situation = add_situation(session, "price-pms", add_place(session, "NG-LA", "Lagos", "state"))
    version = add_version(session, situation)
    notify_published(session, version.id, "new_version")
    notify_published(session, version.id, "new_version")
    jobs = session.scalars(select(Job).where(Job.kind == "notify_followers")).all()
    assert [j.payload["version_id"] for j in jobs] == [version.id]


def test_the_worker_registers_the_publication_hook_exactly_once() -> None:
    out = run_python(
        "from africasignal.jobs import handlers; from africasignal.publish import hooks; "
        "handlers.load_all(); handlers.load_all(); print(len(hooks._hooks))"
    )
    assert out == "1"


def test_a_fresh_install_needs_only_the_database_and_the_secret_key() -> None:
    out = run_python(
        "from africasignal.config import get_settings; from africasignal.web.app import create_app; "
        "from africasignal.jobs import handlers; "
        "get_settings(); create_app(); handlers.load_all(); print('ok')",
        ENV="production",
        DATABASE_URL="postgresql+psycopg://u:p@localhost/x",
        SECRET_KEY="k" * 32,
    )
    assert out == "ok"


def test_nothing_else_is_required_outside_development() -> None:
    with pytest.raises(subprocess.CalledProcessError):
        run_python(
            "from africasignal.config import get_settings; get_settings()",
            ENV="production",
            DATABASE_URL="postgresql+psycopg://u:p@localhost/x",
        )  # no SECRET_KEY
