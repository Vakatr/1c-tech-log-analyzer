"""Tests for the derived error-description SQL (DEBT-007).

The LogT ``events`` table has no dedicated ``descr`` column, so the adapter
derives the description from ``event_string`` entirely inside ClickHouse.  These
tests inspect the generated SQL and the row mapping with a fake connection; no
ClickHouse server or display is required.
"""

import datetime as dt
import re
import unittest

import logt_adapter

try:  # Tk is optional in headless environments (same guard as the GUI tests).
    import ics_log_analyzer as gui
except Exception:  # pragma: no cover - no Tk on this machine
    gui = None


class FakeConnection:
    """Stand-in connection returning canned rows and recording SQL."""

    def __init__(self, rows):
        """Store the rows to return and reset the query log.

        Args:
            rows: Rows returned by :meth:`execute`.
        """
        self.config = {"database": "tracelog"}
        self.rows = rows
        self.queries = []
        self.closed = False

    def execute(self, sql, params=None):
        """Record the normalized SQL text and return the canned rows."""
        self.queries.append((" ".join(sql.split()), dict(params or {})))
        return self.rows

    def close(self):
        """Mark the fake connection as closed."""
        self.closed = True


class TestDescriptionExpressions(unittest.TestCase):
    """The helper builds pure, parameter-free ClickHouse SQL."""

    def test_expression_uses_expected_clickhouse_functions(self):
        description, is_excerpt = logt_adapter.build_description_expressions()
        for fragment in (
            "position(event_string, 'Descr=')",
            "extract(event_string",
            "trim(BOTH",
            "replaceAll(",
            "substring(event_string, 1, 200)",
            "length(",
            "if(",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, description)
        self.assertIn("length(", is_excerpt)
        self.assertTrue(is_excerpt.endswith("= 0"))

    def test_expressions_contain_no_query_placeholders(self):
        description, is_excerpt = logt_adapter.build_description_expressions()
        self.assertNotIn("{", description)
        self.assertNotIn("{", is_excerpt)

    def test_backwards_compatible_constant_is_a_template(self):
        self.assertIn("{es}", logt_adapter.DESCRIPTION_EXPR)
        expanded = logt_adapter.DESCRIPTION_EXPR.replace(
            "{es}", logt_adapter.EVENT_STRING_COLUMN
        )
        description, _ = logt_adapter.build_description_expressions()
        self.assertEqual(expanded, description)

    def test_pattern_matches_quoted_bare_and_escaped_values(self):
        pattern = re.compile(logt_adapter.DESCRIPTION_VALUE_PATTERN)
        cases = [
            ('N=EXCP, Descr="Ошибка, при выполнении", Usr=Иванов',
             '"Ошибка, при выполнении"'),
            ("N=EXCP, Descr='src\\path(1129), line'", "'src\\path(1129), line'"),
            ("N=EXCP, Descr=Просто текст, Usr=Иванов", "Просто текст"),
            ('N=EXCP, Descr="Ошибка \\"X\\""', '"Ошибка \\"X\\""'),
            ("13.09.2026\nN=EXCP, Descr=Документ.Модуль(125)", "Документ.Модуль(125)"),
            ('N=EXCP Usr=Иванов Descr="space separated"', '"space separated"'),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                match = pattern.search(text)
                self.assertIsNotNone(match)
                self.assertEqual(match.group(1), expected)

    def test_pattern_has_exactly_one_capturing_group(self):
        """ClickHouse ``extract`` returns the first capturing group."""
        self.assertEqual(re.compile(logt_adapter.DESCRIPTION_VALUE_PATTERN).groups, 1)

    def test_pattern_rejects_events_without_descr(self):
        pattern = re.compile(logt_adapter.DESCRIPTION_VALUE_PATTERN)
        self.assertIsNone(pattern.search("N=CALL, Usr=Иванов"))


class TestQueryErrorsDescription(unittest.TestCase):
    """``query_errors`` maps the SQL flag to ``description_is_excerpt``."""

    def test_rows_are_mapped_with_excerpt_flag(self):
        rows = [
            (dt.datetime(2026, 1, 1, 10, 0, 0), "EXCP", "db1", "u1", "Ошибка SQL", 0),
            (dt.datetime(2026, 1, 1, 10, 0, 1), "EXCP", "db1", "u1", "Первые 200", 1),
        ]
        connection = FakeConnection(rows)

        result = logt_adapter.query_errors(connection=connection, page=0, page_size=10)

        self.assertEqual(result[0]["description"], "Ошибка SQL")
        self.assertFalse(result[0]["description_is_excerpt"])
        self.assertEqual(result[1]["description"], "Первые 200")
        self.assertTrue(result[1]["description_is_excerpt"])
        self.assertTrue(connection.queries)

    def test_sql_uses_the_shared_expressions(self):
        connection = FakeConnection([])
        logt_adapter.query_errors(connection=connection)

        sql = connection.queries[0][0]
        description, is_excerpt = logt_adapter.build_description_expressions()
        self.assertIn(description, sql)
        self.assertIn(is_excerpt, sql)
        self.assertIn("AS descr_is_excerpt", sql)
        self.assertIn("LIMIT", sql.upper())
        self.assertIn("OFFSET", sql.upper())


class TestQueryUserEventsDescription(unittest.TestCase):
    """``query_user_events`` exposes the same flag and keeps duration mapping."""

    def test_rows_are_mapped_with_excerpt_flag(self):
        rows = [
            (dt.datetime(2026, 1, 1, 10, 0, 0), "CALL", "db1", "вызов", 1500, 0),
            (dt.datetime(2026, 1, 1, 10, 0, 1), "CALL", "db1", "фрагмент", 0, 1),
        ]
        connection = FakeConnection(rows)

        result = logt_adapter.query_user_events("u1", connection=connection)

        self.assertEqual(result[0]["description"], "вызов")
        self.assertFalse(result[0]["description_is_excerpt"])
        self.assertEqual(result[0]["duration_ms"], 1.5)
        self.assertTrue(result[1]["description_is_excerpt"])

    def test_sql_uses_the_shared_expressions(self):
        connection = FakeConnection([])
        logt_adapter.query_user_events("u1", connection=connection)

        sql = connection.queries[0][0]
        description, is_excerpt = logt_adapter.build_description_expressions()
        self.assertIn(description, sql)
        self.assertIn(is_excerpt, sql)
        self.assertIn("AS descr_is_excerpt", sql)


@unittest.skipUnless(gui is not None, "GUI module (Tk) is not available")
class TestGuiExcerptRendering(unittest.TestCase):
    """The GUI marks excerpts with a trailing ellipsis."""

    def test_explicit_description_is_unchanged(self):
        text = gui.format_description(
            {"description": "Ошибка SQL", "description_is_excerpt": False}
        )
        self.assertEqual(text, "Ошибка SQL")

    def test_excerpt_gets_trailing_ellipsis(self):
        text = gui.format_description(
            {"description": "Первые 200", "description_is_excerpt": True}
        )
        self.assertEqual(text, "Первые 200" + gui.DESCRIPTION_EXCERPT_MARK)

    def test_empty_description_gets_no_ellipsis(self):
        text = gui.format_description(
            {"description": "", "description_is_excerpt": True}
        )
        self.assertEqual(text, "")

    def test_missing_flag_defaults_to_explicit(self):
        text = gui.format_description({"description": "текст"})
        self.assertEqual(text, "текст")


if __name__ == "__main__":
    unittest.main()
