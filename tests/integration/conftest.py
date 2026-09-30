from collections.abc import Iterator

import pytest
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from africasignal.db import get_engine
from alembic import command


def alembic_config() -> Config:
    return Config("alembic.ini")


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    """A database migrated to head for the module, emptied again afterwards."""
    cfg = alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    yield get_engine()
    command.downgrade(cfg, "base")


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    """A session whose work is rolled back after each test."""
    connection = engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    yield session
    session.close()
    transaction.rollback()
    connection.close()


def count(engine: Engine, sql: str) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(sql)).scalar_one())
