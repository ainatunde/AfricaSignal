import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from sqlalchemy import Engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from africasignal.jobs import handlers, queue
from africasignal.jobs.handlers import fetch_source as fetch_source_module
from africasignal.jobs.worker import Worker
from africasignal.models import Source, SourcePermission
from africasignal.sources import base
from africasignal.sources.base import AdapterContext, DiscoveredItem, ProcessResult
from africasignal.sources.permissions import current_permission
from africasignal.sources.seed import load_seed, seed_sources

NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def _snapshot(session: Session) -> list[tuple[object, ...]]:
    rows = session.execute(
        text(
            "SELECT s.slug, s.name, s.active, s.next_due_at, s.schedule_minutes, s.feed_url, "
            "p.version, p.approved_at, p.may_collect FROM source s "
            "LEFT JOIN source_permission p ON p.source_id = s.id ORDER BY s.slug, p.version"
        )
    ).all()
    return [tuple(r) for r in rows]


# --- seed --------------------------------------------------------------------------------------


def test_shipped_seed_loads_and_covers_the_plan_sources() -> None:
    slugs = {s.slug for s in load_seed()}
    assert {"nbs-elibrary", "nerc", "nmdpra", "gdelt", "punch-rss"} <= slugs
    assert len(slugs) == len(load_seed())


def test_news_feeds_are_seeded_inactive_and_only_checked_feeds_have_a_url() -> None:
    """Every outlet stays inactive until an operator has reviewed its terms. Eight feeds were
    checked on 2026-09-30; TheCable and Guardian Nigeria answer bots with a challenge page."""
    news = {s.slug: s for s in load_seed() if s.adapter == "rss"}
    assert len(news) == 10
    assert all(not s.active for s in news.values())
    without_feed = {slug for slug, s in news.items() if s.feed_url is None}
    assert without_feed == {"thecable-rss", "guardian-nigeria-rss"}
    assert all(s.feed_url.startswith("https://") for s in news.values() if s.feed_url)


def test_seed_creates_sources_with_unapproved_permissions(session: Session) -> None:
    result = seed_sources(session, load_seed(), now=NOW)
    sources = load_seed()
    assert result.created == len(sources) == _count(session, Source)
    assert result.permissions_created == len(sources)
    assert session.scalar(
        select(func.count())
        .select_from(SourcePermission)
        .where(SourcePermission.approved_at.is_(None))
    ) == len(sources)
    nbs = session.scalars(select(Source).where(Source.slug == "nbs-elibrary")).one()
    assert current_permission(session, nbs.id) is None  # nothing is in force until approved
    assert nbs.next_due_at == NOW


def test_running_the_seed_twice_changes_nothing(session: Session) -> None:
    seed_sources(session, load_seed(), now=NOW)
    before = _snapshot(session)
    second = seed_sources(session, load_seed(), now=datetime(2027, 1, 1, tzinfo=UTC))
    assert not second.changed
    assert _snapshot(session) == before


def test_seed_updates_descriptive_fields_but_keeps_operator_state(session: Session) -> None:
    seed_sources(session, load_seed(), now=NOW)
    nerc = session.scalars(select(Source).where(Source.slug == "nerc")).one()
    nerc.active = False  # an operator paused it in the console
    nerc.health = "failing"
    approved = datetime(2026, 9, 1, tzinfo=UTC)
    permission = session.scalars(
        select(SourcePermission).where(SourcePermission.source_id == nerc.id)
    ).one()
    permission.approved_at = approved
    session.flush()

    changed = [s.model_copy(update={"name": "NERC (renamed)", "schedule_minutes": 120}) if s.slug == "nerc" else s
               for s in load_seed()]  # fmt: skip
    result = seed_sources(session, changed, now=NOW)

    assert (result.created, result.updated, result.permissions_created) == (0, 1, 0)
    session.refresh(nerc)
    assert (nerc.name, nerc.schedule_minutes) == ("NERC (renamed)", 120)
    assert nerc.active is False and nerc.health == "failing"
    assert current_permission(session, nerc.id) is not None  # approval untouched


def test_seed_does_not_add_a_second_permission_when_one_exists(session: Session) -> None:
    seed_sources(session, load_seed(), now=NOW)
    nerc = session.scalars(select(Source).where(Source.slug == "nerc")).one()
    session.add(
        SourcePermission(
            source_id=nerc.id, version=2, may_collect=False, may_store_full_text=False,
            may_republish_numbers=False, approved_at=NOW,
        )
    )  # fmt: skip
    session.flush()
    assert not seed_sources(session, load_seed(), now=NOW).changed
    assert current_permission(session, nerc.id).version == 2  # type: ignore[union-attr]


def test_invalid_seed_is_rejected(tmp_path: Path) -> None:
    bad = tmp_path / "sources.yaml"
    bad.write_text(
        yaml.safe_dump(
            [{"slug": "x", "name": "X", "kind": "blog", "adapter": "rss", "schedule_minutes": 5}]
        )
    )
    with pytest.raises(ValueError):
        load_seed(bad)
    dup = tmp_path / "dup.yaml"
    entry = {
        "slug": "x",
        "name": "X",
        "kind": "news_outlet",
        "adapter": "rss",
        "schedule_minutes": 5,
    }
    dup.write_text(yaml.safe_dump([entry, entry]))
    with pytest.raises(ValueError, match="duplicate"):
        load_seed(dup)


# --- fetch_source ------------------------------------------------------------------------------


@pytest.fixture
def factory(engine: Engine) -> Iterator[sessionmaker[Session]]:
    with engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM job; DELETE FROM job_deduplication; DELETE FROM source_permission; DELETE FROM source"
            )
        )
    yield sessionmaker(bind=engine, expire_on_commit=False)
    with engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM job; DELETE FROM job_deduplication; DELETE FROM source_permission; DELETE FROM source"
            )
        )


class FakeAdapter:
    slug_prefix = "fake"

    def __init__(self, items: list[DiscoveredItem] | Exception) -> None:
        self.items = items
        self.calls = 0

    def discover(self, source: Source, ctx: AdapterContext) -> list[DiscoveredItem]:
        self.calls += 1
        if isinstance(self.items, Exception):
            raise self.items
        return self.items

    def process(self, doc, ctx) -> ProcessResult:  # type: ignore[no-untyped-def]
        return ProcessResult()


@pytest.fixture(autouse=True)
def isolated_registries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(handlers.HANDLERS, "fetch_source", fetch_source_module.fetch_source)
    monkeypatch.setattr(base, "ADAPTERS", {})
    monkeypatch.setattr(fetch_source_module, "get_adapter", lambda name: base.ADAPTERS.get(name))
    monkeypatch.setattr(fetch_source_module, "get_store", lambda: object())


def _make_source(
    factory: sessionmaker[Session], *, approved: bool, may_collect: bool = True, active: bool = True
) -> int:
    with factory() as s:
        src = Source(slug="nbs-elibrary", name="NBS", kind="official_statistics", adapter="nbs",
                     schedule_minutes=60, active=active)  # fmt: skip
        s.add(src)
        s.flush()
        s.add(SourcePermission(source_id=src.id, version=1, may_collect=may_collect,
                               may_store_full_text=True, may_republish_numbers=True,
                               approved_at=datetime.now(UTC) if approved else None))  # fmt: skip
        s.commit()
        return src.id


def _run(factory: sessionmaker[Session], source_id: int) -> None:
    with factory() as s:
        queue.enqueue(s, "fetch_source", {"source_id": source_id})
        s.commit()
    Worker(factory, "w").run_once()


def _jobs(factory: sessionmaker[Session], kind: str) -> list[str]:
    with factory() as s:
        return list(
            s.execute(
                text("SELECT dedupe_key FROM job WHERE kind = :k ORDER BY id"), {"k": kind}
            ).scalars()
        )


def _job_status(factory: sessionmaker[Session]) -> str:
    with factory() as s:
        return str(s.execute(text("SELECT status FROM job WHERE kind='fetch_source'")).scalar_one())


def test_unapproved_source_is_refused_with_a_clear_log_line(
    factory: sessionmaker[Session], caplog: pytest.LogCaptureFixture
) -> None:
    adapter = FakeAdapter([DiscoveredItem("https://x.example/a")])
    base.ADAPTERS["nbs"] = adapter
    source_id = _make_source(factory, approved=False)
    with caplog.at_level(logging.WARNING, logger="africasignal.fetch_source"):
        _run(factory, source_id)
    assert adapter.calls == 0
    assert "refusing to fetch source nbs-elibrary" in caplog.text
    assert "no approved permission" in caplog.text
    assert _job_status(factory) == "done" and _jobs(factory, "process_document") == []


def test_permission_that_forbids_collecting_is_refused(factory: sessionmaker[Session]) -> None:
    adapter = FakeAdapter([DiscoveredItem("https://x.example/a")])
    base.ADAPTERS["nbs"] = adapter
    _run(factory, _make_source(factory, approved=True, may_collect=False))
    assert adapter.calls == 0


def test_inactive_source_is_skipped(factory: sessionmaker[Session]) -> None:
    adapter = FakeAdapter([DiscoveredItem("https://x.example/a")])
    base.ADAPTERS["nbs"] = adapter
    _run(factory, _make_source(factory, approved=True, active=False))
    assert adapter.calls == 0


def test_approved_source_without_an_adapter_is_a_quiet_noop(factory: sessionmaker[Session]) -> None:
    _run(factory, _make_source(factory, approved=True))
    assert _job_status(factory) == "done"


def test_approved_source_enqueues_one_process_job_per_canonical_url(
    factory: sessionmaker[Session],
) -> None:
    base.ADAPTERS["nbs"] = FakeAdapter(
        [
            DiscoveredItem("https://x.example/a/"),
            DiscoveredItem("https://x.example/a?utm_source=rss"),  # the same page
            DiscoveredItem("https://x.example/b"),
        ]
    )
    source_id = _make_source(factory, approved=True)
    _run(factory, source_id)
    keys = _jobs(factory, "process_document")
    assert keys == [
        f"process_document:{source_id}:https://x.example/a",
        f"process_document:{source_id}:https://x.example/b",
    ]
    _run_again = factory()
    _run_again.close()
    with factory() as s:
        src = s.get(Source, source_id)
        assert src is not None and src.health == "healthy" and src.last_success_at is not None


def test_relisting_the_same_items_does_not_enqueue_them_again(
    factory: sessionmaker[Session],
) -> None:
    base.ADAPTERS["nbs"] = FakeAdapter([DiscoveredItem("https://x.example/a")])
    source_id = _make_source(factory, approved=True)
    _run(factory, source_id)
    with factory() as s:
        s.execute(text("DELETE FROM job WHERE kind = 'fetch_source'"))
        s.commit()
    _run(factory, source_id)
    assert len(_jobs(factory, "process_document")) == 1


def test_discovery_failures_degrade_then_fail_the_source_and_recovery_resets_it(
    factory: sessionmaker[Session],
) -> None:
    adapter = FakeAdapter(RuntimeError("listing page changed"))
    base.ADAPTERS["nbs"] = adapter
    source_id = _make_source(factory, approved=True)

    def state() -> tuple[str, int, str | None]:
        with factory() as s:
            src = s.get(Source, source_id)
            assert src is not None
            return src.health, src.consecutive_failures, src.last_error

    for expected in [
        ("healthy", 1),
        ("degraded", 2),
        ("degraded", 3),
        ("degraded", 4),
        ("failing", 5),
    ]:
        with factory() as s:
            s.execute(text("DELETE FROM job"))
            s.commit()
        _run(factory, source_id)
        health, failures, error = state()
        assert (health, failures) == expected
        assert error == "RuntimeError: listing page changed"

    adapter.items = []  # the site is back
    with factory() as s:
        s.execute(text("DELETE FROM job"))
        s.commit()
    _run(factory, source_id)
    assert state() == ("healthy", 0, None)
