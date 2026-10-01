from logging.config import fileConfig

from sqlalchemy import create_engine, pool

from africasignal.config import get_settings
from africasignal.models import Base
from alembic import context

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata

# Tables owned by PostGIS, not by us.
POSTGIS_TABLES = {"spatial_ref_sys"}


def include_object(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    if type_ == "table" and name in POSTGIS_TABLES:
        return False
    return True


def database_url() -> str:
    return config.get_main_option("sqlalchemy.url") or get_settings().effective_database_url


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
