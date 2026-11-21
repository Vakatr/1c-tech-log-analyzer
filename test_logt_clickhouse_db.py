"""ClickHouse integration tests for the adapter's read paths (no LogT needed).

The existing live suite (``test_logt_integration.py``) requires a vendored LogT
checkout, a private fixture directory and a configured server, so it cannot run
in CI.  This module keeps the parts that only need ClickHouse itself:

* a throw-away database ``pytest_db_<uuid8>`` is created with ``CREATE
  DATABASE`` (the application deliberately never creates databases);
* ``logt_adapter.ensure_schema`` builds the LogT tables in that database;
* deterministic rows are inserted straight through ``clickhouse-driver``;
* every read surface — overview, errors, performance, users — is exercised;
* the database is dropped in ``tearDownClass`` even when a test fails.

Opt in with ``CLICKHOUSE_TEST=1``.  If the variable is set but the server is
unreachable the tests fail (they do not skip), so a broken CI service container
is visible instead of silently green.  Without the variable the module is
skipped entirely, which is what the default offline run expects.

Run locally with::

    set CLICKHOUSE_TEST=1
    python -m pytest test_logt_clickhouse_db.py -q -rs
"""

import datetime as dt
import os
import unittest
import uuid

import pytest

import logt_adapter
import logt_schema

pytestmark = [
    pytest.mark.db,
    pytest.mark.skipif(
        os.environ.get("CLICKHOUSE_TEST") != "1",
        reason="Set CLICKHOUSE_TEST=1 to run ClickHouse integration tests",
    ),
]


#: Dataset tags written into the ``dataset`` column.  Two of them prove that
#: ``Filters(dataset=...)`` isolates rows without relying on users alone.
DATASET_MAIN = "pytest_ds_main"
DATASET_OTHER = "pytest_ds_other"

#: Synthetic users.  No real account names appear in tests or fixtures.
USER_ALPHA = "user_alpha"
USER_BETA = "user_beta"

#: Column order used for the direct INSERT.  Read from the schema module so the
#: test does not drift when a column is added.
EVENT_COLUMNS = tuple(
    logt_schema.TABLE_SPECS[logt_schema.TABLE_EVENTS].column_names
)

#: Datasets in which the seeded error events live.
MAIN_ERROR_COUNT = 3
OTHER_ERROR_COUNT = 1

#: Total seeded events (6 in the main dataset, 2 in the other one).
TOTAL_EVENTS = 8


def _event(
    *,
    dataset,
    timestamp,
    user,
    db_name,
    event_name,
    duration_us,
    event_string,
    session_id,
    client_id,
    connect_id,
    context="",
):
    """Build one ``events`` row from concise, readable arguments.

    Args:
        dataset: Value of the ``dataset`` column.
        timestamp: Timezone-aware UTC timestamp for the event.
        user: 1C user name.
        db_name: 1C infobase name.
        event_name: Event code (``CALL``, ``EXCP``, ...).
        duration_us: Event duration in microseconds.
        event_string: Raw 1C event text, as LogT stores it.
        session_id: 1C session identifier.
        client_id: 1C client identifier.
        connect_id: 1C connection identifier.
        context: Optional ``context`` column value.

    Returns:
        Dictionary with every ``events`` column populated.
    """
    return {
        "dataset": dataset,
        "db_name": db_name,
        "session_id": session_id,
        "client_id": client_id,
        "connect_id": connect_id,
        "user": user,
        "ts_event_us": timestamp,
        "ts_event": timestamp.replace(microsecond=0),
        "ts_vrsrequest_us": timestamp,
        "ts_vrsresponse_us": timestamp,
        "event_name": event_name,
        "duration_us": duration_us,
        "space_us": 0,
        "event_string": event_string,
        "context": context,
    }


#: Deterministic seed data.  Durations are deliberately distinct so the
#: performance ordering is unambiguous; the main dataset has two explicit
#: ``Descr="..."`` values plus one error without one (the 200-character excerpt
#: fallback), which pins the ``description_is_excerpt`` flag.
SEEDED_EVENTS = (
    _event(
        dataset=DATASET_MAIN,
        timestamp=dt.datetime(2026, 1, 1, 1, 0, 0, 1000, tzinfo=dt.timezone.utc),
        user=USER_ALPHA,
        db_name="db_alpha",
        event_name="CALL",
        duration_us=1_000,
        event_string=(
            'N=CALL, Usr="user_alpha", p:processName=db_alpha, '
            'Context="Открыть форму"'
        ),
        session_id=1001,
        client_id=101,
        connect_id=201,
        context="ctx-alpha-1",
    ),
    _event(
        dataset=DATASET_MAIN,
        timestamp=dt.datetime(2026, 1, 1, 1, 0, 1, 2000, tzinfo=dt.timezone.utc),
        user=USER_ALPHA,
        db_name="db_alpha",
        event_name="CALL",
        duration_us=5_000,
        event_string=(
            'N=CALL, Usr="user_alpha", p:processName=db_alpha, '
            'Context="Провести документ"'
        ),
        session_id=1002,
        client_id=102,
        connect_id=202,
        context="ctx-alpha-2",
    ),
    _event(
        dataset=DATASET_MAIN,
        timestamp=dt.datetime(2026, 1, 1, 1, 0, 2, 3000, tzinfo=dt.timezone.utc),
        user=USER_ALPHA,
        db_name="db_beta",
        event_name="DBMSSQL",
        duration_us=20_000,
        event_string=(
            'N=DBMSSQL, Usr="user_alpha", p:processName=db_beta, '
            'Context="SELECT 1"'
        ),
        session_id=1003,
        client_id=103,
        connect_id=203,
        context="ctx-alpha-3",
    ),
    _event(
        dataset=DATASET_MAIN,
        timestamp=dt.datetime(2026, 1, 1, 1, 0, 3, 4000, tzinfo=dt.timezone.utc),
        user=USER_BETA,
        db_name="db_alpha",
        event_name="EXCP",
        duration_us=3_000,
        event_string=(
            'N=EXCP, Usr="user_beta", Descr="Ошибка выполнения операции", '
            "t:clientID=104"
        ),
        session_id=1004,
        client_id=104,
        connect_id=204,
        context="ctx-beta-1",
    ),
    _event(
        dataset=DATASET_MAIN,
        timestamp=dt.datetime(2026, 1, 1, 1, 0, 4, 5000, tzinfo=dt.timezone.utc),
        user=USER_BETA,
        db_name="db_alpha",
        event_name="EXCPCNTX",
        duration_us=1_000,
        event_string=(
            'N=EXCPCNTX, Usr="user_beta", Descr="Контекст ошибки"'
        ),
        session_id=1005,
        client_id=105,
        connect_id=205,
        context="ctx-beta-2",
    ),
    _event(
        dataset=DATASET_MAIN,
        timestamp=dt.datetime(2026, 1, 1, 1, 0, 5, 6000, tzinfo=dt.timezone.utc),
        user=USER_BETA,
        db_name="db_beta",
        event_name="EXCP",
        duration_us=7_000,
        # Deliberately no Descr: the description must fall back to the first
        # 200 characters of event_string and set description_is_excerpt.
        event_string='N=EXCP, Usr="user_beta", p:processName=db_beta',
        session_id=1006,
        client_id=106,
        connect_id=206,
        context="ctx-beta-3",
    ),
    _event(
        dataset=DATASET_OTHER,
        timestamp=dt.datetime(2026, 1, 1, 1, 0, 10, 7000, tzinfo=dt.timezone.utc),
        user=USER_ALPHA,
        db_name="db_alpha",
        event_name="CALL",
        duration_us=50_000,
        event_string='N=CALL, Usr="user_alpha", p:processName=db_alpha',
        session_id=1007,
        client_id=107,
        connect_id=207,
        context="ctx-other-1",
    ),
    _event(
        dataset=DATASET_OTHER,
        timestamp=dt.datetime(2026, 1, 1, 1, 0, 11, 8000, tzinfo=dt.timezone.utc),
        user=USER_BETA,
        db_name="db_beta",
        event_name="EXCP",
        duration_us=9_000,
        event_string=(
            'N=EXCP, Usr="user_beta", Descr="Отдельная ошибка"'
        ),
        session_id=1008,
        client_id=108,
        connect_id=208,
        context="ctx-other-2",
    ),
)


class ClickHouseQuerySurfaceTests(unittest.TestCase):
    """Exercise every adapter query against a real, isolated database."""

    @classmethod
    def setUpClass(cls):
        """Create the ephemeral database, build the schema and seed rows."""
        cls.admin_config = logt_adapter.resolve_config({"database": "default"})
        cls.db_name = "pytest_db_" + uuid.uuid4().hex[:8]
        cls.config = logt_adapter.resolve_config({"database": cls.db_name})
        try:
            cls._create_database()
            status = logt_adapter.ensure_schema(cls.config)
            if not status.ready:
                raise AssertionError(
                    f"ensure_schema reported problems: {status.problems}"
                )
            cls._seed_events()
        except BaseException:
            # unittest does not call tearDownClass when setUpClass fails, so
            # drop the database here to avoid leaking it on a setup error.
            # Cleanup is best effort: when the server itself is unreachable the
            # original (more useful) connection error must stay visible.
            try:
                cls._drop_database()
            except Exception:
                pass
            raise

    @classmethod
    def tearDownClass(cls):
        """Drop the ephemeral database even when a test failed."""
        try:
            pass
        finally:
            cls._drop_database()

    # -- setup helpers -----------------------------------------------------
    @classmethod
    def _create_database(cls) -> None:
        """Create the ephemeral database through an existing one."""
        connection = logt_adapter.ClickHouseConnection(cls.admin_config)
        try:
            connection.execute(f"CREATE DATABASE `{cls.db_name}`")
        finally:
            connection.close()

    @classmethod
    def _drop_database(cls) -> None:
        """Drop the ephemeral database, if it was created."""
        if not getattr(cls, "db_name", ""):
            return
        connection = logt_adapter.ClickHouseConnection(cls.admin_config)
        try:
            connection.execute(f"DROP DATABASE IF EXISTS `{cls.db_name}`")
        finally:
            connection.close()

    @classmethod
    def _seed_events(cls) -> None:
        """Insert the deterministic rows and verify the insert landed."""
        table = logt_schema.qualified_table(cls.db_name, logt_schema.TABLE_EVENTS)
        rows = [
            tuple(event[name] for name in EVENT_COLUMNS)
            for event in SEEDED_EVENTS
        ]
        columns = ", ".join(EVENT_COLUMNS)
        connection = logt_adapter.ClickHouseConnection(cls.config)
        try:
            connection.execute(
                f"INSERT INTO {table} ({columns}) VALUES", rows
            )
            stored = connection.execute_scalar(f"SELECT count() FROM {table}")
        finally:
            connection.close()
        if stored != len(SEEDED_EVENTS):
            raise AssertionError(
                f"seed verification failed: stored {stored}, "
                f"expected {len(SEEDED_EVENTS)}"
            )

    def _event_count(self, dataset=None) -> int:
        """Return the number of seeded events, optionally for one dataset."""
        table = logt_schema.qualified_table(self.db_name, logt_schema.TABLE_EVENTS)
        connection = logt_adapter.ClickHouseConnection(self.config)
        try:
            if dataset is None:
                return connection.execute_scalar(f"SELECT count() FROM {table}")
            return connection.execute_scalar(
                f"SELECT count() FROM {table} "
                f"WHERE {logt_schema.DATASET_COLUMN} = {{ds:String}}",
                {"ds": dataset},
            )
        finally:
            connection.close()

    @staticmethod
    def _main_filters() -> "logt_adapter.Filters":
        """Return normalized filters for the main seeded dataset."""
        return logt_adapter.Filters(dataset=DATASET_MAIN).normalized()

    # -- overview ----------------------------------------------------------
    def test_overview_totals_and_top_lists(self):
        """Overview aggregates come back exactly for the filtered dataset."""
        overview = logt_adapter.query_overview(
            config=self.config, filters=self._main_filters(), top_n=10
        )

        self.assertEqual(
            overview["totals"],
            {
                "events": 6,
                "users": 2,
                "event_types": 4,
                "datasets": 1,
                "errors": MAIN_ERROR_COUNT,
            },
        )
        self.assertEqual(overview["time_range"]["first"], "2026-01-01 01:00:00")
        self.assertEqual(overview["time_range"]["last"], "2026-01-01 01:00:05")
        self.assertEqual(
            overview["datasets"], [{"dataset": DATASET_MAIN, "count": 6}]
        )

        events = {row["name"]: row for row in overview["top_events"]}
        self.assertEqual(
            {name: row["count"] for name, row in events.items()},
            {"CALL": 2, "EXCP": 2, "DBMSSQL": 1, "EXCPCNTX": 1},
        )
        self.assertEqual(events["CALL"]["total_ms"], 6.0)
        self.assertEqual(events["CALL"]["max_ms"], 5.0)
        self.assertEqual(events["EXCP"]["total_ms"], 10.0)
        self.assertEqual(events["EXCP"]["max_ms"], 7.0)

        users = {row["user"]: row for row in overview["top_users"]}
        self.assertEqual(
            {name: (row["count"], row["errors"]) for name, row in users.items()},
            {USER_ALPHA: (3, 0), USER_BETA: (3, MAIN_ERROR_COUNT)},
        )

    def test_dataset_filter_isolates_rows(self):
        """A second dataset is invisible through the main-dataset filter."""
        other_filters = logt_adapter.Filters(dataset=DATASET_OTHER).normalized()
        overview = logt_adapter.query_overview(
            config=self.config, filters=other_filters
        )

        self.assertEqual(overview["totals"]["events"], 2)
        self.assertEqual(overview["totals"]["users"], 2)
        self.assertEqual(overview["totals"]["event_types"], 2)
        self.assertEqual(overview["totals"]["datasets"], 1)
        self.assertEqual(overview["totals"]["errors"], OTHER_ERROR_COUNT)
        self.assertEqual(
            logt_adapter.count_errors(self.config, other_filters),
            OTHER_ERROR_COUNT,
        )
        self.assertEqual(
            self._event_count(DATASET_MAIN), 6, "main dataset must be unchanged"
        )

    # -- errors ------------------------------------------------------------
    def test_count_errors_counts_both_error_codes(self):
        """``EXCP`` and ``EXCPCNTX`` are both errors; other events are not."""
        self.assertEqual(
            logt_adapter.count_errors(self.config, self._main_filters()),
            MAIN_ERROR_COUNT,
        )

    def test_error_pagination_is_stable_and_complete(self):
        """Pages slice one deterministic newest-first list without gaps."""
        filters = self._main_filters()
        full = logt_adapter.query_errors(
            config=self.config, filters=filters, page=0, page_size=10
        )
        first = logt_adapter.query_errors(
            config=self.config, filters=filters, page=0, page_size=2
        )
        second = logt_adapter.query_errors(
            config=self.config, filters=filters, page=1, page_size=2
        )
        third = logt_adapter.query_errors(
            config=self.config, filters=filters, page=2, page_size=2
        )

        self.assertEqual(
            [row["time"] for row in full],
            ["2026-01-01 01:00:05", "2026-01-01 01:00:04", "2026-01-01 01:00:03"],
        )
        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 1)
        self.assertEqual(third, [])
        self.assertEqual(first + second, full)
        for row in full:
            self.assertIn(row["event"], {"EXCP", "EXCPCNTX"})
            self.assertIn(row["user"], {USER_ALPHA, USER_BETA})
            self.assertIn(row["db_name"], {"db_alpha", "db_beta"})

    def test_errors_expose_description_and_excerpt_flag(self):
        """Explicit ``Descr`` values are extracted; a missing one is an excerpt."""
        rows = logt_adapter.query_errors(
            config=self.config, filters=self._main_filters(), page=0, page_size=10
        )
        by_time = {row["time"]: row for row in rows}

        explicit = by_time["2026-01-01 01:00:03"]
        self.assertEqual(explicit["description"], "Ошибка выполнения операции")
        self.assertFalse(explicit["description_is_excerpt"])

        context = by_time["2026-01-01 01:00:04"]
        self.assertEqual(context["description"], "Контекст ошибки")
        self.assertFalse(context["description_is_excerpt"])

        excerpt = by_time["2026-01-01 01:00:05"]
        self.assertTrue(excerpt["description_is_excerpt"])
        self.assertTrue(excerpt["description"].startswith("N=EXCP"))
        self.assertNotIn('Descr=', excerpt["description"])

    # -- performance -------------------------------------------------------
    def test_performance_lists_slowest_events_first(self):
        """Individual events are ordered by duration, descending."""
        rows = logt_adapter.query_performance(
            config=self.config, filters=self._main_filters(), limit=3
        )

        self.assertEqual(
            [row["duration_ms"] for row in rows], [20.0, 7.0, 5.0]
        )
        self.assertEqual(
            [(row["time"], row["event"]) for row in rows],
            [
                ("2026-01-01 01:00:02", "DBMSSQL"),
                ("2026-01-01 01:00:05", "EXCP"),
                ("2026-01-01 01:00:01", "CALL"),
            ],
        )
        for row in rows:
            self.assertIn(row["user"], {USER_ALPHA, USER_BETA})

    def test_performance_by_event_aggregates(self):
        """Per-event aggregates are computed server-side and ordered by total."""
        rows = logt_adapter.query_performance_by_event(
            config=self.config, filters=self._main_filters(), limit=10
        )

        self.assertEqual(
            [row["event"] for row in rows],
            ["DBMSSQL", "EXCP", "CALL", "EXCPCNTX"],
        )
        by_event = {row["event"]: row for row in rows}
        self.assertEqual(by_event["CALL"]["count"], 2)
        self.assertEqual(by_event["CALL"]["sum_ms"], 6.0)
        self.assertEqual(by_event["CALL"]["avg_ms"], 3.0)
        self.assertEqual(by_event["CALL"]["max_ms"], 5.0)
        self.assertEqual(by_event["EXCP"]["count"], 2)
        self.assertEqual(by_event["EXCP"]["sum_ms"], 10.0)
        self.assertEqual(by_event["EXCP"]["max_ms"], 7.0)
        self.assertEqual(by_event["EXCPCNTX"]["count"], 1)
        for row in rows:
            self.assertGreaterEqual(row["max_ms"], row["avg_ms"])
            self.assertGreaterEqual(row["p95_ms"], 0.0)
            self.assertLessEqual(row["p95_ms"], row["max_ms"])

    # -- users -------------------------------------------------------------
    def test_users_are_aggregated_per_dataset(self):
        """Per-user counts and seen-range match the seeded rows."""
        rows = logt_adapter.query_users(self.config, self._main_filters())

        self.assertEqual(len(rows), 2)
        by_user = {row["user"]: row for row in rows}
        self.assertEqual(by_user[USER_ALPHA]["events"], 3)
        self.assertEqual(by_user[USER_ALPHA]["errors"], 0)
        self.assertEqual(by_user[USER_ALPHA]["first_seen"], "2026-01-01 01:00:00")
        self.assertEqual(by_user[USER_ALPHA]["last_seen"], "2026-01-01 01:00:02")
        self.assertEqual(by_user[USER_BETA]["events"], 3)
        self.assertEqual(by_user[USER_BETA]["errors"], MAIN_ERROR_COUNT)
        self.assertEqual(by_user[USER_BETA]["first_seen"], "2026-01-01 01:00:03")
        self.assertEqual(by_user[USER_BETA]["last_seen"], "2026-01-01 01:00:05")
        for row in rows:
            self.assertNotEqual(row["user"], "")

    def test_user_events_are_scoped_to_user_and_dataset(self):
        """The drill-down lists only the requested user's events."""
        rows = logt_adapter.query_user_events(
            USER_BETA, config=self.config, filters=self._main_filters(), limit=10
        )

        self.assertEqual(
            [row["time"] for row in rows],
            ["2026-01-01 01:00:05", "2026-01-01 01:00:04", "2026-01-01 01:00:03"],
        )
        self.assertEqual(
            [row["event"] for row in rows], ["EXCP", "EXCPCNTX", "EXCP"]
        )
        self.assertEqual(
            [row["description_is_excerpt"] for row in rows], [True, False, False]
        )
        self.assertEqual(rows[1]["description"], "Контекст ошибки")
        self.assertEqual(rows[2]["description"], "Ошибка выполнения операции")
        self.assertEqual([row["duration_ms"] for row in rows], [7.0, 1.0, 3.0])
        for row in rows:
            self.assertIn(row["db_name"], {"db_alpha", "db_beta"})

        other_filters = logt_adapter.Filters(dataset=DATASET_OTHER).normalized()
        other_rows = logt_adapter.query_user_events(
            USER_BETA, config=self.config, filters=other_filters, limit=10
        )
        self.assertEqual(len(other_rows), 1)
        self.assertEqual(other_rows[0]["description"], "Отдельная ошибка")

    # -- safety and schema -------------------------------------------------
    def test_hostile_filter_value_is_treated_as_data(self):
        """A SQL-injection payload matches nothing and drops nothing."""
        hostile = "'; DROP TABLE events; --"
        hostile_user = logt_adapter.Filters(
            dataset=DATASET_MAIN, user=hostile
        ).normalized()
        hostile_dataset = logt_adapter.Filters(dataset=hostile).normalized()

        self.assertEqual(
            logt_adapter.query_users(self.config, hostile_user), []
        )
        self.assertEqual(logt_adapter.count_errors(self.config, hostile_user), 0)
        self.assertEqual(
            logt_adapter.query_errors(self.config, hostile_user), []
        )
        self.assertEqual(
            logt_adapter.query_user_events(
                hostile, config=self.config, filters=self._main_filters()
            ),
            [],
        )
        self.assertEqual(
            logt_adapter.query_overview(self.config, hostile_user)["totals"][
                "events"
            ],
            0,
        )
        self.assertEqual(
            logt_adapter.count_errors(self.config, hostile_dataset), 0
        )

        # The payload must not have removed or corrupted the seeded table.
        table = logt_schema.qualified_table(
            self.db_name, logt_schema.TABLE_EVENTS
        )
        connection = logt_adapter.ClickHouseConnection(self.config)
        try:
            remaining = connection.execute_scalar(f"SELECT count() FROM {table}")
            columns = connection.read_columns()
        finally:
            connection.close()
        self.assertEqual(remaining, TOTAL_EVENTS)
        self.assertIn(logt_schema.TABLE_EVENTS, columns)

    def test_ensure_schema_is_idempotent(self):
        """A second bootstrap creates nothing and preserves the seeded rows."""
        before = self._event_count()
        status = logt_adapter.ensure_schema(self.config)

        self.assertTrue(status.ready, msg=status.message)
        self.assertEqual(status.created, [])
        self.assertEqual(status.problems, [])
        self.assertEqual(self._event_count(), before)
        validation = logt_adapter.validate_schema(self.config)
        self.assertTrue(validation.ready, msg=validation.message)

    def test_schema_metadata_matches_table_specs(self):
        """The created tables expose the declared columns and engine."""
        connection = logt_adapter.ClickHouseConnection(self.config)
        try:
            self.assertTrue(connection.ping())
            columns = connection.read_columns()
            engines = connection.table_engines()
        finally:
            connection.close()

        self.assertEqual(set(columns), set(logt_schema.TABLE_SPECS))
        self.assertEqual(
            set(columns[logt_schema.TABLE_EVENTS]),
            set(logt_schema.TABLE_SPECS[logt_schema.TABLE_EVENTS].columns),
        )
        for table, engine in engines.items():
            self.assertEqual(engine, "MergeTree", table)

    def test_count_tables_reports_seeded_rows(self):
        """Row counts are read from ClickHouse, per dataset and in total."""
        per_dataset = logt_adapter.count_tables(
            self.config, dataset=DATASET_MAIN
        )
        total = logt_adapter.count_tables(self.config)

        self.assertEqual(per_dataset[logt_schema.TABLE_EVENTS], 6)
        self.assertEqual(total[logt_schema.TABLE_EVENTS], TOTAL_EVENTS)
        for table in logt_schema.TABLE_SPECS:
            self.assertGreaterEqual(per_dataset[table], 0)
            self.assertGreaterEqual(total[table], 0)


if __name__ == "__main__":
    unittest.main()
