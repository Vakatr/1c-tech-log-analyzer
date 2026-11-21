"""Centralized LogT ClickHouse schema description and DDL.

This module is the single source of truth for the table and column names used by
:mod:`logt_adapter`.  The authoritative table definitions live in
:data:`TABLE_SPECS` below, transcribed from
``vendor/logt/src/create_table_clickhouse.sql`` (the schema LogT itself writes).
Keeping them here — rather than reading the vendored file at runtime — means the
application works when the vendored LogT checkout is provisioned separately and
is not present in this repository.

The vendored SQL file cannot be executed as-is in any case: it contains
``DROP TABLE`` statements, so it must never be replayed against a live database.
When the file *is* available, it is only used as an input that gets parsed,
stripped of every destructive statement and re-rendered as ``CREATE TABLE IF NOT
EXISTS``.  That transformation is what makes schema bootstrap non-destructive
(see ``docs/CLICKHOUSE.md``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

#: Repository root, resolved relative to this file (no hardcoded absolute paths).
REPO_ROOT = Path(__file__).resolve().parent

#: Path to the upstream schema shipped with the vendored LogT checkout.
#:
#: The vendored checkout is git-ignored (it is a separate repository carrying
#: credentials in ``src/config.json``), so this path may legitimately not exist.
#: :func:`render_create_statements` therefore falls back to :data:`TABLE_SPECS`.
LOGT_SCHEMA_PATH = REPO_ROOT / "vendor" / "logt" / "src" / "create_table_clickhouse.sql"

#: Default ClickHouse database used by LogT.
DEFAULT_DATABASE = "tracelog"

#: Default dataset name written into the ``dataset`` column.
DEFAULT_DATASET = "default"


@dataclass(frozen=True)
class TableSpec:
    """Expected structure of one LogT table.

    Attributes:
        name: Table name inside the ``tracelog`` database.
        columns: Mapping of column name to the ClickHouse type as declared in
            the vendored SQL file, in declaration order.  Types are the bare
            ClickHouse type (no ``CODEC``); see ``codecs``.
        engine: Table engine.
        order_by: ``ORDER BY`` expression, normalized to a tuple of identifiers.
        partition_by: ``PARTITION BY`` expression, normalized to a tuple.
        codecs: Optional ``column -> CODEC(...)`` mapping applied in the DDL.
        indexes: Optional data-skipping index definitions, emitted verbatim.
        settings: Optional ``SETTINGS`` clauses emitted verbatim.
    """

    name: str
    columns: Dict[str, str]
    engine: str
    order_by: Tuple[str, ...]
    partition_by: Tuple[str, ...]
    codecs: Dict[str, str] = field(default_factory=dict)
    indexes: Tuple[str, ...] = ()
    settings: Tuple[str, ...] = ()

    @property
    def column_names(self) -> List[str]:
        """Return the declared column names in DDL order."""
        return list(self.columns.keys())

    def column_type(self, column: str) -> str:
        """Return the DDL type for ``column``, including any ``CODEC``.

        Args:
            column: Column name.

        Returns:
            The type expression as it should appear in ``CREATE TABLE``.
        """
        base = self.columns[column]
        codec = self.codecs.get(column)
        return f"{base} {codec}" if codec else base


#: Tables written by LogT during an import.
TABLE_SPECS: Dict[str, TableSpec] = {
    "operations": TableSpec(
        name="operations",
        columns={
            "dataset": "LowCardinality(String)",
            "db_name": "LowCardinality(String)",
            "session_id": "UInt64",
            "client_id": "UInt64",
            "connect_id": "UInt64",
            "user": "LowCardinality(String)",
            "vrs_session": "String",
            "ts_vrsrequest_us": "DateTime64(6, 'UTC')",
            "ts_vrsresponse_us": "DateTime64(6, 'UTC')",
            "ts_vrsrequest": "DateTime('UTC')",
            "ts_vrsresponse": "DateTime('UTC')",
            "duration_us": "UInt64",
            "context": "LowCardinality(String)",
        },
        engine="MergeTree",
        order_by=(
            "dataset",
            "db_name",
            "ts_vrsresponse",
            "session_id",
            "client_id",
            "ts_vrsresponse_us",
        ),
        partition_by=("dataset",),
        codecs={"context": "CODEC(ZSTD(6))"},
        indexes=(
            "INDEX idx_ts_vrsresponse ts_vrsresponse TYPE minmax GRANULARITY 1",
            "INDEX idx_ctx_ng context TYPE ngrambf_v1(3, 256, 3, 0) GRANULARITY 2",
        ),
        settings=("index_granularity = 8192",),
    ),
    "events": TableSpec(
        name="events",
        columns={
            "dataset": "LowCardinality(String)",
            "db_name": "LowCardinality(String)",
            "session_id": "UInt64",
            "client_id": "UInt64",
            "connect_id": "UInt64",
            "user": "LowCardinality(String)",
            "ts_event_us": "DateTime64(6, 'UTC')",
            "ts_event": "DateTime('UTC')",
            "ts_vrsrequest_us": "DateTime64(6, 'UTC')",
            "ts_vrsresponse_us": "DateTime64(6, 'UTC')",
            "event_name": "LowCardinality(String)",
            "duration_us": "UInt64",
            "space_us": "Int64",
            "event_string": "LowCardinality(String)",
            "context": "LowCardinality(String)",
        },
        engine="MergeTree",
        order_by=(
            "dataset",
            "db_name",
            "session_id",
            "client_id",
            "ts_vrsrequest_us",
            "ts_vrsresponse_us",
            "ts_event_us",
        ),
        partition_by=("dataset",),
        codecs={
            "event_string": "CODEC(ZSTD(6))",
            "context": "CODEC(ZSTD(6))",
        },
        settings=("index_granularity = 2048", "index_granularity_bytes = 0"),
    ),
    "event_stats": TableSpec(
        name="event_stats",
        columns={
            "dataset": "LowCardinality(String)",
            "db_name": "LowCardinality(String)",
            "session_id": "UInt64",
            "client_id": "UInt64",
            "connect_id": "UInt64",
            "ts_vrsrequest_us": "DateTime64(6, 'UTC')",
            "ts_vrsresponse_us": "DateTime64(6, 'UTC')",
            "event_name": "LowCardinality(String)",
            "total_duration_us": "UInt64",
            "percentage": "Float32",
            "count": "UInt32",
        },
        engine="MergeTree",
        order_by=(
            "dataset",
            "db_name",
            "session_id",
            "client_id",
            "ts_vrsrequest_us",
            "ts_vrsresponse_us",
            "event_name",
        ),
        partition_by=("dataset",),
        indexes=(
            "INDEX idx_event_name event_name TYPE bloom_filter(0.01) GRANULARITY 1",
        ),
        settings=("index_granularity = 2048", "index_granularity_bytes = 0"),
    ),
    "calls": TableSpec(
        name="calls",
        columns={
            "dataset": "LowCardinality(String)",
            "db_name": "LowCardinality(String)",
            "session_id": "UInt64",
            "client_id": "UInt64",
            "connect_id": "UInt64",
            "ts_vrsrequest_us": "DateTime64(6, 'UTC')",
            "ts_vrsresponse_us": "DateTime64(6, 'UTC')",
            "event_name": "LowCardinality(String)",
            "duration_us": "UInt64",
            "cpu_time": "UInt64",
            "memory": "Int64",
            "memory_peak": "UInt64",
        },
        engine="MergeTree",
        order_by=(
            "dataset",
            "db_name",
            "session_id",
            "client_id",
            "ts_vrsrequest_us",
            "ts_vrsresponse_us",
        ),
        partition_by=("dataset",),
        settings=("index_granularity = 2048", "index_granularity_bytes = 0"),
    ),
}

#: Convenience aliases so callers never hardcode table names.
TABLE_OPERATIONS = "operations"
TABLE_EVENTS = "events"
TABLE_EVENT_STATS = "event_stats"
TABLE_CALLS = "calls"

#: Column groups reused by the query builders.
TIMESTAMP_COLUMN = "ts_event"          # event timestamp (seconds resolution)
TIMESTAMP_US_COLUMN = "ts_event_us"    # event timestamp (microsecond resolution)
DATASET_COLUMN = "dataset"
EVENT_NAME_COLUMN = "event_name"
DURATION_US_COLUMN = "duration_us"
USER_COLUMN = "user"
DB_NAME_COLUMN = "db_name"
CONTEXT_COLUMN = "context"
EVENT_STRING_COLUMN = "event_string"


class SchemaDefinitionError(RuntimeError):
    """Raised when the vendored SQL schema cannot be read or parsed."""


def _strip_sql_comments(sql: str) -> str:
    """Remove ``--`` line comments and ``/* */`` block comments from ``sql``."""
    without_block = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", " ", without_block)


def _normalize_key_expression(expression: str) -> Tuple[str, ...]:
    """Normalize an ``ORDER BY``/``PARTITION BY`` expression to a tuple.

    Parentheses are stripped and each identifier is lowercased so that
    ``PARTITION BY (dataset)`` and ``PARTITION BY dataset`` compare equal.

    Args:
        expression: Raw expression text from the DDL or from ``system.tables``.

    Returns:
        Tuple of normalized comma-separated identifiers.
    """
    cleaned = expression.strip()
    while cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = cleaned[1:-1].strip()
    if not cleaned:
        return ()
    return tuple(part.strip().lower() for part in cleaned.split(",") if part.strip())


def _load_vendored_statements() -> List[str]:
    """Read and split the vendored schema SQL into individual statements.

    Returns:
        List of non-empty, comment-free SQL statements (``DROP`` statements are
        kept here so :func:`render_create_statements` can demonstrate removal).

    Raises:
        SchemaDefinitionError: If the vendored SQL file is missing or unreadable.
    """
    if not LOGT_SCHEMA_PATH.exists():
        raise SchemaDefinitionError(
            f"LogT schema file not found: {LOGT_SCHEMA_PATH}. "
            "The vendored LogT checkout is required."
        )
    try:
        raw = LOGT_SCHEMA_PATH.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - defensive
        raise SchemaDefinitionError(f"Cannot read {LOGT_SCHEMA_PATH}: {exc}") from exc

    statements = []
    for chunk in _strip_sql_comments(raw).split(";"):
        statement = " ".join(chunk.split()).strip()
        if statement:
            statements.append(statement)
    return statements


def _strip_destructive(statement: str) -> Optional[str]:
    """Return ``None`` when ``statement`` is destructive, else the statement.

    ``DROP TABLE`` and ``DROP DATABASE`` are always rejected: schema bootstrap
    must never destroy user data.  Filtering happens here, in the single place
    that turns DDL into runnable statements.
    """
    upper = statement.upper()
    if upper.startswith("DROP") or re.search(r"\bDROP\s+(TABLE|DATABASE)\b", upper):
        return None
    return statement


def _make_idempotent(statement: str) -> str:
    """Rewrite ``CREATE ...`` into ``CREATE ... IF NOT EXISTS``.

    ``CREATE DATABASE`` is intentionally left out of the bootstrap set: the
    application connects to an existing database and must not silently create
    one that LogT expects an administrator to provision.
    """
    match = re.match(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?", statement, re.IGNORECASE)
    if not match:
        return statement
    return "CREATE TABLE IF NOT EXISTS " + statement[match.end():]


def _render_from_specs(database: str) -> List[str]:
    """Render ``CREATE TABLE IF NOT EXISTS`` from :data:`TABLE_SPECS`.

    This is the fallback used when the vendored SQL file is not present, which is
    the normal case in this repository (``vendor/`` is git-ignored).  The output
    matches the vendored DDL for the columns, engine, partitioning and ordering
    that :class:`TableSpec` models.

    Args:
        database: Target ClickHouse database name.

    Returns:
        One statement per table, in :data:`TABLE_SPECS` order.
    """
    qualified = _quote_identifier(database)
    statements: List[str] = []
    for spec in TABLE_SPECS.values():
        definitions = [
            f"{name} {spec.column_type(name)}" for name in spec.columns
        ]
        definitions.extend(spec.indexes)
        columns = ",\n    ".join(definitions)
        order_by = ", ".join(spec.order_by)
        partition_by = ", ".join(spec.partition_by)
        statement = (
            f"CREATE TABLE IF NOT EXISTS {qualified}.{spec.name}\n(\n    {columns}\n)\n"
            f"ENGINE = {spec.engine}"
        )
        if partition_by:
            statement += f"\nPARTITION BY ({partition_by})"
        statement += f"\nORDER BY ({order_by})"
        if spec.settings:
            statement += "\nSETTINGS " + ", ".join(spec.settings)
        statements.append(statement + ";")
    return statements


def render_create_statements(database: str = DEFAULT_DATABASE) -> List[str]:
    """Build the idempotent ``CREATE TABLE IF NOT EXISTS`` statements.

    Every ``DROP`` statement is removed and the database qualifier is replaced,
    so the same DDL can target a configured database instead of the hardcoded
    ``tracelog``.

    When the vendored SQL file is unavailable — it is git-ignored, because the
    vendored checkout is a separate repository — the statements are rendered
    from :data:`TABLE_SPECS` instead.  Both paths produce idempotent,
    non-destructive DDL.

    Args:
        database: Target ClickHouse database name.

    Returns:
        List of ``CREATE TABLE IF NOT EXISTS`` statements.

    Raises:
        SchemaDefinitionError: If no ``CREATE TABLE`` statements could be built.
    """
    if not LOGT_SCHEMA_PATH.exists():
        return _render_from_specs(database)

    rendered: List[str] = []
    for statement in _load_vendored_statements():
        safe = _strip_destructive(statement)
        if safe is None:
            continue
        if not safe.upper().startswith("CREATE TABLE"):
            continue
        idempotent = _make_idempotent(safe)
        idempotent = re.sub(
            r"(?i)^CREATE TABLE IF NOT EXISTS\s+`?\w+`?\.",
            f"CREATE TABLE IF NOT EXISTS {_quote_identifier(database)}.",
            idempotent,
        )
        rendered.append(idempotent + ";")

    if not rendered:
        raise SchemaDefinitionError(
            f"No CREATE TABLE statements found in {LOGT_SCHEMA_PATH}."
        )
    return rendered


def compare_vendored_schema(database: str = DEFAULT_DATABASE) -> List[str]:
    """Compare :data:`TABLE_SPECS` with the vendored LogT DDL.

    The vendored checkout is git-ignored, so this audit can only run where it is
    provisioned.  It renders both sources and compares them as normalized text:
    columns, types, codecs, engine, partitioning, ordering, indexes and settings.
    Any difference means ``TABLE_SPECS`` must be updated (DEBT-006).

    Args:
        database: Database name used for both renderings.

    Returns:
        List of human-readable drift descriptions; empty when they match.

    Raises:
        SchemaDefinitionError: If the vendored SQL file is not present.
    """
    if not LOGT_SCHEMA_PATH.exists():
        raise SchemaDefinitionError(
            f"LogT schema file not found: {LOGT_SCHEMA_PATH}. "
            "Provision vendor/logt (or set LOGT_ROOT) before auditing."
        )

    expected = {
        _ddl_table_name(statement): _normalize_ddl(statement)
        for statement in _render_from_specs(database)
    }
    actual = {
        _ddl_table_name(statement): _normalize_ddl(statement)
        for statement in render_create_statements(database)
    }

    problems: List[str] = []
    for table in sorted(set(expected) | set(actual)):
        if table not in expected:
            problems.append(
                f"Table '{table}' exists in the vendored DDL but not in TABLE_SPECS."
            )
        elif table not in actual:
            problems.append(
                f"Table '{table}' exists in TABLE_SPECS but not in the vendored DDL."
            )
        elif expected[table] != actual[table]:
            problems.append(
                f"Table '{table}' differs between TABLE_SPECS and the vendored DDL."
            )
    return problems


def _ddl_table_name(statement: str) -> str:
    """Return the bare table name from a rendered ``CREATE TABLE`` statement."""
    match = re.search(
        r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(?:`[^`]+`\.)?`?([A-Za-z0-9_]+)`?",
        statement,
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else ""


def _normalize_ddl(statement: str) -> str:
    """Normalize DDL text so formatting differences do not count as drift."""
    return re.sub(r"\s+", " ", statement).strip().rstrip(";").lower()


def _quote_identifier(name: str) -> str:
    """Validate and backtick-quote a ClickHouse identifier.

    Args:
        name: Database or table identifier supplied by configuration.

    Returns:
        The identifier wrapped in backticks.

    Raises:
        ValueError: If ``name`` contains characters that are not valid in an
            unquoted ClickHouse identifier, which also blocks SQL injection
            through the database name.
    """
    if not name or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(f"Invalid ClickHouse identifier: {name!r}")
    return f"`{name}`"


def qualified_table(database: str, table: str) -> str:
    """Return a safely quoted ``database.table`` reference.

    Args:
        database: Database name from configuration.
        table: Table name; must exist in :data:`TABLE_SPECS`.

    Returns:
        Backtick-quoted ``database.table`` string.

    Raises:
        ValueError: If either identifier is invalid or the table is unknown.
    """
    if table not in TABLE_SPECS:
        raise ValueError(f"Unknown LogT table: {table!r}")
    return f"{_quote_identifier(database)}.{_quote_identifier(table)}"


def expected_columns(table: str) -> Dict[str, str]:
    """Return the expected ``column -> type`` mapping for ``table``.

    Args:
        table: One of the keys in :data:`TABLE_SPECS`.

    Returns:
        Mapping of column name to ClickHouse type.

    Raises:
        ValueError: If the table is unknown.
    """
    try:
        return dict(TABLE_SPECS[table].columns)
    except KeyError as exc:
        raise ValueError(f"Unknown LogT table: {table!r}") from exc


def normalize_type(type_name: str) -> str:
    """Normalize a ClickHouse type for comparison.

    Whitespace and quote style differ between the vendored DDL and
    ``system.columns`` (e.g. ``DateTime64(6, 'UTC')`` vs ``DateTime64(6,'UTC')``),
    so both sides are compared in a normalized form.

    Args:
        type_name: Raw ClickHouse type string.

    Returns:
        Lowercased, whitespace-free type string.
    """
    return re.sub(r"\s+", "", str(type_name)).replace('"', "'").lower()


def schema_mismatches(
    actual_tables: Dict[str, Dict[str, str]],
    tables: Iterable[str] = tuple(TABLE_SPECS),
) -> List[str]:
    """Compare observed columns with the expected LogT schema.

    Args:
        actual_tables: Mapping of ``table -> {column: type}`` read from
            ``system.columns``.
        tables: Tables to validate. Defaults to every table in
            :data:`TABLE_SPECS`.

    Returns:
        List of blocking mismatch descriptions; empty when the schema matches.
        Descriptions never contain SQL and never suggest dropping data — they
        explain what to migrate manually.
    """
    problems: List[str] = []
    for table in tables:
        spec = TABLE_SPECS.get(table)
        if spec is None:
            problems.append(f"Unknown table requested for validation: {table}")
            continue

        actual = actual_tables.get(table)
        if actual is None:
            problems.append(f"Table '{table}' is missing.")
            continue

        expected = spec.columns
        missing = [name for name in expected if name not in actual]
        if missing:
            problems.append(
                f"Table '{table}' is missing column(s): {', '.join(missing)}."
            )

        for name, expected_type in expected.items():
            if name not in actual:
                continue
            if normalize_type(actual[name]) != normalize_type(expected_type):
                problems.append(
                    f"Table '{table}' column '{name}' has type "
                    f"'{actual[name]}', expected '{expected_type}'."
                )
    return problems


def schema_warnings(
    actual_tables: Dict[str, Dict[str, str]],
    tables: Iterable[str] = tuple(TABLE_SPECS),
) -> List[str]:
    """Return non-blocking differences between observed and expected schema.

    Extra columns are reported but do not break the integration: LogT only
    inserts the columns it knows about, and every column this application reads
    is still present.  They are surfaced as warnings instead of errors so a
    deployment with a newer LogT build keeps working.

    Args:
        actual_tables: Mapping of ``table -> {column: type}``.
        tables: Tables to inspect. Defaults to every table in
            :data:`TABLE_SPECS`.

    Returns:
        List of warning strings; empty when there is nothing to report.
    """
    warnings: List[str] = []
    for table in tables:
        spec = TABLE_SPECS.get(table)
        actual = actual_tables.get(table)
        if spec is None or actual is None:
            continue
        extra = [name for name in actual if name not in spec.columns]
        if extra:
            warnings.append(
                f"Table '{table}' has extra column(s) not present in the "
                f"vendored LogT schema: {', '.join(sorted(extra))}. "
                "This does not block reading data."
            )
    return warnings


def migration_hint(database: str) -> str:
    """Return the manual migration guidance shown alongside a mismatch.

    Args:
        database: Database name used in the guidance text.

    Returns:
        Multi-line guidance string without executable destructive SQL.
    """
    return (
        "The ClickHouse schema does not match the LogT schema this build expects. "
        "Nothing was altered or dropped.\n\n"
        f"Database: {database}\n\n"
        "How to migrate manually:\n"
        "  1. Export the affected table(s) if the data matters, e.g.\n"
        f"     SELECT * FROM {database}.events FORMAT Native > events.native\n"
        "  2. Compare the columns of the table with "
        "vendor/logt/src/create_table_clickhouse.sql.\n"
        "  3. Create a corrected table under a new name, copy the rows across "
        "with INSERT INTO ... SELECT, then rename both tables.\n"
        "  4. Or, if the data is disposable, use 'Reset ClickHouse schema' from "
        "the application menu (explicit confirmation required).\n\n"
        "The application will not drop or alter your tables automatically."
    )


__all__ = [
    "DEFAULT_DATABASE",
    "DEFAULT_DATASET",
    "EVENT_NAME_COLUMN",
    "EVENT_STRING_COLUMN",
    "CONTEXT_COLUMN",
    "DATASET_COLUMN",
    "DURATION_US_COLUMN",
    "LOGT_SCHEMA_PATH",
    "REPO_ROOT",
    "SchemaDefinitionError",
    "TABLE_CALLS",
    "TABLE_EVENT_STATS",
    "TABLE_EVENTS",
    "TABLE_OPERATIONS",
    "TABLE_SPECS",
    "TIMESTAMP_COLUMN",
    "TIMESTAMP_US_COLUMN",
    "USER_COLUMN",
    "TableSpec",
    "expected_columns",
    "migration_hint",
    "normalize_type",
    "compare_vendored_schema",
    "qualified_table",
    "render_create_statements",
    "schema_mismatches",
    "schema_warnings",
]
