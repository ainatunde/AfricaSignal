import os

# Tests run against a real PostgreSQL/PostGIS database, never SQLite.
os.environ["ENV"] = "development"
# Never inherit the application's DATABASE_URL: integration fixtures drop the entire schema.
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://africasignal:africasignal@localhost:5432/africasignal_test",
)


# Local developers can run subsets without system tools. The required release job must
# execute every operations test rather than silently accepting missing prerequisites.
_ops_skips: list[str] = []


def pytest_runtest_logreport(report):
    if os.environ.get("REQUIRE_OPS_TESTS") == "1" and report.skipped:
        if report.nodeid.startswith("tests/ops/"):
            _ops_skips.append(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    if _ops_skips:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter:
            reporter.write_sep("=", "Required operations tests skipped: " + ", ".join(_ops_skips))
        session.exitstatus = 1
