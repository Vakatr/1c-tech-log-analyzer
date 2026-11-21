"""SQL-construction safety tests for :mod:`logt_adapter`.

The review requires that user-supplied filters can never alter query structure:
every value must be bound as a parameter, and identifiers must be validated.
These tests assert that property directly against the SQL text the adapter
builds, without needing a ClickHouse server.
"""

import dataclasses
import datetime as dt
import re
import unittest
from unittest import mock

import logt_adapter
from logt_adapter import Filters

#: Payloads that would break out of a naive string-interpolated query.
INJECTION_PAYLOADS = [
    "'; DROP TABLE events; --",
    "x' OR '1'='1",
    "1; SELECT * FROM system.users",
    "\\' OR 1=1 --",
    "admin'--",
    "{f_dataset:String}",
    "x`; DROP DATABASE tracelog; --",
    "%(f_user)s",
    "CALL ' UNION ALL SELECT 1,2,3 --",
    "0x27 OR 1=1#",
]


class TestWhereClauseIsParameterized(unittest.TestCase):
    """Filters never appear literally in SQL; they are bound as parameters."""

    def test_empty_filters_produce_no_clause(self):
        clause, params = logt_adapter._where_clause(Filters())
        self.assertEqual(clause, "")
        self.assertEqual(params, {})

    def test_dataset_is_bound_not_interpolated(self):
        clause, params = logt_adapter._where_clause(Filters(dataset="ds1"))
        self.assertIn("{f_dataset:String}", clause)
        self.assertNotIn("ds1", clause)
        self.assertEqual(params["f_dataset"], "ds1")

    def test_user_is_bound_not_interpolated(self):
        clause, params = logt_adapter._where_clause(Filters(user="Иванов"))
        self.assertIn("{f_user:String}", clause)
        self.assertNotIn("Иванов", clause)
        self.assertEqual(params["f_user"], "Иванов")

    def test_all_injection_payloads_are_bound_only(self):
        for payload in INJECTION_PAYLOADS:
            with self.subTest(payload=payload):
                filters = Filters(
                    dataset=payload,
                    user=payload,
                    event_name=payload,
                    search=payload,
                )
                clause, params = logt_adapter._where_clause(filters)
                # Nothing user-supplied appears in the SQL text itself.
                for value in (
                    "DROP TABLE",
                    "DROP DATABASE",
                    "OR 1=1",
                    "UNION ALL",
                    "system.users",
                ):
                    self.assertNotIn(value, clause)
                # And every value round-trips through the parameter map.
                self.assertEqual(params["f_dataset"], payload)
                self.assertEqual(params["f_user"], payload)
                self.assertEqual(params["f_event"], payload)
                self.assertEqual(params["f_search"], payload)

    def test_placeholders_are_unique_per_clause(self):
        clause, params = logt_adapter._where_clause(
            Filters(dataset="a", user="b", event_name="c", search="d")
        )
        placeholders = set(__import__("re").findall(r"\{(\w+):", clause))
        self.assertEqual(placeholders, set(params))
        self.assertEqual(len(placeholders), 4)

    def test_dates_are_bound_as_datetimes(self):
        moment = dt.datetime(2026, 1, 2, 3, 4, 5)
        clause, params = logt_adapter._where_clause(Filters(date_from=moment))
        self.assertIn("{f_from:DateTime64(6)}", clause)
        self.assertIsInstance(params["f_from"], dt.datetime)

    def test_aware_datetime_is_converted_to_naive_utc(self):
        aware = dt.datetime(2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone(dt.timedelta(hours=3)))
        _, params = logt_adapter._where_clause(Filters(date_from=aware))
        self.assertIsNone(params["f_from"].tzinfo)
        self.assertEqual(params["f_from"], dt.datetime(2026, 1, 2, 0, 4, 5))


class TestExtendedWhereClause(unittest.TestCase):
    """Process, AppID and duration filters stay parameterized."""

    def test_process_name_uses_normalized_db_name_column(self):
        clause, params = logt_adapter._where_clause(Filters(process_name="  RPhost  "))
        self.assertIn("db_name = {f_process:String}", clause)
        self.assertNotIn("lower(db_name)", clause)
        self.assertNotIn("RPhost", clause)
        self.assertEqual(params["f_process"], "rphost")

    def test_app_id_uses_constant_pattern_and_bound_value(self):
        clause, params = logt_adapter._where_clause(Filters(app_id="1CV8"))
        # The regex is a constant; only the value is bound.
        self.assertIn("extract(event_string, 'AppID=([^,]+)')", clause)
        self.assertIn(
            "lowerUTF8(trimBoth(extract(event_string, 'AppID=([^,]+)'))) "
            "= lowerUTF8(trimBoth({f_appid:String}))",
            clause,
        )
        self.assertNotIn("1CV8", clause)
        self.assertEqual(params["f_appid"], "1CV8")

    def test_min_duration_is_bound_as_microseconds(self):
        clause, params = logt_adapter._where_clause(Filters(min_duration_ms=1500))
        self.assertIn("duration_us >= {f_min_duration_us:UInt64}", clause)
        self.assertNotIn("1500", clause)
        self.assertEqual(params["f_min_duration_us"], 1_500_000)
        self.assertIsInstance(params["f_min_duration_us"], int)

    def test_extended_clauses_follow_the_existing_six(self):
        filters = Filters(
            dataset="ds",
            date_from=dt.datetime(2026, 1, 1),
            date_to=dt.datetime(2026, 1, 2),
            user="u",
            event_name="EXCP",
            search="text",
            process_name="rphost",
            app_id="1CV8",
            min_duration_ms=10,
        )
        clause, _ = logt_adapter._where_clause(filters)
        fragments = [
            "dataset = {f_dataset:String}",
            "ts_event >= {f_from:DateTime64(6)}",
            "ts_event <= {f_to:DateTime64(6)}",
            "user = {f_user:String}",
            "event_name = {f_event:String}",
            "positionCaseInsensitiveUTF8(event_string, {f_search:String})",
            "db_name = {f_process:String}",
            "AppID=([^,]+)",
            "duration_us >= {f_min_duration_us:UInt64}",
        ]
        positions = [clause.index(fragment) for fragment in fragments]
        self.assertEqual(positions, sorted(positions))

    def test_extended_placeholders_are_unique_with_all_filters(self):
        filters = Filters(
            dataset="ds",
            date_from=dt.datetime(2026, 1, 1),
            date_to=dt.datetime(2026, 1, 2),
            user="u",
            event_name="EXCP",
            search="text",
            process_name="rphost",
            app_id="1CV8",
            min_duration_ms=10,
        )
        clause, params = logt_adapter._where_clause(filters)
        placeholders = re.findall(r"\{(\w+):", clause)
        self.assertEqual(len(placeholders), len(set(placeholders)))
        self.assertEqual(set(placeholders), set(params))
        self.assertEqual(len(placeholders), 9)

    def test_extended_injection_payloads_are_bound_only(self):
        for payload in INJECTION_PAYLOADS:
            with self.subTest(payload=payload):
                clause, params = logt_adapter._where_clause(
                    Filters(process_name=f"  {payload}  ", app_id=payload)
                )
                self.assertNotIn(payload, clause)
                for value in ("DROP TABLE", "DROP DATABASE", "OR 1=1", "UNION ALL"):
                    self.assertNotIn(value, clause)
                self.assertEqual(params["f_process"], payload.strip().casefold())
                self.assertEqual(params["f_appid"], payload.strip())



class TestQueriesUseParameterizedPaths(unittest.TestCase):
    """Each public query builds SQL whose filters are fully parameterized.

    The adapter is patched at the connection level so the SQL text can be
    inspected without a server.
    """

    def _capture(self, function, *args, **kwargs):
        """Run ``function`` with a recording connection and return the SQL used."""
        recorded = []

        class Recording:
            """Stand-in connection that records SQL instead of executing it."""

            def __init__(self, config):
                self.config = config

            def execute(self, sql, params=None):
                recorded.append((" ".join(sql.split()), dict(params or {})))
                return []

            def execute_scalar(self, sql, params=None):
                recorded.append((" ".join(sql.split()), dict(params or {})))
                return 0

            def ping(self):
                return True

            def close(self):
                pass

        with mock.patch.object(logt_adapter, "ClickHouseConnection", Recording):
            function(*args, **kwargs)
        return recorded

    def test_query_overview_parameterizes_dataset(self):
        recorded = self._capture(
            logt_adapter.query_overview,
            {"database": "tracelog"},
            Filters(dataset="'; DROP TABLE events; --"),
        )
        self.assertTrue(recorded)
        for sql, params in recorded:
            self.assertNotIn("DROP TABLE", sql)
        self.assertTrue(any("'{ " not in sql for sql, _ in recorded))

    def test_query_errors_parameterizes_and_limits(self):
        recorded = self._capture(
            logt_adapter.query_errors,
            {"database": "tracelog"},
            Filters(user="x' OR '1'='1"),
            page=2,
            page_size=50,
        )
        self.assertTrue(recorded)
        sql = recorded[0][0]
        self.assertIn("LIMIT", sql.upper())
        self.assertIn("OFFSET", sql.upper())
        self.assertNotIn("OR '1'='1", sql)

    def test_query_errors_rejects_absurd_page_size(self):
        with self.assertRaises((ValueError, logt_adapter.AdapterError)):
            logt_adapter.query_errors(
                {"database": "tracelog"}, Filters(), page=0, page_size=10**9
            )

    def test_query_errors_rejects_zero_page_size(self):
        with self.assertRaises((ValueError, logt_adapter.AdapterError)):
            logt_adapter.query_errors(
                {"database": "tracelog"}, Filters(), page=0, page_size=0
            )

    def test_query_errors_rejects_negative_page_size(self):
        with self.assertRaises((ValueError, logt_adapter.AdapterError)):
            logt_adapter.query_errors(
                {"database": "tracelog"}, Filters(), page=0, page_size=-5
            )

    def test_query_errors_rejects_page_size_above_max(self):
        with self.assertRaises((ValueError, logt_adapter.AdapterError)):
            logt_adapter.query_errors(
                {"database": "tracelog"},
                Filters(),
                page=0,
                page_size=logt_adapter.MAX_PAGE_SIZE + 1,
            )

    def test_query_errors_rejects_negative_page(self):
        with self.assertRaises((ValueError, logt_adapter.AdapterError)):
            logt_adapter.query_errors(
                {"database": "tracelog"}, Filters(), page=-1, page_size=10
            )

    def test_query_errors_rejects_non_numeric_page(self):
        with self.assertRaises((ValueError, logt_adapter.AdapterError)):
            logt_adapter.query_errors(
                {"database": "tracelog"}, Filters(), page="not-a-number", page_size=10
            )

    def test_query_errors_accepts_boundary_pagination(self):
        for page, page_size in ((0, 1), (0, logt_adapter.MAX_PAGE_SIZE)):
            with self.subTest(page=page, page_size=page_size):
                recorded = self._capture(
                    logt_adapter.query_errors,
                    {"database": "tracelog"},
                    Filters(),
                    page=page,
                    page_size=page_size,
                )
                self.assertTrue(recorded)
                sql, params = recorded[0]
                self.assertIn(f"LIMIT {page_size}", sql)
                self.assertIn(f"OFFSET {page * page_size}", sql)
                self.assertNotIn("limit", params)

    def test_query_errors_accepts_integer_like_pagination(self):
        recorded = self._capture(
            logt_adapter.query_errors,
            {"database": "tracelog"},
            Filters(),
            page="2",
            page_size="50",
        )
        self.assertTrue(recorded)
        sql, params = recorded[0]
        self.assertIn("LIMIT 50", sql)
        self.assertIn("OFFSET 100", sql)
        self.assertNotIn("limit", params)

    def test_query_errors_rejects_uint64_offset_overflow(self):
        with self.assertRaises((ValueError, logt_adapter.AdapterError)):
            logt_adapter.query_errors(
                {"database": "tracelog"}, Filters(), page=10**20, page_size=1
            )

    def test_query_performance_bounds_limit(self):
        recorded = self._capture(
            logt_adapter.query_performance,
            {"database": "tracelog"},
            Filters(),
            limit=10**6,
        )
        self.assertTrue(recorded)
        self.assertIn("LIMIT", recorded[0][0].upper())

    def test_query_users_is_parameterized(self):
        recorded = self._capture(
            logt_adapter.query_users,
            {"database": "tracelog"},
            Filters(dataset="x'; DROP TABLE events; --"),
        )
        self.assertTrue(recorded)
        self.assertNotIn("DROP TABLE", recorded[0][0])

    def test_query_errors_includes_extended_filters(self):
        recorded = self._capture(
            logt_adapter.query_errors,
            {"database": "tracelog"},
            Filters(process_name="RPhost", app_id="1CV8", min_duration_ms=25),
        )
        self.assertTrue(recorded)
        sql, params = recorded[0]
        self.assertIn("db_name = {f_process:String}", sql)
        self.assertIn("AppID=([^,]+)", sql)
        self.assertIn("duration_us >= {f_min_duration_us:UInt64}", sql)
        self.assertNotIn("RPhost", sql)
        self.assertEqual(params["f_process"], "rphost")
        self.assertEqual(params["f_appid"], "1CV8")
        self.assertEqual(params["f_min_duration_us"], 25_000)

    def test_query_users_includes_extended_filters(self):
        recorded = self._capture(
            logt_adapter.query_users,
            {"database": "tracelog"},
            Filters(process_name="rphost", app_id="1CV8", min_duration_ms=25),
        )
        self.assertTrue(recorded)
        sql, params = recorded[0]
        self.assertIn("db_name = {f_process:String}", sql)
        self.assertIn("AppID=([^,]+)", sql)
        self.assertIn("duration_us >= {f_min_duration_us:UInt64}", sql)
        self.assertEqual(params["f_process"], "rphost")
        self.assertEqual(params["f_appid"], "1CV8")
        self.assertEqual(params["f_min_duration_us"], 25_000)

    def test_query_overview_includes_extended_filters(self):
        recorded = self._capture(
            logt_adapter.query_overview,
            {"database": "tracelog"},
            Filters(process_name="rphost", app_id="1CV8", min_duration_ms=25),
        )
        self.assertTrue(recorded)
        for sql, params in recorded:
            self.assertIn("db_name = {f_process:String}", sql)
            self.assertEqual(params["f_process"], "rphost")
            self.assertEqual(params["f_appid"], "1CV8")
            self.assertEqual(params["f_min_duration_us"], 25_000)

    def test_query_user_events_preserves_extended_filters(self):
        recorded = self._capture(
            logt_adapter.query_user_events,
            "new-user",
            {"database": "tracelog"},
            Filters(
                user="old-user",
                process_name="  RPhost  ",
                app_id="1CV8",
                min_duration_ms=250,
            ),
        )
        self.assertTrue(recorded)
        sql, params = recorded[0]
        self.assertIn("db_name = {f_process:String}", sql)
        self.assertIn("AppID=([^,]+)", sql)
        self.assertIn("duration_us >= {f_min_duration_us:UInt64}", sql)
        self.assertEqual(params["f_user"], "new-user")
        self.assertEqual(params["f_process"], "rphost")
        self.assertEqual(params["f_appid"], "1CV8")
        self.assertEqual(params["f_min_duration_us"], 250_000)


    def test_error_condition_uses_bound_names(self):
        condition, params = logt_adapter._error_condition()
        self.assertNotIn("EXCP", condition)
        self.assertEqual(set(params.values()), set(logt_adapter.ERROR_EVENT_NAMES))


class TestIdentifierValidation(unittest.TestCase):
    """Database names are validated before being quoted into SQL."""

    def test_valid_database_accepted(self):
        config = logt_adapter.resolve_config({"database": "tracelog"})
        self.assertEqual(config["database"], "tracelog")

    def test_injection_database_rejected(self):
        for bad in (
            "tracelog; DROP DATABASE default",
            "tracelog`",
            "tracelog events",
            "1tracelog",
        ):
            with self.subTest(database=bad):
                with self.assertRaises((ValueError, logt_adapter.AdapterError)):
                    logt_adapter.resolve_config({"database": bad})

    def test_blank_database_falls_back_to_default(self):
        """A blank field in the GUI must not override the configured database."""
        config = logt_adapter.resolve_config({"database": ""})
        self.assertEqual(config["database"], logt_adapter.DEFAULT_DATABASE)


class TestFiltersNormalization(unittest.TestCase):
    """``Filters.normalized()`` strips whitespace and blanks empty values."""

    def test_blank_strings_become_none(self):
        normalized = Filters(dataset="   ", user="", search="\t").normalized()
        self.assertIsNone(normalized.dataset)
        self.assertIsNone(normalized.user)
        self.assertIsNone(normalized.search)

    def test_values_are_stripped(self):
        normalized = Filters(user="  Иванов  ").normalized()
        self.assertEqual(normalized.user, "Иванов")


class TestExtendedFilterNormalization(unittest.TestCase):
    """The three new fields normalize without disturbing positional order."""

    def test_new_fields_are_appended_and_positionally_compatible(self):
        self.assertEqual(
            [field.name for field in dataclasses.fields(Filters)],
            [
                "dataset",
                "date_from",
                "date_to",
                "user",
                "event_name",
                "search",
                "process_name",
                "app_id",
                "min_duration_ms",
            ],
        )
        legacy = Filters("ds", None, None, "u", "ev", "text")
        self.assertIsNone(legacy.process_name)
        self.assertIsNone(legacy.app_id)
        self.assertIsNone(legacy.min_duration_ms)

    def test_process_name_is_stripped_and_casefolded(self):
        normalized = Filters(process_name="  RPhost  ").normalized()
        self.assertEqual(normalized.process_name, "rphost")

    def test_blank_extended_fields_become_none(self):
        normalized = Filters(
            process_name="   ", app_id="\t", min_duration_ms=""
        ).normalized()
        self.assertIsNone(normalized.process_name)
        self.assertIsNone(normalized.app_id)
        self.assertIsNone(normalized.min_duration_ms)

    def test_app_id_is_stripped_and_keeps_case(self):
        normalized = Filters(app_id="  1CV8  ").normalized()
        self.assertEqual(normalized.app_id, "1CV8")

    def test_min_duration_accepts_int_and_digit_string(self):
        for value, expected in ((1500, 1500), ("1500", 1500), (" 42 ", 42)):
            with self.subTest(value=value):
                normalized = Filters(min_duration_ms=value).normalized()
                self.assertEqual(normalized.min_duration_ms, expected)
                self.assertIsInstance(normalized.min_duration_ms, int)

    def test_min_duration_rejects_bool_and_non_positive(self):
        for value in (True, False, 0, -1, "0", "-5"):
            with self.subTest(value=value):
                self.assertIsNone(Filters(min_duration_ms=value).normalized().min_duration_ms)

    def test_min_duration_is_clamped_to_upper_bound(self):
        for value in (logt_adapter.MAX_MIN_DURATION_MS, logt_adapter.MAX_MIN_DURATION_MS + 1):
            with self.subTest(value=value):
                normalized = Filters(min_duration_ms=value).normalized()
                self.assertEqual(normalized.min_duration_ms, logt_adapter.MAX_MIN_DURATION_MS)

    def test_min_duration_rejects_non_numeric(self):
        for value in ("abc", "1.5", 1.5, object()):
            with self.subTest(value=value):
                self.assertIsNone(Filters(min_duration_ms=value).normalized().min_duration_ms)



if __name__ == "__main__":
    unittest.main()
