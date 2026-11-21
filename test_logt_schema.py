"""Tests for :mod:`logt_schema` and the schema bootstrap in :mod:`logt_adapter`.

The review's BLOCKER 1 requires that schema bootstrap be idempotent and never
destructive.  These tests prove it without a ClickHouse server by mocking the
driver, and additionally assert that no ``DROP`` statement is ever rendered.
"""

import re
import unittest
from pathlib import Path
from unittest import mock

import logt_adapter
import logt_schema

#: The vendored LogT SQL file is optional in the public repository.
LOGT_SCHEMA_VENDORED = logt_schema.LOGT_SCHEMA_PATH.is_file()


class FakeConnection:
    """Minimal stand-in for :class:`logt_adapter.ClickHouseConnection`."""

    def __init__(self, databases=None, existing=None, columns=None):
        """Record the fake server state.

        Args:
            databases: Databases reported by ``SHOW DATABASES``.
            existing: Table names reported as present in the target database.
            columns: Mapping ``(table, column) -> type`` for ``system.columns``.
        """
        self.config = {"database": logt_schema.DEFAULT_DATABASE}
        self.databases = databases if databases is not None else [logt_schema.DEFAULT_DATABASE]
        self.existing = set(existing or [])
        self.columns = dict(columns or {})
        self.statements = []

    def execute(self, sql, params=None):
        """Emulate the handful of statements ``ensure_schema`` issues."""
        self.statements.append(sql)
        normalized = " ".join(sql.split())
        if normalized.startswith("SHOW DATABASES"):
            return [(name,) for name in self.databases]
        if normalized.startswith("CREATE TABLE"):
            match = re.search(r"CREATE TABLE IF NOT EXISTS `[^`]+`\.(\w+)", normalized)
            if match:
                table = match.group(1)
                self.existing.add(table)
                for name, type_name in logt_schema.TABLE_SPECS[table].columns.items():
                    self.columns[(table, name)] = type_name
            return []
        if normalized.startswith("DROP TABLE"):
            match = re.search(r"DROP TABLE (?:IF EXISTS )?`[^`]+`\.`?(\w+)`?", normalized)
            if match:
                table = match.group(1)
                self.existing.discard(table)
                for key in [key for key in self.columns if key[0] == table]:
                    self.columns.pop(key, None)
            return []
        if normalized.startswith("ALTER"):
            self.statements.append("-- ALTER SHOULD NOT HAPPEN")
            return []
        return []

    def read_columns(self):
        """Return ``system.columns`` as ``table -> {column: type}``."""
        result = {}
        for (table, column), type_name in self.columns.items():
            result.setdefault(table, {})[column] = type_name
        return result

    def table_engines(self):
        """Return table engines (unused by these tests)."""
        return {name: "MergeTree" for name in self.existing}

    def ping(self):
        """Report the fake as reachable."""
        return True

    def close(self):
        """No-op close."""


def _full_columns():
    """Return a flat ``(table, column) -> type`` mapping of the full schema."""
    columns = {}
    for table, spec in logt_schema.TABLE_SPECS.items():
        for name, type_name in spec.columns.items():
            columns[(table, name)] = type_name
    return columns


def _nested(flat):
    """Convert a flat ``(table, column) -> type`` map to the nested form."""
    result = {}
    for (table, column), type_name in flat.items():
        result.setdefault(table, {})[column] = type_name
    return result


class TestRenderedSchema(unittest.TestCase):
    """The vendored SQL is rendered safely and completely."""

    def test_renders_all_four_tables(self):
        statements = logt_schema.render_create_statements("tracelog")
        self.assertEqual(len(statements), 4)
        names = {
            re.search(r"`tracelog`\.(\w+)", statement).group(1)
            for statement in statements
        }
        self.assertEqual(names, {"operations", "events", "event_stats", "calls"})

    def test_never_renders_drop(self):
        statements = logt_schema.render_create_statements("tracelog")
        for statement in statements:
            self.assertNotIn("DROP", statement.upper())

    def test_uses_if_not_exists(self):
        for statement in logt_schema.render_create_statements("tracelog"):
            self.assertIn("CREATE TABLE IF NOT EXISTS", statement)

    @unittest.skipUnless(LOGT_SCHEMA_VENDORED, "vendor/logt SQL is not bundled")
    def test_vendored_sql_contains_drop_but_renderer_strips_it(self):
        """The vendored file does have DROP statements; they must not survive."""
        raw = logt_schema.LOGT_SCHEMA_PATH.read_text(encoding="utf-8")
        # Prove the file we start from is the risky one.
        self.assertIn("DROP", raw.upper())
        rendered = "\n".join(logt_schema.render_create_statements("tracelog"))
        self.assertNotIn("DROP", rendered.upper())

    def test_fallback_without_vendored_file_is_still_safe(self):
        """``vendor/`` is git-ignored, so bootstrap must work without it."""
        with mock.patch.object(
            logt_schema, "LOGT_SCHEMA_PATH", Path(logt_schema.REPO_ROOT) / "absent.sql"
        ):
            statements = logt_schema.render_create_statements("tracelog")
        self.assertEqual(len(statements), 4)
        for statement in statements:
            self.assertIn("CREATE TABLE IF NOT EXISTS", statement)
            self.assertNotIn("DROP", statement.upper())

    def test_fallback_matches_vendored_columns_and_types(self):
        """Both rendering paths must describe the same tables."""
        with mock.patch.object(
            logt_schema, "LOGT_SCHEMA_PATH", Path(logt_schema.REPO_ROOT) / "absent.sql"
        ):
            fallback = logt_schema.render_create_statements("tracelog")
        vendored = logt_schema.render_create_statements("tracelog")
        self.assertEqual(len(fallback), len(vendored))
        for left, right in zip(fallback, vendored):
            self.assertEqual(
                _column_definitions(left),
                _column_definitions(right),
                msg=f"DDL differs for {left.splitlines()[0]}",
            )

    def test_identifier_quoting_rejects_injection(self):
        for bad in ("bad; DROP TABLE x", "a`b", "1abc", "", "with space", "quote'"):
            with self.assertRaises(ValueError):
                logt_schema.qualified_table(bad, "events")

    def test_database_name_is_quoted(self):
        self.assertEqual(
            logt_schema.qualified_table("tracelog", "events"), "`tracelog`.`events`"
        )


class TestNormalizeKeyExpression(unittest.TestCase):
    """``ORDER BY``/``PARTITION BY`` text is normalized before comparison."""

    def test_parenthesised_list_is_unwrapped_and_lowercased(self):
        self.assertEqual(
            logt_schema._normalize_key_expression("(dataset, DB_NAME)"),
            ("dataset", "db_name"),
        )

    def test_bare_identifier_is_a_single_element_tuple(self):
        self.assertEqual(
            logt_schema._normalize_key_expression("  dataset  "), ("dataset",)
        )

    def test_repeated_parentheses_are_unwrapped(self):
        self.assertEqual(
            logt_schema._normalize_key_expression("((dataset))"), ("dataset",)
        )

    def test_empty_expressions_return_an_empty_tuple(self):
        for expression in ("", "   ", "()", "( )"):
            with self.subTest(expression=expression):
                self.assertEqual(logt_schema._normalize_key_expression(expression), ())

    def test_dotted_and_quoted_identifiers_are_preserved(self):
        self.assertEqual(
            logt_schema._normalize_key_expression("`dataset`, events.ts_event"),
            ("`dataset`", "events.ts_event"),
        )

    def test_type_constructor_is_treated_as_text(self):
        """Only bare parenthesised lists are unwrapped; constructors are text.

        This pins the current limitation of the helper rather than a schema
        contract: ``tuple(...)``/``array(...)`` are not parsed, but they must
        not raise either.
        """
        self.assertEqual(
            logt_schema._normalize_key_expression("array(String)"),
            ("array(string)",),
        )
        nested = logt_schema._normalize_key_expression("tuple(dataset, db_name)")
        self.assertIsInstance(nested, tuple)
        self.assertEqual(" ".join(nested), "tuple(dataset db_name)")


class TestExpectedColumns(unittest.TestCase):
    """``expected_columns`` mirrors :data:`TABLE_SPECS` for every table."""

    def test_every_table_returns_its_declared_columns(self):
        for table, spec in logt_schema.TABLE_SPECS.items():
            with self.subTest(table=table):
                self.assertEqual(logt_schema.expected_columns(table), spec.columns)

    def test_returned_mapping_is_a_copy(self):
        columns = logt_schema.expected_columns("events")
        columns["injected"] = "String"
        self.assertNotIn("injected", logt_schema.TABLE_SPECS["events"].columns)
        self.assertNotIn("injected", logt_schema.expected_columns("events"))

    def test_unknown_table_raises_value_error(self):
        with self.assertRaises(ValueError) as caught:
            logt_schema.expected_columns("no_such_table")
        self.assertIn("no_such_table", str(caught.exception))


class TestIdentifierQuotingEdges(unittest.TestCase):
    """Identifier validation rejects separators, quoting and whitespace."""

    def test_valid_identifiers_are_backtick_quoted(self):
        for name in ("_x", "A1", "snake_case", "a" * 64):
            with self.subTest(name=name):
                self.assertEqual(
                    logt_schema.qualified_table(name, "events"),
                    f"`{name}`.`events`",
                )

    def test_dots_backticks_and_whitespace_are_rejected(self):
        for name in (
            "a.b",
            "`tracelog`",
            " tracelog",
            "tracelog ",
            "tr-acelog",
            "db$",
            "1db",
            "semi;colon",
            "line\nbreak",
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    logt_schema.qualified_table(name, "events")

    def test_unknown_table_is_rejected(self):
        for table in ("no_such_table", "events_v2", ""):
            with self.subTest(table=table):
                with self.assertRaises(ValueError):
                    logt_schema.qualified_table("tracelog", table)

    def test_all_declared_tables_are_quotable(self):
        for table in logt_schema.TABLE_SPECS:
            with self.subTest(table=table):
                self.assertEqual(
                    logt_schema.qualified_table("tracelog", table),
                    f"`tracelog`.`{table}`",
                )


class TestVendoredDdlAbsent(unittest.TestCase):
    """The shipped application must work without the git-ignored vendor tree."""

    def _absent(self):
        return mock.patch.object(
            logt_schema,
            "LOGT_SCHEMA_PATH",
            Path(logt_schema.REPO_ROOT) / "vendor" / "absent" / "schema.sql",
        )

    def test_fallback_renders_every_expected_column(self):
        with self._absent():
            statements = logt_schema.render_create_statements("edge_db")
        by_table = {
            logt_schema._ddl_table_name(statement): _column_definitions(statement)
            for statement in statements
        }
        self.assertEqual(set(by_table), set(logt_schema.TABLE_SPECS))
        for table, spec in logt_schema.TABLE_SPECS.items():
            with self.subTest(table=table):
                self.assertEqual(set(by_table[table]), set(spec.columns))
                for column, type_name in spec.columns.items():
                    self.assertEqual(
                        logt_schema.normalize_type(by_table[table][column]),
                        logt_schema.normalize_type(spec.column_type(column)),
                    )

    def test_fallback_output_is_qualified_and_non_destructive(self):
        with self._absent():
            statements = logt_schema.render_create_statements("edge_db")
        for statement in statements:
            self.assertIn("edge_db", statement)
            self.assertIn("CREATE TABLE IF NOT EXISTS", statement)
            self.assertNotIn("DROP", statement.upper())

    def test_schema_audit_without_vendor_raises(self):
        with self._absent():
            with self.assertRaises(logt_schema.SchemaDefinitionError):
                logt_schema.compare_vendored_schema()
            with self.assertRaises(logt_schema.SchemaDefinitionError):
                logt_schema._load_vendored_statements()


@unittest.skipUnless(
    logt_schema.LOGT_SCHEMA_PATH.exists(),
    "vendor/logt is not provisioned",
)
class TestVendoredSchemaContract(unittest.TestCase):
    """DEBT-006: schema drift is detected instead of updated by eye."""

    def test_table_specs_match_the_vendored_ddl(self):
        self.assertEqual(
            logt_schema.compare_vendored_schema(),
            [],
            msg=(
                "vendor/logt/src/create_table_clickhouse.sql changed: update "
                "TABLE_SPECS in logt_schema.py to match it."
            ),
        )

    def test_drift_is_reported(self):
        original = logt_schema._render_from_specs

        def altered(database):
            statements = original(database)
            return [statements[0].replace("db_name", "db_name_changed")] + statements[1:]

        with mock.patch.object(logt_schema, "_render_from_specs", altered):
            problems = logt_schema.compare_vendored_schema()
        self.assertTrue(any("operations" in problem for problem in problems))

    def test_missing_vendored_file_is_reported(self):
        with mock.patch.object(
            logt_schema, "LOGT_SCHEMA_PATH", Path(logt_schema.REPO_ROOT) / "absent.sql"
        ):
            with self.assertRaises(logt_schema.SchemaDefinitionError):
                logt_schema.compare_vendored_schema()


def _column_definitions(statement: str) -> dict:
    """Return ``column -> type`` parsed from a ``CREATE TABLE`` statement.

    Splits the first parenthesised block on commas that are not nested inside
    type parentheses, so ``DateTime64(6, 'UTC')`` stays intact.  Data-skipping
    ``INDEX`` entries are skipped.

    Args:
        statement: A ``CREATE TABLE ...`` statement.

    Returns:
        Mapping of column name to its type expression.
    """
    start = statement.index("(")
    depth = 0
    end = start
    for index in range(start, len(statement)):
        if statement[index] == "(":
            depth += 1
        elif statement[index] == ")":
            depth -= 1
            if depth == 0:
                end = index
                break
    inner = statement[start + 1:end]

    chunks, depth, current = [], 0, ""
    for char in inner:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            chunks.append(current)
            current = ""
        else:
            current += char
    chunks.append(current)

    result = {}
    for chunk in chunks:
        line = " ".join(chunk.split())
        if not line or line.upper().startswith("INDEX"):
            continue
        name, _, type_name = line.partition(" ")
        result[name] = type_name
    return result


class TestSchemaMismatches(unittest.TestCase):
    """Mismatch detection is precise about what blocks and what does not."""

    def _full_columns(self):
        columns = {}
        for table, spec in logt_schema.TABLE_SPECS.items():
            for name, type_name in spec.columns.items():
                columns[(table, name)] = type_name
        return columns

    def test_matching_schema_has_no_problems(self):
        columns = _nested(_full_columns())
        self.assertEqual(logt_schema.schema_mismatches(columns), [])
        self.assertEqual(logt_schema.schema_warnings(columns), [])

    def test_missing_table_is_a_problem(self):
        columns = _nested(_full_columns())
        columns.pop("calls", None)
        problems = logt_schema.schema_mismatches(columns)
        self.assertTrue(any("calls" in problem for problem in problems))
        self.assertEqual(logt_schema.schema_warnings(columns), [])

    def test_missing_column_is_a_problem(self):
        columns = _nested(_full_columns())
        columns["events"].pop(logt_schema.EVENT_STRING_COLUMN, None)
        problems = logt_schema.schema_mismatches(columns)
        self.assertTrue(
            any(logt_schema.EVENT_STRING_COLUMN in problem for problem in problems)
        )

    def test_type_mismatch_is_a_problem(self):
        columns = _nested(_full_columns())
        columns["events"][logt_schema.EVENT_STRING_COLUMN] = "Int32"
        problems = logt_schema.schema_mismatches(columns)
        self.assertTrue(any("type" in problem.lower() for problem in problems))

    def test_extra_column_is_only_a_warning(self):
        """Extra columns (newer LogT builds add them) must not block queries."""
        columns = _nested(_full_columns())
        columns["operations"]["some_future_column"] = "String"
        self.assertEqual(logt_schema.schema_mismatches(columns), [])
        warnings = logt_schema.schema_warnings(columns)
        self.assertTrue(any("some_future_column" in warning for warning in warnings))

    def test_missing_column_blocks_while_extra_column_only_warns(self):
        """A combined drift keeps the two severities separate."""
        columns = _nested(_full_columns())
        columns["events"].pop(logt_schema.EVENT_STRING_COLUMN, None)
        columns["operations"]["some_future_column"] = "String"
        problems = logt_schema.schema_mismatches(columns)
        warnings = logt_schema.schema_warnings(columns)
        self.assertTrue(any("missing column" in problem for problem in problems))
        self.assertFalse(
            any("some_future_column" in problem for problem in problems)
        )
        self.assertTrue(any("some_future_column" in warning for warning in warnings))

    def test_extra_type_spelling_differences_are_not_problems(self):
        """Whitespace and quote style must not count as a type change."""
        columns = _nested(_full_columns())
        columns["events"][logt_schema.EVENT_STRING_COLUMN] = (
            "  LowCardinality( String ) "
        )
        columns["events"]["ts_event_us"] = 'DateTime64(6,"UTC")'
        self.assertEqual(logt_schema.schema_mismatches(columns), [])
        self.assertEqual(logt_schema.schema_warnings(columns), [])

    def test_unknown_table_requested_is_a_problem(self):
        """Asking to validate a table outside the schema is reported, not ignored."""
        problems = logt_schema.schema_mismatches({}, tables=("events", "no_such_table"))
        self.assertTrue(
            any("Unknown table" in problem for problem in problems),
            msg=problems,
        )
        self.assertEqual(
            logt_schema.schema_warnings({}, tables=("no_such_table",)), []
        )

    def test_vrs_session_is_expected_not_extra(self):
        """``operations.vrs_session`` is declared in the vendored SQL, so it is
        part of the expected schema rather than an unexpected column."""
        self.assertIn("vrs_session", logt_schema.TABLE_SPECS["operations"].columns)
        columns = _nested(_full_columns())
        self.assertEqual(logt_schema.schema_warnings(columns), [])


class TestEnsureSchemaIsIdempotent(unittest.TestCase):
    """BLOCKER 1: opening the app twice must not destroy data."""

    def test_ensure_schema_issues_no_drop(self):
        connection = FakeConnection()
        logt_adapter.ensure_schema({"database": "tracelog"}, connection=connection)
        joined = "\n".join(connection.statements).upper()
        self.assertNotIn("DROP", joined)

    def test_ensure_schema_is_idempotent(self):
        connection = FakeConnection()
        first = logt_adapter.ensure_schema({"database": "tracelog"}, connection=connection)
        second = logt_adapter.ensure_schema({"database": "tracelog"}, connection=connection)
        self.assertTrue(first.ready)
        self.assertTrue(second.ready)
        self.assertEqual(sorted(first.created), ["calls", "event_stats", "events", "operations"])
        # Second run creates nothing: no data is lost, no table recreated.
        self.assertEqual(second.created, [])
        creates = [s for s in connection.statements if s.upper().startswith("CREATE")]
        self.assertEqual(len(creates), 4)

    def test_ensure_schema_twice_reports_same_state(self):
        connection = FakeConnection()
        for _ in range(3):
            status = logt_adapter.ensure_schema({"database": "tracelog"}, connection=connection)
            self.assertTrue(status.ready)
        creates = [s for s in connection.statements if s.upper().startswith("CREATE")]
        self.assertEqual(len(creates), 4)

    def test_missing_database_is_reported_not_created(self):
        connection = FakeConnection(databases=["default"])
        with self.assertRaises(logt_adapter.ClickHouseUnavailable) as caught:
            logt_adapter.ensure_schema({"database": "tracelog"}, connection=connection)
        self.assertIn("tracelog", str(caught.exception))
        self.assertNotIn("CREATE DATABASE", "\n".join(connection.statements).upper())

    def test_type_mismatch_blocks_and_alters_nothing(self):
        columns = _full_columns()
        columns[("events", logt_schema.EVENT_STRING_COLUMN)] = "Int32"
        connection = FakeConnection(existing=list(logt_schema.TABLE_SPECS), columns=columns)
        status = logt_adapter.ensure_schema({"database": "tracelog"}, connection=connection)
        self.assertFalse(status.ready)
        self.assertTrue(status.problems)
        joined = "\n".join(connection.statements).upper()
        self.assertNotIn("ALTER", joined)
        self.assertNotIn("DROP", joined)

    def test_validate_schema_never_writes(self):
        connection = FakeConnection()
        logt_adapter.validate_schema({"database": "tracelog"}, connection=connection)
        for statement in connection.statements:
            self.assertFalse(statement.upper().startswith("CREATE"))


class TestResetSchemaIsGuarded(unittest.TestCase):
    """The only destructive path must require an explicit confirmation."""

    def test_reset_refuses_without_confirm(self):
        connection = FakeConnection(
            existing=list(logt_schema.TABLE_SPECS), columns=_full_columns()
        )
        with self.assertRaises(logt_adapter.AdapterError):
            logt_adapter.reset_schema(
                {"database": "tracelog"}, connection=connection, confirm=False
            )
        self.assertNotIn("DROP", "\n".join(connection.statements).upper())

    def test_reset_drops_only_when_confirmed(self):
        connection = FakeConnection(
            existing=list(logt_schema.TABLE_SPECS), columns=_full_columns()
        )
        status = logt_adapter.reset_schema(
            {"database": "tracelog"}, connection=connection, confirm=True
        )
        self.assertTrue(status.ready)
        joined = "\n".join(connection.statements).upper()
        self.assertIn("DROP TABLE", joined)


class TestNoDropOnAnyAutomaticPath(unittest.TestCase):
    """Static proof that executable ``DROP`` lives only behind ``reset_schema``."""

    #: Matches a *real* SQL DROP statement, not prose such as
    #: "nothing was dropped".
    SQL_DROP = re.compile(r"\bDROP\s+(TABLE|DATABASE|VIEW|DICTIONARY)\b", re.IGNORECASE)

    def test_executable_drop_only_in_reset_schema(self):
        """Parse both modules and locate every SQL ``DROP`` string literal.

        Only string literals can reach the driver, so those are what matter.
        Prose in docstrings and user-facing messages is ignored.  A literal is
        allowed inside ``reset_schema`` (the explicitly confirmed path) or
        inside ``logt_schema``'s defensive stripper, which *removes* DROP
        statements from the vendored DDL.
        """
        import ast

        allowed_functions = {
            (logt_adapter.__file__, "reset_schema"),
            (logt_schema.__file__, "_strip_destructive"),
        }
        offenders = []
        for path in (logt_adapter.__file__, logt_schema.__file__):
            tree = ast.parse(open(path, encoding="utf-8").read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                for inner in ast.walk(node):
                    if not isinstance(inner, ast.Constant) or not isinstance(inner.value, str):
                        continue
                    if not self.SQL_DROP.search(inner.value):
                        continue
                    if (path, node.name) in allowed_functions:
                        continue
                    offenders.append(
                        (path.rsplit("\\", 1)[-1], node.name, inner.value.strip()[:70])
                    )
        self.assertEqual(
            offenders,
            [],
            msg=f"executable DROP outside the guarded reset path: {offenders}",
        )

    def test_reset_schema_is_the_only_function_that_emits_drop(self):
        """``ensure_schema`` and friends must never build a DROP statement."""
        for name in ("ensure_schema", "validate_schema", "count_tables"):
            source = getattr(logt_adapter, name).__doc__ or ""
            self.assertNotIn("conn.execute(f\"DROP", source)
        source = open(logt_adapter.__file__, encoding="utf-8").read()
        # The literal template may appear exactly once, inside reset_schema.
        self.assertEqual(source.count('DROP TABLE IF EXISTS'), 1)


if __name__ == "__main__":
    unittest.main()

