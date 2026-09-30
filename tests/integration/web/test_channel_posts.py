"""Channel posts from real NBS assessments (AS-033): every published version, every item."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from africasignal.catalog import load_items
from africasignal.models import AssessmentVersion, Place, Situation, Source
from africasignal.publish.factfmt import allowed_numbers, numbers_in
from africasignal.publish.situations import ensure_situations
from africasignal.publish.whatsapp_text import (
    MAX_CHARS,
    channel_posts,
    material_changes,
    write_post,
)
from africasignal.storage import S3Store
from tests.integration.nbs_support import import_bytes
from tests.integration.web.web_support import PUBLISHED_AT, assess_and_publish, seed_petrol

BASE = "https://africasignal.example"
SEPT_OCT = {
    "pms_litre": ("FUEL_SEPT_2024_REPORT.xlsx", "PMS_OCT_2024_REPORT.xlsx"),
    "ago_litre": ("DIESEL_SEPT_2024_REPORT.xlsx", "DIESEL_OCT_2024_REPORT.xlsx"),
    "dpk_litre": ("HOUSEHOLD_KEROSENE_SEPT_2024.xlsx", "HOUSEHOLD_KEROSENE_OCT_2024.xlsx"),
    "lpg_5kg": ("GAS_PRICE_WATCH_SEPT_2024.xlsx", "GAS_PRICE_WATCH_OCT_2024.xlsx"),
    "lpg_12_5kg": ("GAS_PRICE_WATCH_SEPT_2024.xlsx", "GAS_PRICE_WATCH_OCT_2024.xlsx"),
    "rice_local_1kg": ("selected_food_sept_2024.xlsx", "selected_food_oct_2024.xlsx"),
    "garri_white_1kg": ("selected_food_sept_2024.xlsx", "selected_food_oct_2024.xlsx"),
    "beans_brown_1kg": ("selected_food_sept_2024.xlsx", "selected_food_oct_2024.xlsx"),
    "maize_white_1kg": ("selected_food_sept_2024.xlsx", "selected_food_oct_2024.xlsx"),
}


def test_posts_for_every_item_fit_and_state_only_numbers_from_the_facts(
    session: Session, store: S3Store, source: Source
) -> None:
    imported: set[str] = set()
    for files in SEPT_OCT.values():
        for file in files:
            if file not in imported:
                import_bytes(session, store, source, file)
                imported.add(file)
    published: list[AssessmentVersion] = []
    for item in load_items().items:
        for code in ("NG", "NG-LA", "NG-KN", "NG-FC"):
            place_id = session.scalars(select(Place.id).where(Place.code == code)).one()
            if ensure_situations(session, [(item.code, place_id)]):  # food is national only
                published.append(assess_and_publish(session, item.code, code)[1])
    assert len(published) == 5 * 4 + 4  # five fuel and gas items in four places, four food items
    assert {v.facts[0]["unit"] for v in published} >= {
        "NGN/litre",
        "NGN/5kg",
        "NGN/12.5kg",
        "NGN/kg",
    }
    for version in published:
        situation = session.get(Situation, version.situation_id)
        assert situation is not None
        for channel in ("wa", "x"):
            post = write_post(version, situation, base_url=BASE, channel=channel)
            assert len(post.text) <= MAX_CHARS[channel]
            body = post.text.replace(post.link, "")
            allowed = allowed_numbers(list(version.facts))
            assert all(n in allowed for n in numbers_in(body)), (post.text, version.facts)
            assert post.text.endswith(f"?ref={channel}")
            assert version.headline in post.text  # the headline is never cut or reworded


def test_material_changes_since_a_time_are_the_published_current_material_ones(
    session: Session, store: S3Store, source: Source
) -> None:
    seeded = seed_petrol(session, store, source)
    lagos, nigeria = seeded["NG-LA"][1], seeded["NG"][1]
    lagos.severity, nigeria.severity = "high", "none"  # Nigeria's change is not material
    session.flush()
    found = material_changes(session, PUBLISHED_AT - timedelta(hours=1))
    assert [(s.slug, v.version) for s, v in found] == [("price-pms_litre-ng-la", 1)]
    assert material_changes(session, PUBLISHED_AT + timedelta(hours=1)) == []  # already posted
    lagos.status = "withheld"
    session.flush()
    assert material_changes(session, PUBLISHED_AT - timedelta(hours=1)) == []
    lagos.status, lagos.evidence_state = "published", "insufficient"
    session.flush()
    assert material_changes(session, PUBLISHED_AT - timedelta(hours=1)) == []


def test_channel_posts_pair_a_whatsapp_and_an_x_post_per_change(
    session: Session, store: S3Store, source: Source
) -> None:
    seeded = seed_petrol(session, store, source)
    seeded["NG-LA"][1].severity = "medium"
    seeded["NG"][1].severity = "low"
    session.flush()
    posts = channel_posts(session, datetime(2024, 1, 1, tzinfo=UTC), base_url=BASE)
    assert sorted(p.situation_slug for p in posts) == [
        "price-pms_litre-ng",
        "price-pms_litre-ng-la",
    ]
    for p in posts:
        assert p.whatsapp.text.endswith("?ref=wa") and p.x.text.endswith("?ref=x")
        assert len(p.x.text) <= 270 and len(p.whatsapp.text) <= 600
