import os

# Tests run against a real PostgreSQL/PostGIS database, never SQLite.
os.environ["ENV"] = "development"
# Never inherit the application's DATABASE_URL: integration fixtures drop the entire schema.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://africasignal:africasignal@localhost:5432/africasignal_test",
)
