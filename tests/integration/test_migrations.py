from sqlalchemy import Engine, inspect, text

from africasignal.models import Base
from alembic import command
from tests.integration.conftest import alembic_config, count


def _user_tables(engine: Engine) -> set[str]:
    # The PostGIS image also installs tiger/topology schemas that are on the search path, so
    # look at the public schema explicitly.
    names = set(inspect(engine).get_table_names(schema="public"))
    return names - {"alembic_version", "spatial_ref_sys"}


def test_upgrade_creates_every_model_table(engine: Engine) -> None:
    assert _user_tables(engine) == set(Base.metadata.tables)


def test_extensions_enabled(engine: Engine) -> None:
    with engine.connect() as conn:
        names = set(conn.execute(text("SELECT extname FROM pg_extension")).scalars())
    assert {"postgis", "citext"} <= names


def test_downgrade_removes_tables_and_enum_types_then_upgrade_again(engine: Engine) -> None:
    cfg = alembic_config()
    command.downgrade(cfg, "base")
    assert _user_tables(engine) == set()
    # Only enum types we created; PostGIS's own types are not enums.
    assert (
        count(
            engine,
            "SELECT count(*) FROM pg_type WHERE typtype = 'e' "
            "AND typnamespace = 'public'::regnamespace",
        )
        == 0
    )
    command.upgrade(cfg, "head")
    assert _user_tables(engine) == set(Base.metadata.tables)


def test_upgrade_from_an_empty_database_lands_on_the_single_head(engine: Engine) -> None:
    from alembic.script import ScriptDirectory

    cfg = alembic_config()
    command.downgrade(cfg, "base")
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM alembic_version"))
    assert _user_tables(engine) == set()
    command.upgrade(cfg, "head")
    heads = ScriptDirectory.from_config(cfg).get_heads()
    with engine.connect() as conn:
        stored = set(conn.execute(text("SELECT version_num FROM alembic_version")).scalars())
    assert stored == set(heads)
    assert _user_tables(engine) == set(Base.metadata.tables)
