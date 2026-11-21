"""Integration tests that require a running ClickHouse server.

Live runs are opt-in: the module is marked ``live`` and every test is skipped
unless ``CLICKHOUSE_TEST=1`` is set *and* credentials are available (either
``CLICKHOUSE_PASSWORD`` or a local ``config/clickhouse.local.json``).  Merely
having a local config is not enough, so the default test run stays offline and
never probes the network.  Run them explicitly with::

    $env:CLICKHOUSE_TEST = "1"
    python -m pytest test_logt_integration.py -v -m live

They import a small fixture through LogT and then assert that all four tab
queries return well-formed, non-empty results.
"""

import os
import unittest
from pathlib import Path

import pytest

import logt_adapter

pytestmark = pytest.mark.live

DATASET = "pytest_integration"
FIXTURE = Path(__file__).parent / ".tmp" / "fixture"


def _clickhouse_configured() -> bool:
    """Return whether live tests are explicitly enabled and credentials exist."""
    if os.environ.get("CLICKHOUSE_TEST") != "1":
        return False
    if os.environ.get("CLICKHOUSE_PASSWORD"):
        return True
    return (Path(logt_adapter.REPO_ROOT) / "config" / "clickhouse.local.json").exists()


def _reachable() -> bool:
    """Return whether the configured ClickHouse answers a trivial query."""
    try:
        return logt_adapter.check_clickhouse_connection()
    except Exception:
        return False


CONFIGURED = _clickhouse_configured()
LIVE = CONFIGURED and _reachable()


@unittest.skipUnless(CONFIGURED, "ClickHouse is not configured")
@unittest.skipUnless(LIVE, "ClickHouse is not reachable")
class TestLiveClickHouse(unittest.TestCase):
    """End-to-end checks against a real server."""

    @classmethod
    def setUpClass(cls):
        """Ensure the schema exists and the fixture is imported."""
        cls.config = logt_adapter.resolve_config()
        status = logt_adapter.ensure_schema(cls.config)
        if not status.ready:
            raise unittest.SkipTest(f"Schema not usable: {status.message}")
        if not FIXTURE.exists():
            raise unittest.SkipTest(f"fixture not found at {FIXTURE}")
        # Import once for the whole class: tests below only read.
        cls.import_result = logt_adapter.run_logt_import(
            str(FIXTURE), cls.config, dataset_name=DATASET
        )

    def test_connection(self):
        self.assertTrue(logt_adapter.check_clickhouse_connection(self.config))

    def test_ensure_schema_is_idempotent_against_live_server(self):
        first = logt_adapter.ensure_schema(self.config)
        second = logt_adapter.ensure_schema(self.config)
        self.assertTrue(first.ready)
        self.assertTrue(second.ready)
        self.assertEqual(second.created, [], "second run must create nothing")

    def test_validate_schema_passes(self):
        status = logt_adapter.validate_schema(self.config)
        self.assertTrue(status.ready, msg=status.message)

    def test_import_fixture_then_query_all_tabs(self):
        result = self.import_result
        self.assertTrue(result.success, msg=result.error)
        self.assertGreater(result.rows_loaded, 0)

        filters = logt_adapter.Filters(dataset=DATASET).normalized()

        # Overview ---------------------------------------------------------
        overview = logt_adapter.query_overview(self.config, filters)
        self.assertIn("totals", overview)
        self.assertGreater(overview["totals"]["events"], 0)
        self.assertGreater(overview["totals"]["event_types"], 0)
        self.assertTrue(overview["top_events"])
        for row in overview["top_events"]:
            self.assertIn("name", row)
            self.assertGreaterEqual(row["count"], 1)

        # Errors -----------------------------------------------------------
        total = logt_adapter.count_errors(self.config, filters)
        self.assertGreaterEqual(total, 0)
        rows = logt_adapter.query_errors(self.config, filters, page=0, page_size=10)
        self.assertLessEqual(len(rows), 10)
        for row in rows:
            self.assertIn("time", row)
            self.assertIn("event", row)
            self.assertIn("description", row)

        # Performance ------------------------------------------------------
        slowest = logt_adapter.query_performance(self.config, filters, limit=5)
        self.assertLessEqual(len(slowest), 5)
        if slowest:
            durations = [row["duration_ms"] for row in slowest]
            self.assertEqual(durations, sorted(durations, reverse=True))
        aggregates = logt_adapter.query_performance_by_event(self.config, filters, limit=5)
        for row in aggregates:
            self.assertGreaterEqual(row["max_ms"], row["avg_ms"])
            self.assertGreaterEqual(row["count"], 1)

        # Users ------------------------------------------------------------
        users = logt_adapter.query_users(self.config, filters)
        self.assertTrue(users)
        for row in users:
            self.assertNotEqual(row["user"], "", "empty user must be excluded")
            self.assertGreaterEqual(row["events"], 1)
            self.assertGreaterEqual(row["errors"], 0)

        # Pagination must not repeat rows across pages ---------------------
        page0 = logt_adapter.query_errors(self.config, filters, page=0, page_size=1)
        page1 = logt_adapter.query_errors(self.config, filters, page=1, page_size=1)
        if page0 and page1:
            self.assertNotEqual(page0[0]["time"], page1[0]["time"])

    def test_filters_change_results(self):
        filters_all = logt_adapter.Filters(dataset=DATASET).normalized()
        users = logt_adapter.query_users(self.config, filters_all)
        if not users:
            self.skipTest("no users in the imported dataset")
        target = users[0]["user"]

        filtered = logt_adapter.Filters(dataset=DATASET, user=target).normalized()
        narrowed = logt_adapter.query_users(self.config, filtered)
        self.assertEqual(len(narrowed), 1)
        self.assertEqual(narrowed[0]["user"], target)

        # A non-existent user must return nothing at all.
        absent = logt_adapter.Filters(
            dataset=DATASET, user="no-such-user-12345"
        ).normalized()
        self.assertEqual(logt_adapter.query_users(self.config, absent), [])
        self.assertEqual(logt_adapter.count_errors(self.config, absent), 0)

    def test_injection_attempt_returns_nothing(self):
        """A hostile filter value must be treated as data, not SQL."""
        filters = logt_adapter.Filters(
            dataset=DATASET, user="'; DROP TABLE events; --"
        ).normalized()
        users = logt_adapter.query_users(self.config, filters)
        self.assertEqual(users, [])
        # And the table must still exist afterwards.
        status = logt_adapter.validate_schema(self.config)
        self.assertTrue(status.ready, msg=status.message)


if __name__ == "__main__":
    unittest.main()
