from __future__ import annotations

from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

from alembic import command


def test_sponsor_migration_upgrades_from_0040(engine: Engine) -> None:
    cfg = Config("alembic.ini")
    command.downgrade(cfg, "0040")
    assert "sponsor" not in inspect(engine).get_table_names()

    command.upgrade(cfg, "head")

    inspector = inspect(engine)
    assert "sponsor" in inspector.get_table_names()
    assert {column["name"] for column in inspector.get_columns("sponsor")} >= {
        "public_name",
        "website_url",
        "contact_email",
        "status",
        "revision",
        "created_by_operator_id",
        "updated_by_operator_id",
    }
    assert "commercial_control" in inspector.get_table_names()
    assert "campaign" in inspector.get_table_names()
    assert "creative_version" in inspector.get_table_names()

    assert "content_context" in inspector.get_table_names()

    assert "placement_booking" in inspector.get_table_names()

    assert "delivery_event" in inspector.get_table_names()
    assert "delivery_aggregate" in inspector.get_table_names()
