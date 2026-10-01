"""Refuse destructive migrations outside a dedicated test database."""

import os

from sqlalchemy.engine import make_url


def require_test_database() -> None:
    url = make_url(os.environ["DATABASE_URL"])
    if os.environ.get("ENV") != "development" or not (url.database or "").endswith("_test"):
        raise RuntimeError(
            "Integration tests require ENV=development and a dedicated *_test database"
        )
