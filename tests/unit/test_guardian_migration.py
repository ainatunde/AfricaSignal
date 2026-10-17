import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.dialects import postgresql


def _migration():
    path = (
        Path(__file__).parents[2] / "alembic" / "versions" / "0072_guardian_public_page_checks.py"
    )
    spec = importlib.util.spec_from_file_location("guardian_public_page_migration", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_guardian_check_migration_upgrades_and_downgrades_a_disposable_database() -> None:
    engine = create_engine("sqlite:///:memory:")
    migration = _migration()
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        table_names = inspect(connection).get_table_names()
        assert "guardian_check_run" in table_names
        indexes = {index["name"] for index in inspect(connection).get_indexes("guardian_check_run")}
        assert indexes == {
            "ix_guardian_check_run_subject_checked",
            "ix_guardian_check_run_checked",
        }

        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert "guardian_check_run" not in inspect(connection).get_table_names()

    observed_type = migration.postgresql.JSONB(astext_type=migration.sa.Text())
    assert observed_type.compile(dialect=postgresql.dialect()) == "JSONB"
