import os

# Tests run against a real PostgreSQL/PostGIS database, never SQLite.
os.environ.setdefault("ENV", "development")
os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://africasignal:africasignal@localhost:5432/africasignal_test",
)
