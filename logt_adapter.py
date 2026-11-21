"""ClickHouse access layer for the 1C Log Analyzer (RDV-LogT integration).

Design notes
------------
* **One transport.** Every ClickHouse operation — schema inspection, DDL,
  imports counting and all GUI queries — goes through ``clickhouse-driver``
  (native protocol, port 9000).  No ``requests`` HTTP calls remain.
* **Credentials.** Connection settings are resolved from (in order) explicit
  arguments, the ``CLICKHOUSE_*`` environment variables, and a git-ignored
  ``config/clickhouse.local.json``.  The password is never rendered into SQL,
  never passed on a command line, and never logged — see :func:`redact`.
* **Non-destructive schema.** :func:`ensure_schema` only ever emits
  ``CREATE DATABASE IF NOT EXISTS`` and ``CREATE TABLE IF NOT EXISTS``.  A
  mismatch is reported, never repaired automatically.  The only destructive
  path is :func:`reset_schema`, which requires an explicit user action.
* **Parameterized SQL.** All user input is bound through server-side query
  parameters (``{name:Type}`` placeholders).  Identifiers that must be
  interpolated (database, table, column names) are validated and quoted by
  :mod:`logt_schema`, never concatenated from user input.

This module has no Tkinter imports so it can be exercised headless.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import logt_schema as schema
from ics_log_constants import ERROR_EVENT_NAMES
from import_progress import ImportProgress, ImportStage, ProgressReporter
from logt_progress import LogTProgressParser, is_tqdm_line
from logt_schema import (
    CONTEXT_COLUMN,
    DATASET_COLUMN,
    DB_NAME_COLUMN,
    DEFAULT_DATABASE,
    DEFAULT_DATASET,
    DURATION_US_COLUMN,
    EVENT_NAME_COLUMN,
    EVENT_STRING_COLUMN,
    REPO_ROOT,
    TABLE_CALLS,
    TABLE_EVENT_STATS,
    TABLE_EVENTS,
    TABLE_OPERATIONS,
    TIMESTAMP_COLUMN,
    TIMESTAMP_US_COLUMN,
    USER_COLUMN,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AdapterError",
    "ClickHouseConnection",
    "ClickHouseUnavailable",
    "Filters",
    "ImportCancelled",
    "ImportProgress",
    "ImportResult",
    "ImportStage",
    "LogTImportError",
    "SchemaError",
    "SchemaStatus",
    "build_description_expressions",
    "build_logt_config",
    "check_clickhouse_connection",
    "check_logt_runtime",
    "count_errors",
    "count_tables",
    "ensure_schema",
    "load_local_config",
    "query_errors",
    "query_overview",
    "query_performance",
    "query_performance_by_event",
    "query_user_events",
    "query_users",
    "redact",
    "reset_schema",
    "resolve_config",
    "resolve_logt_python",
    "run_logt_import",
    "validate_schema",
]

#: Directory holding generated LogT config files (git-ignored, restrictive).
LOGT_CONFIG_DIR = REPO_ROOT / ".logt_local"

#: Environment variable that overrides where temporary import files are staged.
#: Useful on hardened Windows hosts whose ACLs block the inherited temp
#: directory (and in tests, where no filesystem writes are desired).
ENV_WORK_DIR = "LOGT_WORK_DIR"

#: Environment variable that names the Python interpreter used to run LogT.
#:
#: Only consulted in a frozen (PyInstaller) build, where ``sys.executable`` is
#: the GUI binary rather than an interpreter.  The value may be a path to an
#: interpreter or a command name resolvable through ``PATH``.
ENV_LOGT_PYTHON = "LOGT_PYTHON"

#: Location of the optional local connection config (git-ignored).
LOCAL_CONFIG_PATH = REPO_ROOT / "config" / "clickhouse.local.json"

#: Environment variables consulted for connection settings.
ENV_HOST = "CLICKHOUSE_HOST"
ENV_PORT = "CLICKHOUSE_PORT"
ENV_DATABASE = "CLICKHOUSE_DATABASE"
ENV_USER = "CLICKHOUSE_USER"
ENV_PASSWORD = "CLICKHOUSE_PASSWORD"

#: Defaults matching a stock ClickHouse container (see docs/CLICKHOUSE.md).
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9000

#: Number of stderr lines kept for error reporting.
STDERR_TAIL_LINES = 20


class AdapterError(RuntimeError):
    """Base class for adapter failures with a user-presentable message."""


class ClickHouseUnavailable(AdapterError):
    """Raised when ClickHouse cannot be reached or rejects the connection."""


class SchemaError(AdapterError):
    """Raised when the LogT schema is missing or does not match expectations."""

    def __init__(self, message: str, problems: Sequence[str] = (), hints: Sequence[str] = ()):
        super().__init__(message)
        self.problems = list(problems)
        self.hints = list(hints)


class LogTImportError(AdapterError):
    """Raised when the LogT subprocess fails."""

    def __init__(self, message: str, exit_code: int = -1, stderr_tail: Sequence[str] = ()):
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr_tail = list(stderr_tail)


class ImportCancelled(AdapterError):
    """Raised when the user cancels an import."""


# ---------------------------------------------------------------------------
# Credential handling
# ---------------------------------------------------------------------------


def _is_connection_error(exc: BaseException) -> bool:
    """Decide whether ``exc`` means "ClickHouse is unreachable".

    Inspects the exception type chain (including ``__cause__``/``__context__``)
    instead of matching message text, which is localized by the operating
    system.  ``clickhouse_driver`` raises ``NetworkError``/``SocketTimeoutError``
    for transport failures and ``ServerException`` for server-side rejections.

    Args:
        exc: The exception raised by the driver.

    Returns:
        ``True`` for transport-level failures.
    """
    seen = set()
    current: Optional[BaseException] = exc
    # Only network-flavoured names count.  A bare ``OSError`` and local
    # filesystem subclasses (``FileNotFoundError``, ``PermissionError``) say
    # nothing about ClickHouse connectivity; treating them as transport errors
    # would report a missing local file as "ClickHouse is unavailable".
    transport_names = {
        "NetworkError",
        "SocketTimeoutError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "ConnectionError",
        "TimeoutError",
        "socket.timeout",
        "gaierror",
    }
    # A plain ``OSError`` with a network errno is still a transport failure.
    # Local filesystem errnos (ENOENT, EACCES) are deliberately absent.
    transport_errnos = {
        getattr(errno, name)
        for name in (
            "ECONNABORTED",
            "ECONNREFUSED",
            "ECONNRESET",
            "EHOSTDOWN",
            "EHOSTUNREACH",
            "ENETDOWN",
            "ENETRESET",
            "ENETUNREACH",
            "EPIPE",
            "ETIMEDOUT",
        )
        if hasattr(errno, name)
    }
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        names = {cls.__name__ for cls in type(current).__mro__}
        if names & transport_names:
            return True
        if isinstance(current, OSError) and current.errno in transport_errnos:
            return True
        current = current.__cause__ or current.__context__
    return False


def redact(text: Any, secret: Optional[str] = None) -> str:
    """Return ``text`` with the password removed.

    A very short password (fewer than four characters) is only redacted where it
    appears in a credential-shaped context.  Blanket replacement of, say, the
    single character ``1`` would corrupt ordinary diagnostics (ports, PIDs,
    ClickHouse error codes) without adding any protection: this module never
    renders a password into a URL, a command line, or a log line in the first
    place, and those contexts are covered by the targeted patterns below.

    Args:
        text: Value to sanitize. Non-strings are coerced with ``str``.
        secret: Password to redact. When omitted, every known
            ``CLICKHOUSE_PASSWORD`` value and the password in the local config
            are redacted.

    Returns:
        Sanitized string safe to log or display.
    """
    rendered = str(text)
    secrets = set()
    if secret:
        secrets.add(secret)
    env_secret = os.environ.get(ENV_PASSWORD)
    if env_secret:
        secrets.add(env_secret)
    local_secret = load_local_config().get("password")
    if local_secret:
        secrets.add(str(local_secret))

    # Longest first so a password containing another one is fully masked.
    for item in sorted(secrets, key=len, reverse=True):
        if not item:
            continue
        if len(item) >= 4:
            rendered = rendered.replace(item, "***")
            continue
        # Short secret: only mask credential-shaped occurrences.
        pattern = r"(?i)(password[\"']?\s*[:=]\s*[\"']?)" + re.escape(item)
        rendered = re.sub(pattern, r"\1***", rendered)
        pattern = r"(X-ClickHouse-Key\s*:\s*)" + re.escape(item)
        rendered = re.sub(pattern, r"\1***", rendered)
        pattern = r"(" + re.escape(item) + r"@)"
        rendered = re.sub(pattern, "***@", rendered)
    return rendered


#: Re-entry guard for :func:`load_local_config`.
#:
#: The error path logs through :func:`redact`, and ``redact`` consults the local
#: config for a password.  Without this guard a malformed config file makes the
#: two functions call each other until ``RecursionError``.
_LOCAL_CONFIG_GUARD = threading.local()


def load_local_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Read the git-ignored local connection config, if present.

    Args:
        path: Override for :data:`LOCAL_CONFIG_PATH` (used by tests).

    Returns:
        Parsed config dictionary, or an empty dict when the file is absent or
        unreadable.  Errors are logged without their contents so a malformed
        file never leaks a secret.
    """
    if getattr(_LOCAL_CONFIG_GUARD, "active", False):
        # Called from redact() while handling a read error for this same file.
        # Returning {} breaks the cycle; the outer call reports the error.
        return {}

    _LOCAL_CONFIG_GUARD.active = True
    config_path = path or LOCAL_CONFIG_PATH
    try:
        try:
            if not config_path.exists():
                return {}
            with open(config_path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError) as exc:
            logger.warning(
                "Cannot read local ClickHouse config %s: %s", config_path, redact(exc)
            )
            return {}
    finally:
        _LOCAL_CONFIG_GUARD.active = False


def resolve_config(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Merge connection settings from args, environment and local config.

    Precedence: explicit ``overrides`` > environment variables > local config >
    built-in defaults.  Empty strings are treated as "not set" so a blank
    password field in the GUI does not mask ``CLICKHOUSE_PASSWORD``.

    Args:
        overrides: Settings supplied by the caller (for example by the GUI).

    Returns:
        Dictionary with keys ``host``, ``port``, ``database``, ``user``,
        ``password`` and ``secure``.
    """
    local = load_local_config()
    merged: Dict[str, Any] = {
        "host": DEFAULT_HOST,
        "port": DEFAULT_PORT,
        "database": DEFAULT_DATABASE,
        "user": "default",
        "password": "",
        "secure": False,
    }

    for key in ("host", "port", "database", "user", "password", "secure"):
        if key in local and local[key] not in (None, ""):
            merged[key] = local[key]

    env_map = {
        "host": ENV_HOST,
        "port": ENV_PORT,
        "database": ENV_DATABASE,
        "user": ENV_USER,
        "password": ENV_PASSWORD,
    }
    for key, env_name in env_map.items():
        value = os.environ.get(env_name)
        if value:
            merged[key] = value

    for key, value in (overrides or {}).items():
        if key in merged and value not in (None, ""):
            merged[key] = value
        elif key == "policy" and value:
            merged[key] = value

    merged["port"] = int(merged["port"])
    merged["secure"] = bool(merged["secure"])
    database = str(merged["database"])
    try:
        schema.qualified_table(database, TABLE_EVENTS)
    except ValueError as exc:
        raise AdapterError(
            redact(f"Invalid database name {database!r}: {exc}")
        ) from exc
    return merged


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


def _import_driver():
    """Import ``clickhouse_driver`` lazily with an actionable error message.

    Returns:
        The ``clickhouse_driver`` module.

    Raises:
        ClickHouseUnavailable: If the driver is not installed.
    """
    try:
        import clickhouse_driver  # noqa: PLC0415 - optional heavy import
    except ImportError as exc:  # pragma: no cover - environment specific
        raise ClickHouseUnavailable(
            "The 'clickhouse-driver' package is required. "
            "Install it with: pip install -r requirements.txt"
        ) from exc
    return clickhouse_driver


class ClickHouseConnection:
    """Thin, reusable wrapper around a native ClickHouse client.

    The client is created lazily and transparently recreated after a connection
    error, which is what lets the GUI recover from a ClickHouse restart without
    restarting the application.
    """

    def __init__(self, config: Dict[str, Any]):
        """Store connection settings; no socket is opened yet.

        Args:
            config: Result of :func:`resolve_config`.
        """
        self.config = dict(config)
        self._client = None

    # -- lifecycle ---------------------------------------------------------
    def _make_client(self):
        """Create a fresh native client.

        Raises:
            ClickHouseUnavailable: If the driver is missing.
        """
        driver = _import_driver()
        return driver.Client(
            host=self.config["host"],
            port=int(self.config["port"]),
            user=self.config["user"],
            password=self.config["password"],
            database=self.config["database"],
            secure=self.config.get("secure", False),
            connect_timeout=5,
            send_receive_timeout=30,
            sync_request_timeout=30,
            compression=False,
            settings={"server_side_params": 1},
        )

    @property
    def client(self) -> Any:
        """Return the cached client, creating it on first use."""
        if self._client is None:
            self._client = self._make_client()
        return self._client

    def close(self) -> None:
        """Close the underlying client, ignoring already-closed errors."""
        if self._client is not None:
            try:
                self._client.disconnect()
            except Exception:  # pragma: no cover - best effort
                pass
            finally:
                self._client = None

    # -- queries -----------------------------------------------------------
    def execute(self, sql: str, params: Optional[Dict[str, Any]] = None) -> List[Tuple]:
        """Execute a parameterized statement and return rows.

        On a connection-level failure the client is dropped so the next call
        reconnects; this is the recovery path used by the GUI.

        Args:
            sql: SQL text using ``{name:Type}`` placeholders for user input.
            params: Parameter values bound server-side.

        Returns:
            List of row tuples (empty for DDL).

        Raises:
            ClickHouseUnavailable: If ClickHouse cannot be reached.
            AdapterError: For any other server-side error, with the password
                redacted from the message.
        """
        try:
            return self.client.execute(sql, params or {})
        except Exception as exc:  # driver raises several unrelated types
            self.close()
            message = redact(exc, self.config.get("password"))
            if _is_connection_error(exc):
                raise ClickHouseUnavailable(
                    f"Cannot connect to ClickHouse at "
                    f"{self.config['host']}:{self.config['port']}: {message}"
                ) from exc
            raise AdapterError(
                "ClickHouse query failed: "
                + (message.splitlines()[0] if message else "unknown error")
            ) from exc

    def execute_scalar(self, sql: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Execute a query and return the first column of the first row.

        Args:
            sql: SQL with server-side placeholders.
            params: Bound parameter values.

        Returns:
            The scalar value, or ``None`` when the query returned no rows.
        """
        rows = self.execute(sql, params)
        if not rows:
            return None
        return rows[0][0]

    # -- metadata ----------------------------------------------------------
    def ping(self) -> bool:
        """Return ``True`` when a trivial query succeeds.

        Returns:
            Connectivity flag; never raises.
        """
        try:
            self.execute("SELECT 1")
            return True
        except AdapterError:
            return False
        except Exception:  # pragma: no cover - defensive
            return False

    def read_columns(self) -> Dict[str, Dict[str, str]]:
        """Read ``system.columns`` for the configured database.

        Returns:
            Mapping of ``table -> {column: type}``.

        Raises:
            ClickHouseUnavailable: If ClickHouse cannot be reached.
        """
        rows = self.execute(
            """
            SELECT table, name, type
            FROM system.columns
            WHERE database = {db:String}
            ORDER BY table, position
            """,
            {"db": self.config["database"]},
        )
        result: Dict[str, Dict[str, str]] = {}
        for table, column, type_name in rows:
            result.setdefault(table, {})[column] = type_name
        return result

    def table_engines(self) -> Dict[str, str]:
        """Return ``table -> engine`` for the configured database.

        Returns:
            Mapping of table name to engine name.
        """
        rows = self.execute(
            "SELECT name, engine FROM system.tables WHERE database = {db:String}",
            {"db": self.config["database"]},
        )
        return {name: engine for name, engine in rows}


def check_clickhouse_connection(config: Optional[Dict[str, Any]] = None) -> bool:
    """Return ``True`` when ClickHouse is reachable with ``config``.

    Args:
        config: Optional connection overrides.

    Returns:
        Connectivity flag; never raises.
    """
    try:
        resolved = resolve_config(config)
    except AdapterError:
        return False
    connection = ClickHouseConnection(resolved)
    try:
        return connection.ping()
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------


@dataclass
class SchemaStatus:
    """Outcome of :func:`ensure_schema`.

    Attributes:
        ready: ``True`` when the schema matches and the app may query it.
        created: Names of tables created by this call.
        problems: Blocking mismatches. Empty when ``ready`` is ``True``.
        warnings: Non-blocking differences (for example extra columns).
        message: Human-readable summary suitable for a dialog.
    """

    ready: bool
    created: List[str] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    message: str = ""


def _table_name_from_statement(statement: str) -> str:
    """Extract the table name from a rendered ``CREATE TABLE`` statement.

    Args:
        statement: Rendered DDL from :func:`logt_schema.render_create_statements`.

    Returns:
        Bare table name.
    """
    match = re.search(
        r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+`?[A-Za-z0-9_]+`?\.`?([A-Za-z0-9_]+)`?",
        statement,
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else "unknown"


def ensure_schema(
    config: Optional[Dict[str, Any]] = None,
    connection: Optional[ClickHouseConnection] = None,
) -> SchemaStatus:
    """Create missing LogT tables without ever destroying existing data.

    The function is idempotent: running it repeatedly on the same database
    issues only ``CREATE ... IF NOT EXISTS`` statements, so existing rows are
    untouched.  When tables exist with an unexpected structure the function
    reports the difference and returns ``ready=False`` instead of altering
    anything.

    Args:
        config: Connection overrides; resolved via :func:`resolve_config`.
        connection: Optional pre-built connection (used by tests).

    Returns:
        :class:`SchemaStatus` describing the result.

    Raises:
        ClickHouseUnavailable: If the database cannot be reached, or the
            configured database does not exist.
    """
    resolved = connection.config if connection is not None else resolve_config(config)
    conn = connection or ClickHouseConnection(resolved)
    try:
        databases = {row[0] for row in conn.execute("SHOW DATABASES")}
        database = resolved["database"]
        created: List[str] = []
        if database not in databases:
            raise ClickHouseUnavailable(
                f"Database '{database}' does not exist. Create it first, for example: "
                f"CREATE DATABASE IF NOT EXISTS `{database}` (see docs/CLICKHOUSE.md). "
                "The application does not create databases automatically."
            )

        existing = conn.read_columns()
        statements = schema.render_create_statements(database)
        for statement in statements:
            table = _table_name_from_statement(statement)
            if table in existing:
                continue
            conn.execute(statement)
            created.append(table)

        actual = conn.read_columns()
        problems = schema.schema_mismatches(actual)
        warnings = schema.schema_warnings(actual)
    finally:
        if connection is None:
            conn.close()

    if problems:
        details = "\n".join(f"  - {item}" for item in problems)
        message = (
            "ClickHouse schema mismatch detected. Nothing was dropped or altered.\n\n"
            f"{details}\n\n"
            + schema.migration_hint(database)
        )
        if warnings:
            message += "\n\nAdditional notes:\n" + "\n".join(f"  - {w}" for w in warnings)
        return SchemaStatus(
            ready=False, created=created, problems=problems, warnings=warnings, message=message
        )

    summary = "Schema ready."
    if created:
        summary = "Created table(s): " + ", ".join(created) + "."
    if warnings:
        summary += " Notes: " + " ".join(warnings)
    return SchemaStatus(
        ready=True, created=created, problems=[], warnings=warnings, message=summary
    )


def validate_schema(
    config: Optional[Dict[str, Any]] = None,
    connection: Optional[ClickHouseConnection] = None,
) -> SchemaStatus:
    """Validate the schema at startup without making any changes.

    Args:
        config: Connection overrides.
        connection: Optional pre-built connection.

    Returns:
        :class:`SchemaStatus`; ``ready`` is ``False`` when tables are missing or
        differ from the expected structure.
    """
    resolved = connection.config if connection is not None else resolve_config(config)
    conn = connection or ClickHouseConnection(resolved)
    try:
        actual = conn.read_columns()
        problems = schema.schema_mismatches(actual)
        warnings = schema.schema_warnings(actual)
        missing = [t for t in schema.TABLE_SPECS if t not in actual]
    finally:
        if connection is None:
            conn.close()

    if problems:
        details = "\n".join(f"  - {item}" for item in problems)
        message = (
            "ClickHouse is reachable but the LogT schema is not usable.\n\n"
            f"{details}\n\n" + schema.migration_hint(resolved["database"])
        )
        return SchemaStatus(
            ready=False, created=[], problems=problems, warnings=warnings, message=message
        )
    return SchemaStatus(
        ready=True,
        created=[],
        problems=[],
        warnings=warnings,
        message="Schema OK" + (f"; missing tables: {', '.join(missing)}" if missing else ""),
    )


def reset_schema(
    config: Optional[Dict[str, Any]] = None,
    connection: Optional[ClickHouseConnection] = None,
    confirm: bool = False,
) -> SchemaStatus:
    """Drop and recreate the LogT tables (explicit, user-initiated only).

    This is the *only* code path in the project that issues ``DROP``.  It must
    never be called automatically: the GUI exposes it behind a menu item and a
    confirmation dialog, and passes ``confirm=True`` only after the user agrees.

    Args:
        config: Connection overrides.
        connection: Optional pre-built connection.
        confirm: Must be ``True``; otherwise the call is refused before any DDL.

    Returns:
        :class:`SchemaStatus` for the recreated schema.

    Raises:
        AdapterError: If ``confirm`` is not ``True``.
        ClickHouseUnavailable: If the database cannot be reached.
    """
    if not confirm:
        raise AdapterError(
            "reset_schema requires explicit confirmation; refusing to drop tables."
        )

    resolved = connection.config if connection is not None else resolve_config(config)
    conn = connection or ClickHouseConnection(resolved)
    database = resolved["database"]
    dropped: List[str] = []
    try:
        existing = conn.read_columns()
        for table in schema.TABLE_SPECS:
            if table in existing:
                conn.execute(f"DROP TABLE IF EXISTS {schema.qualified_table(database, table)}")
                dropped.append(table)
        for statement in schema.render_create_statements(database):
            conn.execute(statement)
        actual = conn.read_columns()
        problems = schema.schema_mismatches(actual)
    finally:
        if connection is None:
            conn.close()

    message = "Schema reset. Dropped: " + (", ".join(dropped) if dropped else "nothing") + "."
    if problems:
        message += " Post-reset validation reported: " + " ".join(problems)
    return SchemaStatus(ready=not problems, created=list(schema.TABLE_SPECS), problems=problems,
                        warnings=[], message=message)


# ---------------------------------------------------------------------------
# LogT import
# ---------------------------------------------------------------------------


def count_tables(
    config: Optional[Dict[str, Any]] = None,
    connection: Optional[ClickHouseConnection] = None,
    dataset: Optional[str] = None,
) -> Dict[str, int]:
    """Count rows per LogT table — the authoritative import result.

    LogT's stdout is never parsed for control flow (review MAJOR 1); row counts
    come from ClickHouse itself after the subprocess exits.

    Args:
        config: Connection overrides.
        connection: Optional pre-built connection.
        dataset: When given, count only rows for that dataset.

    Returns:
        Mapping of table name to row count. Tables that cannot be read are
        reported as ``-1`` rather than raising, so a partial import still yields
        diagnostics.
    """
    resolved = connection.config if connection is not None else resolve_config(config)
    conn = connection or ClickHouseConnection(resolved)
    counts: Dict[str, int] = {}
    try:
        for table in schema.TABLE_SPECS:
            qualified = schema.qualified_table(resolved["database"], table)
            try:
                if dataset:
                    value = conn.execute_scalar(
                        f"SELECT count() FROM {qualified} "
                        f"WHERE {DATASET_COLUMN} = {{{DATASET_COLUMN}:String}}",
                        {DATASET_COLUMN: dataset},
                    )
                else:
                    value = conn.execute_scalar(f"SELECT count() FROM {qualified}")
                counts[table] = int(value or 0)
            except AdapterError:
                counts[table] = -1
    finally:
        if connection is None:
            conn.close()
    return counts


@dataclass
class ImportResult:
    """Result of a LogT import.

    Attributes:
        success: ``True`` when LogT exited 0 and ClickHouse reported rows.
        exit_code: LogT process exit code.
        stdout: Captured stdout, for diagnostics only (never parsed).
        stderr: Captured stderr, for diagnostics only.
        duration: Wall-clock duration in seconds.
        rows_loaded: Authoritative row count from ClickHouse, or ``None``.
        table_counts: Per-table row counts read from ClickHouse.
        error: Populated when the import failed.
        cancelled: ``True`` when the user cancelled the import.
    """

    success: bool
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration: float = 0.0
    rows_loaded: Optional[int] = None
    table_counts: Dict[str, int] = field(default_factory=dict)
    error: str = ""
    cancelled: bool = False

    @property
    def stderr_tail(self) -> List[str]:
        """Return the last :data:`STDERR_TAIL_LINES` non-empty stderr lines."""
        lines = [line for line in self.stderr.splitlines() if line.strip()]
        return lines[-STDERR_TAIL_LINES:]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ImportResult(success={self.success}, exit_code={self.exit_code}, "
            f"rows={self.rows_loaded}, cancelled={self.cancelled})"
        )


def _resolve_logt_entrypoint(logt_root: Optional[Path] = None) -> Path:
    """Locate LogT's module entry point relative to the repository root.

    Resolution order:
      1. ``logt_root`` argument (or ``LOGT_ROOT`` environment variable).
      2. ``<repo>/vendor/logt``.
      3. Development dependency next to the repository.

    Args:
        logt_root: Optional override for the LogT checkout directory.

    Returns:
        Path to ``main.py``.

    Raises:
        AdapterError: If no LogT entry point can be found.
    """
    candidates: List[Path] = []
    override = logt_root or (Path(os.environ["LOGT_ROOT"]) if os.environ.get("LOGT_ROOT") else None)
    if override:
        candidates.append(Path(override))
    candidates.append(REPO_ROOT / "vendor" / "logt")

    for root in candidates:
        entry = root / "src" / "main.py"
        if entry.exists():
            return entry
    if getattr(sys, "frozen", False):
        raise AdapterError(
            "LogT entry point not found: the packaged application does not include "
            "vendor/logt. Set LOGT_ROOT to the LogT checkout directory that "
            "contains src/main.py (and LOGT_PYTHON to a Python 3.11+ interpreter, "
            "if it is not on PATH)."
        )
    raise AdapterError(
        "LogT entry point not found. Expected vendor/logt/src/main.py "
        "(or set the LOGT_ROOT environment variable)."
    )


def resolve_logt_python() -> str:
    """Return the Python interpreter that should execute LogT.

    In a source checkout ``sys.executable`` is already an interpreter, so the
    behaviour is unchanged.  A frozen (PyInstaller) build is a GUI binary with
    no embedded interpreter, so an external Python 3.11+ is required.  The
    resolution order in that case is:

      1. ``LOGT_PYTHON`` — either a path to an interpreter or a command name.
      2. ``python``, ``python3``, ``py`` looked up on ``PATH``.

    Returns:
        Path to a Python interpreter.

    Raises:
        AdapterError: If the application is frozen and no interpreter is found.
    """
    if not getattr(sys, "frozen", False):
        return sys.executable

    override = (os.environ.get(ENV_LOGT_PYTHON) or "").strip()
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return str(candidate)
        found = shutil.which(override)
        if found:
            return found

    for name in ("python", "python3", "py"):
        found = shutil.which(name)
        if found:
            return found

    raise AdapterError(
        "LogT cannot be started from the packaged application: it needs an "
        "external Python 3.11+ interpreter. Install Python 3.11 or newer and "
        f"point {ENV_LOGT_PYTHON} at python.exe (or add python to PATH), and "
        "set LOGT_ROOT to the LogT checkout directory that contains src/main.py."
    )


def check_logt_runtime(logt_root: Optional[Path] = None) -> str:
    """Verify that LogT can actually be started before an import begins.

    This is the GUI preflight for BUG-006: in a frozen build both the LogT
    sources and the interpreter are external, so the failure must be reported
    before any thread, staging directory or child process is created.

    Args:
        logt_root: Optional LogT checkout override (``LOGT_ROOT`` otherwise).

    Returns:
        Path to the Python interpreter that would run LogT.

    Raises:
        AdapterError: If the LogT entry point or the interpreter is missing.
    """
    _resolve_logt_entrypoint(logt_root)
    return resolve_logt_python()


def _write_private_config(directory: Path, payload: Dict[str, Any]) -> Path:
    """Write a temporary config file readable only by the current user.

    ``os.open(..., 0o600)`` only affects POSIX permission bits; on Windows the
    mode argument is ignored for access control, so an explicit DACL is applied
    and verified there.  The file is created empty, protected, and only then
    filled with the payload, so the password never exists on disk before the
    DACL is verified.  If the permissions cannot be restricted, the file is
    deleted before the error is raised, so a password-bearing file is never left
    behind unprotected.  The staging directory is also removed in
    :func:`run_logt_import`'s ``finally`` block.

    Args:
        directory: Directory that will receive ``config.json``.
        payload: JSON-serializable configuration (contains the password).

    Returns:
        Path to the written file.

    Raises:
        AdapterError: If the file cannot be written, or if its permissions
            cannot be restricted and verified.
    """
    config_path = directory / "config.json"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        # Create the file empty first: the password must not exist on disk
        # before the DACL has been applied and verified.
        descriptor = os.open(str(config_path), flags, 0o600)
        os.close(descriptor)
    except OSError as exc:
        _remove_quietly(config_path)
        raise AdapterError(f"Cannot write LogT config: {redact(exc)}") from exc

    try:
        _restrict_to_current_user(config_path)
    except OSError as exc:
        _remove_quietly(config_path)
        raise AdapterError(
            "Cannot protect the temporary LogT config file; refusing to keep the "
            f"password unprotected. Set {ENV_WORK_DIR} to a private directory or "
            f"fix the file permissions. ({redact(exc)})"
        ) from exc

    try:
        with open(config_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=4)
    except OSError as exc:
        _remove_quietly(config_path)
        raise AdapterError(f"Cannot write LogT config: {redact(exc)}") from exc
    except Exception:
        _remove_quietly(config_path)
        raise
    return config_path


def _remove_quietly(path: Path) -> None:
    """Delete ``path`` when it exists, ignoring secondary failures."""
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - best effort during cleanup
        logger.debug("Could not delete %s", path, exc_info=True)


#: Platform switch kept as a module constant so tests can exercise both branches.
_IS_WINDOWS = os.name == "nt"

#: Lazily initialised Win32 security functions and structures.
_WINDOWS_SECURITY_API: Optional[Dict[str, Any]] = None


def _windows_security_api() -> Dict[str, Any]:
    """Load the Win32 security calls used to set and verify a DACL.

    Prototypes and structures are declared explicitly so 64-bit handles and SID
    pointers are not truncated by ctypes' default ``int`` return type.

    Returns:
        Mapping with ``ctypes``, ``wintypes``, the two DLL handles and the
        ``TOKEN_USER``/``ACL``/``ACL_SIZE_INFORMATION``/
        ``ACCESS_ALLOWED_ACE`` types.
    """
    global _WINDOWS_SECURITY_API
    if _WINDOWS_SECURITY_API is not None:
        return _WINDOWS_SECURITY_API

    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class TOKEN_USER(ctypes.Structure):
        _fields_ = [("User", SID_AND_ATTRIBUTES)]

    class ACL_SIZE_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    class ACL(ctypes.Structure):
        _fields_ = [
            ("AclRevision", wintypes.BYTE),
            ("Sbz1", wintypes.BYTE),
            ("AclSize", wintypes.WORD),
            ("AceCount", wintypes.WORD),
            ("Sbz2", wintypes.WORD),
        ]

    class ACE_HEADER(ctypes.Structure):
        _fields_ = [
            ("AceType", wintypes.BYTE),
            ("AceFlags", wintypes.BYTE),
            ("AceSize", wintypes.WORD),
        ]

    class ACCESS_ALLOWED_ACE(ctypes.Structure):
        _fields_ = [
            ("Header", ACE_HEADER),
            ("Mask", wintypes.DWORD),
            ("SidStart", wintypes.DWORD),
        ]

    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSidToSidW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi32.GetAclInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_int,
    ]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi32.EqualSid.restype = wintypes.BOOL
    advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
    advapi32.GetLengthSid.restype = wintypes.DWORD
    advapi32.InitializeAcl.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    advapi32.InitializeAcl.restype = wintypes.BOOL
    advapi32.AddAccessAllowedAce.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    advapi32.AddAccessAllowedAce.restype = wintypes.BOOL
    advapi32.InitializeSecurityDescriptor.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    advapi32.InitializeSecurityDescriptor.restype = wintypes.BOOL
    advapi32.SetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        wintypes.BOOL,
        ctypes.c_void_p,
        wintypes.BOOL,
    ]
    advapi32.SetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi32.SetFileSecurityW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    advapi32.SetFileSecurityW.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    _WINDOWS_SECURITY_API = {
        "ctypes": ctypes,
        "wintypes": wintypes,
        "advapi32": advapi32,
        "kernel32": kernel32,
        "TOKEN_USER": TOKEN_USER,
        "ACL_SIZE_INFORMATION": ACL_SIZE_INFORMATION,
        "ACL": ACL,
        "ACCESS_ALLOWED_ACE": ACCESS_ALLOWED_ACE,
    }
    return _WINDOWS_SECURITY_API


def _current_user_sid() -> str:
    """Return the current Windows account SID in ``S-1-...`` form.

    Raises:
        OSError: If the process token cannot be queried.
    """
    api = _windows_security_api()
    ctypes = api["ctypes"]
    wintypes = api["wintypes"]
    advapi32 = api["advapi32"]
    kernel32 = api["kernel32"]

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)  # TOKEN_QUERY
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
        if not size.value:
            raise ctypes.WinError(ctypes.get_last_error())
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(
            token, 1, buffer, size.value, ctypes.byref(size)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        token_user = ctypes.cast(buffer, ctypes.POINTER(api["TOKEN_USER"])).contents
        sid_text = ctypes.c_wchar_p()
        if not advapi32.ConvertSidToStringSidW(
            token_user.User.Sid, ctypes.byref(sid_text)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return str(sid_text.value or "")
        finally:
            kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle(token)


def _set_windows_private_dacl(path: Path, sid_text: str) -> None:
    """Replace ``path``'s DACL with a single full-access ACE for ``sid_text``.

    The DACL is built in memory and applied with ``SetFileSecurityW``; no
    external ``icacls`` process is involved, so the operation cannot be broken
    by a localized shell or a missing PATH entry.

    Args:
        path: File whose DACL should be replaced.
        sid_text: Current user's SID in ``S-1-...`` form.

    Raises:
        OSError: If any Win32 call fails.
    """
    api = _windows_security_api()
    ctypes = api["ctypes"]
    wintypes = api["wintypes"]
    advapi32 = api["advapi32"]
    kernel32 = api["kernel32"]

    FILE_ALL_ACCESS = 0x1F01FF
    ACL_REVISION = 2
    SECURITY_DESCRIPTOR_REVISION = 1
    DACL_SECURITY_INFORMATION = 0x00000004

    sid = ctypes.c_void_p()
    if not advapi32.ConvertStringSidToSidW(sid_text, ctypes.byref(sid)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        sid_length = advapi32.GetLengthSid(sid)
        if not sid_length:
            raise ctypes.WinError(ctypes.get_last_error())

        # Documented layout: ACL header + ACE header + SID, minus the four bytes
        # of SidStart that are already counted in the ACE header.  A small
        # alignment slack keeps AddAccessAllowedAce from failing on padding.
        acl_size = (
            ctypes.sizeof(api["ACL"])
            + ctypes.sizeof(api["ACCESS_ALLOWED_ACE"])
            - ctypes.sizeof(wintypes.DWORD)
            + sid_length
            + 16
        )
        acl = ctypes.create_string_buffer(acl_size)
        if not advapi32.InitializeAcl(acl, acl_size, ACL_REVISION):
            raise ctypes.WinError(ctypes.get_last_error())
        if not advapi32.AddAccessAllowedAce(
            acl, ACL_REVISION, FILE_ALL_ACCESS, sid
        ):
            raise ctypes.WinError(ctypes.get_last_error())

        descriptor = ctypes.create_string_buffer(64)
        if not advapi32.InitializeSecurityDescriptor(
            descriptor, SECURITY_DESCRIPTOR_REVISION
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not advapi32.SetSecurityDescriptorDacl(
            descriptor, True, acl, False
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not advapi32.SetFileSecurityW(
            str(path), DACL_SECURITY_INFORMATION, descriptor
        ):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel32.LocalFree(sid)


def _windows_dacl_matches(path: Path, sid_text: str) -> bool:
    """Return whether ``path`` has exactly one full-access ACE for ``sid_text``.

    Reading the DACL back is what turns the permission change from "best
    effort" into a verified guarantee.  Inherited entries do not appear here
    because :func:`_restrict_to_current_user` removes inheritance first.

    Raises:
        OSError: If the security descriptor cannot be read.
    """
    api = _windows_security_api()
    ctypes = api["ctypes"]
    wintypes = api["wintypes"]
    advapi32 = api["advapi32"]
    kernel32 = api["kernel32"]

    p_security = ctypes.c_void_p()
    p_dacl = ctypes.c_void_p()
    result = advapi32.GetNamedSecurityInfoW(
        str(path),
        1,  # SE_FILE_OBJECT
        0x00000004,  # DACL_SECURITY_INFORMATION
        None,
        None,
        ctypes.byref(p_dacl),
        None,
        ctypes.byref(p_security),
    )
    if result != 0:
        raise OSError(result, ctypes.FormatError(result))
    try:
        if not p_dacl:
            return False

        info = api["ACL_SIZE_INFORMATION"]()
        if not advapi32.GetAclInformation(
            p_dacl,
            ctypes.byref(info),
            ctypes.sizeof(info),
            2,  # AclSizeInformation
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.AceCount != 1:
            return False

        p_ace = ctypes.c_void_p()
        if not advapi32.GetAce(p_dacl, 0, ctypes.byref(p_ace)):
            raise ctypes.WinError(ctypes.get_last_error())
        ace = ctypes.cast(p_ace, ctypes.POINTER(api["ACCESS_ALLOWED_ACE"])).contents
        if ace.Header.AceType != 0:  # ACCESS_ALLOWED_ACE_TYPE
            return False
        FILE_ALL_ACCESS = 0x1F01FF
        if ace.Mask & FILE_ALL_ACCESS != FILE_ALL_ACCESS:
            return False

        expected = ctypes.c_void_p()
        if not advapi32.ConvertStringSidToSidW(sid_text, ctypes.byref(expected)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ace_sid = ctypes.c_void_p(
                ctypes.addressof(ace) + api["ACCESS_ALLOWED_ACE"].SidStart.offset
            )
            return bool(advapi32.EqualSid(ace_sid, expected))
        finally:
            kernel32.LocalFree(expected)
    finally:
        kernel32.LocalFree(p_security)


def _restrict_to_current_user(path: Path) -> None:
    """Make ``path`` accessible only to the current user, or raise ``OSError``.

    On POSIX the permission bits are set to ``0o600`` and read back.  On Windows
    an explicit DACL is applied with the Win32 API using the current account's
    SID, then read back and verified to contain exactly one full-access ACE for
    that SID (DEBT-008).  Callers treat a failure as fatal for the import: the
    password-bearing file is deleted instead of being left on disk with unknown
    permissions.

    Args:
        path: File to restrict.

    Raises:
        OSError: If the permissions cannot be applied or verified.
    """
    if not _IS_WINDOWS:
        os.chmod(path, 0o600)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode & 0o077:
            raise OSError(
                f"Permission bits {oct(mode)} still allow group/other access"
            )
        return

    sid = _current_user_sid()
    _set_windows_private_dacl(path, sid)
    if not _windows_dacl_matches(path, sid):
        raise OSError(
            f"ACL verification failed for {path}: expected exactly one ACE for "
            f"SID {sid}"
        )


def resolve_work_dir() -> Path:
    """Return a writable directory for staging temporary import files.

    Resolution order:
      1. ``LOGT_WORK_DIR`` environment variable.
      2. The system temp directory, when it is actually writable.
      3. ``<repo>/.logt_local`` as a last resort.

    Some hardened Windows hosts deny ``FILE_ADD_FILE`` on the inherited temp
    directory, so writability is probed rather than assumed.

    Returns:
        Path to a directory that can hold staging files.

    Raises:
        AdapterError: If no candidate directory is writable.
    """
    import uuid

    candidates: List[Path] = []
    override = os.environ.get(ENV_WORK_DIR)
    if override:
        candidates.append(Path(override))
    candidates.append(Path(tempfile.gettempdir()))
    candidates.append(LOGT_CONFIG_DIR)

    errors: List[str] = []
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / f"write-probe-{uuid.uuid4().hex[:8]}.tmp"
            probe.write_text("probe", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return candidate
        except OSError as exc:
            errors.append(f"{candidate}: {redact(exc)}")

    raise AdapterError(
        "No writable directory for temporary import files. Set the "
        f"{ENV_WORK_DIR} environment variable to a writable path. "
        "Tried: " + "; ".join(errors)
    )


def _make_staging_dir(parent: Path) -> Path:
    """Create a unique, writable staging directory under ``parent``.

    ``tempfile.mkdtemp`` is deliberately not used: it creates the directory with
    mode ``0o700``, which on some hardened Windows hosts denies the very token
    that created it (the effective access comes from group entries, not from the
    owner entry).  Creating the directory with inherited permissions keeps it
    usable.  Confidentiality does not depend on the directory mode — the
    ``config.json`` written inside it is created mode ``0o600`` via
    :func:`_write_private_config`.

    Args:
        parent: Directory that receives the new staging directory.

    Returns:
        Path to the created directory.

    Raises:
        AdapterError: If the directory cannot be created.
    """
    import uuid

    for _ in range(10):
        candidate = parent / f"logt-run-{uuid.uuid4().hex[:12]}"
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:  # pragma: no cover - vanishingly unlikely
            continue
        except OSError as exc:
            raise AdapterError(
                redact(f"Cannot create a staging directory in {parent}: {exc}")
            ) from exc
    raise AdapterError(  # pragma: no cover - defensive
        f"Cannot create a unique staging directory in {parent}"
    )


def build_logt_config(
    log_dir: str,
    config: Dict[str, Any],
    dataset_name: str = DEFAULT_DATASET,
    min_operation_duration_ms: int = 0,
    min_operation_events: int = 0,
    temp_dir: Optional[str] = None,
    count_lines_for_progress: bool = True,
) -> Dict[str, Any]:
    """Build the JSON payload LogT expects in its ``config.json``.

    Args:
        log_dir: Directory containing 1C technological logs.
        config: Resolved clickhouse connection settings.
        dataset_name: Dataset tag written into the ``dataset`` column.
        min_operation_duration_ms: LogT filtering threshold.
        min_operation_events: LogT filtering threshold.
        temp_dir: Optional LogT working directory.
        count_lines_for_progress: When true, LogT counts the lines of every
            input file before processing it.  That extra pass makes the tqdm
            bar determinate (``total`` is known) at the cost of a slower start;
            the GUI exposes it as an opt-out checkbox (P1-04).

    Returns:
        Configuration dictionary ready to be serialized.

    Note:
        LogT reads ClickHouse settings from ``config["clickhouse"]["url"]``,
        which is an HTTP URL.  The application itself uses the native protocol
        (port 9000); the mapping is derived from the configured host and the
        conventional HTTP port 8123, so no password ever appears in a URL that
        this application requests — only in LogT's own config file.
    """
    http_url = f"http://{config['host']}:8123"
    logt_log_dir = temp_dir or str((Path(log_dir) / ".logt_logs").resolve())
    return {
        "clickhouse": {
            "url": http_url,
            "user": config["user"],
            "password": config["password"],
            "database": config["database"],
        },
        "processing": {
            "mode": "single",
            "batch_size": 10000,
            "timeout": 120,
            "dataset_name": dataset_name,
            "cleanup_temp_dir": False,
            "client_server_operations": True,
            "background_jobs": True,
            "insert_compression": "none",
            "gzip_level": 1,
            "count_lines_for_progress": bool(count_lines_for_progress),
        },
        "filtering": {
            "min_operation_duration_ms": min_operation_duration_ms,
            "min_operation_events": min_operation_events,
        },
        "paths": {
            "input_dir": str(Path(log_dir).resolve()),
            "log_dir": logt_log_dir,
        },
    }


def run_logt_import(
    log_dir: str,
    config: Optional[Dict[str, Any]] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
    cancel_event: Optional["object"] = None,
    dataset_name: str = DEFAULT_DATASET,
    min_operation_duration_ms: int = 0,
    min_operation_events: int = 0,
    logt_root: Optional[Path] = None,
    connection: Optional[ClickHouseConnection] = None,
    *,
    on_progress: Optional[Callable[[ImportProgress], None]] = None,
    count_lines_for_progress: bool = True,
    progress_interval: float = 0.1,
) -> ImportResult:
    """Run LogT as a subprocess, then read authoritative row counts.

    The password never appears on the command line: it is written to a private
    temporary ``config.json`` whose path is passed to the child process, and the
    file is removed in a ``finally`` block.

    Args:
        log_dir: Directory containing 1C technological logs.
        config: Connection overrides.
        progress_callback: Optional ``callback(message)`` for status updates.
            Legacy, string based; still receives every stdout line.
        cancel_event: Object with ``is_set()`` used to abort the import.
        dataset_name: Dataset tag for this import.
        min_operation_duration_ms: LogT filtering threshold.
        min_operation_events: LogT filtering threshold.
        logt_root: Optional LogT checkout override.
        connection: Optional pre-built connection for the count step.
        on_progress: Optional ``callback(ImportProgress)`` receiving typed,
            redacted and throttled snapshots (P1-03).  Called from the pump
            threads; the GUI marshals it to the Tk thread (P1-04).
        count_lines_for_progress: Forwarded to LogT as
            ``processing.count_lines_for_progress``; makes the bar determinate.
        progress_interval: Minimum seconds between two non-forced typed events.

    Returns:
        :class:`ImportResult` with ClickHouse-derived ``rows_loaded``.

    Raises:
        AdapterError: If the LogT entry point or config cannot be prepared.
    """
    started = time.time()
    resolved = connection.config if connection is not None else resolve_config(config)

    # P1-03: typed progress.  ``PREPARING`` is sent before any filesystem or
    # child-process work so the GUI can show a phase immediately.
    bridge: Optional[_ProgressBridge] = None
    if on_progress is not None:
        bridge = _ProgressBridge(
            on_progress,
            resolved.get("password"),
            min_interval=progress_interval,
        )
        bridge.emit(
            ImportProgress(ImportStage.PREPARING, "Подготовка импорта LogT…"),
            force=True,
        )

    entry = _resolve_logt_entrypoint(logt_root)
    # BUG-006: a frozen build cannot reuse ``sys.executable`` (it is the GUI
    # binary); the interpreter is resolved up front so a missing one fails
    # before any temporary file or child process is created.
    python_executable = resolve_logt_python()

    def emit(message: str) -> None:
        """Forward a progress message, redacted with the effective password."""
        if progress_callback:
            try:
                progress_callback(redact(message, resolved["password"]))
            except Exception:  # pragma: no cover - UI callback must not break import
                logger.debug("progress_callback raised", exc_info=True)

    payload = build_logt_config(
        log_dir,
        resolved,
        dataset_name=dataset_name,
        min_operation_duration_ms=min_operation_duration_ms,
        min_operation_events=min_operation_events,
        count_lines_for_progress=count_lines_for_progress,
    )

    # LogT loads config.json from its own directory, so the entry point is
    # staged beside a private copy of the config rather than mutating the
    # vendored checkout.
    work_dir = resolve_work_dir()
    staging = _make_staging_dir(work_dir)
    config_path: Optional[Path] = None
    process: Optional[subprocess.Popen] = None
    try:
        staged_entry = staging / "main.py"
        staged_entry.write_text(entry.read_text(encoding="utf-8"), encoding="utf-8")
        config_path = _write_private_config(staging, payload)
        emit("LogT config written to a temporary location (password not logged).")

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env.pop(ENV_PASSWORD, None)  # keep the secret out of the child environment
        env["PYTHONPATH"] = os.pathsep.join(
            [str(REPO_ROOT)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
        )

        process = subprocess.Popen(
            [python_executable, "-u", str(staged_entry)],
            cwd=str(staging),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )

        stdout_parts: List[str] = []
        stderr_parts: List[str] = []
        # P1-03: the legacy string callback keeps every stdout line; typed
        # progress additionally feeds the same line into the parser.  stderr is
        # only used for typed progress, and tqdm redraws are filtered out of the
        # diagnostic tail so real errors stay visible (P1-03 / F3).
        on_stdout_line: Optional[Callable[[str], None]] = emit
        on_stderr_line: Optional[Callable[[str], None]] = None
        store_stderr_line: Optional[Callable[[str], bool]] = None
        if bridge is not None:
            def on_stdout_line(text: str) -> None:
                """Report a stdout line to both the legacy and typed sinks."""
                emit(text)
                bridge.feed_stdout(text)

            on_stderr_line = bridge.feed_stderr
            store_stderr_line = lambda text: not is_tqdm_line(text)

        # Drain both pipes on background threads so a chatty child can never
        # deadlock on a full pipe buffer, and so cancellation stays responsive.
        # Output is captured for diagnostics only — never parsed for control flow.
        try:
            _drain_process(
                process,
                stdout_parts,
                stderr_parts,
                on_line=on_stdout_line,
                cancel_event=cancel_event,
                on_stderr_line=on_stderr_line,
                store_stderr_line=store_stderr_line,
            )
        finally:
            _close_streams(process)

        exit_code = process.returncode if process.returncode is not None else -1
        stdout_text = "\n".join(stdout_parts)
        stderr_text = "\n".join(stderr_parts)
        duration = time.time() - started

        if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
            return ImportResult(
                success=False,
                exit_code=exit_code,
                stdout=redact(stdout_text, resolved["password"]),
                stderr=redact(stderr_text, resolved["password"]),
                duration=duration,
                error="Import cancelled by user.",
                cancelled=True,
            )

        if exit_code != 0:
            tail = "\n".join(stderr_parts[-STDERR_TAIL_LINES:])
            message = (
                f"LogT exited with code {exit_code}.\n\n"
                f"Last {STDERR_TAIL_LINES} stderr lines:\n{tail or '(no stderr output)'}"
            )
            return ImportResult(
                success=False,
                exit_code=exit_code,
                stdout=redact(stdout_text, resolved["password"]),
                stderr=redact(stderr_text, resolved["password"]),
                duration=duration,
                error=redact(message, resolved["password"]),
            )

        emit("LogT finished; reading row counts from ClickHouse...")
        if bridge is not None:
            bridge.emit(
                ImportProgress(
                    ImportStage.COUNTING,
                    "Подсчёт строк в ClickHouse…",
                    percent=95.0,
                )
            )
        counts: Dict[str, int] = {}
        count_error = ""
        try:
            counts = count_tables(
                config=resolved, connection=connection, dataset=dataset_name
            )
        except AdapterError as exc:
            count_error = str(exc)
            stderr_text = (stderr_text + f"\n{exc}").strip()

        readable = {table: value for table, value in counts.items() if value >= 0}
        loaded = readable.get(TABLE_EVENTS)
        if loaded is None:
            loaded = sum(value for value in readable.values() if value > 0)

        # LogT exits 0 even when every ClickHouse insert failed (it logs insert
        # errors to stdout and carries on).  ClickHouse counts are therefore the
        # authority: if nothing is readable, or nothing was written, the import
        # did not succeed and the user must be told why.
        if counts and not readable:
            message = (
                "LogT finished but ClickHouse row counts could not be read, so the "
                "import cannot be verified.\n\n"
                f"{count_error or 'No tables were readable.'}\n\n"
                f"Last {STDERR_TAIL_LINES} stderr lines:\n"
                + ("\n".join(stderr_parts[-STDERR_TAIL_LINES:]) or "(no stderr output)")
            )
            return ImportResult(
                success=False,
                exit_code=exit_code,
                stdout=redact(stdout_text, resolved["password"]),
                stderr=redact(stderr_text, resolved["password"]),
                duration=duration,
                rows_loaded=0,
                table_counts=counts,
                error=redact(message, resolved["password"]),
            )

        if not loaded:
            message = (
                "LogT finished with exit code 0 but ClickHouse contains 0 events for "
                f"dataset '{dataset_name}', so nothing was imported.\n\n"
                "Common causes: the selected folder has no rphost_*/rmngr_* log files, "
                "the schema does not match (see 'Validate ClickHouse schema'), or "
                "ClickHouse rejected the inserts.\n\n"
                f"Last {STDERR_TAIL_LINES} stdout lines:\n"
                + ("\n".join(stdout_parts[-STDERR_TAIL_LINES:]) or "(no stdout output)")
            )
            return ImportResult(
                success=False,
                exit_code=exit_code,
                stdout=redact(stdout_text, resolved["password"]),
                stderr=redact(stderr_text, resolved["password"]),
                duration=duration,
                rows_loaded=0,
                table_counts=counts,
                error=redact(message, resolved["password"]),
            )

        if bridge is not None:
            bridge.emit(
                ImportProgress(
                    ImportStage.DONE,
                    f"Импорт завершён: {loaded} событий за {duration:.1f} с",
                    percent=100.0,
                ),
                force=True,
            )

        return ImportResult(
            success=True,
            exit_code=exit_code,
            stdout=redact(stdout_text, resolved["password"]),
            stderr=redact(stderr_text, resolved["password"]),
            duration=duration,
            rows_loaded=loaded,
            table_counts=counts,
        )
    finally:
        if bridge is not None:
            bridge.close()
        if process is not None and process.poll() is None:  # pragma: no cover - safety
            _terminate(process)
        if config_path is not None:
            try:
                config_path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best effort
                logger.warning("Could not delete temporary LogT config")
        try:
            shutil.rmtree(staging, ignore_errors=True)
        except Exception:  # pragma: no cover - best effort
            pass


def _redact_progress(
    progress: ImportProgress, secret: Optional[str]
) -> ImportProgress:
    """Return ``progress`` with ``message``/``detail`` passed through :func:`redact`.

    Args:
        progress: Raw snapshot produced by the parser or by the adapter.
        secret: Effective password to mask.

    Returns:
        The same snapshot when nothing needed masking, otherwise a redacted
        copy.  ``stage``/``current``/``total``/``percent`` are preserved.
    """
    message = redact(progress.message, secret) if progress.message else progress.message
    detail = (
        redact(progress.detail, secret) if progress.detail is not None else None
    )
    if message == progress.message and detail == progress.detail:
        return progress
    return ImportProgress(
        stage=progress.stage,
        message=message,
        current=progress.current,
        total=progress.total,
        percent=progress.percent,
        detail=detail,
    )


class _ProgressBridge:
    """Connect LogT's two output streams to :class:`ProgressReporter`.

    ``stdout`` and ``stderr`` are pumped by two independent threads, so the
    parser is only touched under a lock.  The reporter (and therefore the user
    callback) is invoked *outside* that lock: a slow GUI handler must not stall
    the other stream, and the parser must never be re-entered concurrently.

    The bridge is deliberately private: callers pass ``on_progress`` to
    :func:`run_logt_import` and never see this class.

    Args:
        callback: Receives throttled, redacted :class:`ImportProgress` values on
            the pump thread that produced them.
        secret: Effective password, used to redact messages and details.
        min_interval: Minimum seconds between two non-forced events.
    """

    def __init__(
        self,
        callback: Callable[[ImportProgress], None],
        secret: Optional[str] = None,
        *,
        min_interval: float = 0.1,
    ) -> None:
        self._secret = secret
        self._parser = LogTProgressParser()
        self._lock = threading.Lock()
        self._reporter = ProgressReporter(callback, min_interval=min_interval)

    # -- output streams ----------------------------------------------------
    def feed_stdout(self, line: str) -> None:
        """Parse one stdout line/chunk and report a snapshot when there is one."""
        self._feed(self._parser.feed_stdout, line)

    def feed_stderr(self, line: str) -> None:
        """Parse one stderr line/chunk (tqdm ticks) and report a snapshot."""
        self._feed(self._parser.feed_stderr, line)

    def _feed(self, handler: Callable[[str], Optional[ImportProgress]], line: str) -> None:
        """Run ``handler`` under the parser lock, then emit outside it."""
        try:
            with self._lock:
                snapshot = handler(line)
        except Exception:  # pragma: no cover - a log line must never stop an import
            logger.debug("LogT progress parser raised", exc_info=True)
            return
        if snapshot is not None:
            self._reporter.emit(_redact_progress(snapshot, self._secret))

    # -- adapter-owned stages ---------------------------------------------
    def emit(self, progress: ImportProgress, force: bool = False) -> None:
        """Report a snapshot produced by the adapter itself."""
        self._reporter.emit(_redact_progress(progress, self._secret), force=force)

    def close(self) -> None:
        """Stop delivering events; idempotent."""
        self._reporter.close()


def _pump_stream(
    stream,
    sink: List[str],
    on_line: Optional[Callable[[str], None]] = None,
    store_line: Optional[Callable[[str], bool]] = None,
) -> None:
    """Read a child process pipe line by line into ``sink``.

    Runs on a daemon thread so both pipes are drained concurrently.

    Args:
        stream: Text-mode pipe to read until EOF.
        sink: List that receives the stripped lines.
        on_line: Optional callback invoked for each non-empty line.  It is
            called even when ``store_line`` rejects the line, because progress
            consumers still want tqdm ticks that diagnostics do not.
        store_line: Optional ``store_line(text) -> bool`` filter.  Returning
            ``False`` keeps the line out of ``sink`` (used to stop tqdm ticks
            from crowding real errors out of ``stderr_tail``).  An exception is
            treated as ``True``: keeping a diagnostic line is always safer than
            losing it.
    """
    try:
        for line in iter(stream.readline, ""):
            text = line.rstrip("\r\n")
            keep = True
            if store_line is not None:
                try:
                    keep = bool(store_line(text))
                except Exception:  # pragma: no cover - filter must not kill the pump
                    logger.debug("stream store filter raised", exc_info=True)
                    keep = True
            if keep:
                sink.append(text)
            if on_line is not None and text.strip():
                try:
                    on_line(text.strip())
                except Exception:  # pragma: no cover - callback must not kill the pump
                    logger.debug("stream callback raised", exc_info=True)
    except (ValueError, OSError):  # pragma: no cover - pipe closed during shutdown
        pass
    finally:
        try:
            stream.close()
        except Exception:  # pragma: no cover - best effort
            pass


#: Seconds a child gets to honour ``terminate()`` before ``kill()`` is sent.
TERMINATE_GRACE = 1.0

#: Seconds allowed for reaping a killed child.  ``kill()`` is immediate, so this
#: is only a safety net and must not stretch the cancellation budget.
KILL_GRACE = 0.5

#: Total seconds spent joining the stdout/stderr pump threads after shutdown.
#: The pumps are daemons over pipes owned by the child, so they end on EOF.
PUMP_JOIN_TIMEOUT = 0.5


def _drain_process(
    process: subprocess.Popen,
    stdout_sink: List[str],
    stderr_sink: List[str],
    on_line: Optional[Callable[[str], None]] = None,
    cancel_event: Optional["object"] = None,
    poll_interval: float = 0.1,
    pump_join_timeout: float = PUMP_JOIN_TIMEOUT,
    on_stderr_line: Optional[Callable[[str], None]] = None,
    store_stderr_line: Optional[Callable[[str], bool]] = None,
) -> None:
    """Wait for a child process while draining stdout and stderr.

    Cancellation is checked on a short interval and implemented by terminating
    the child, so no orphaned process is left behind.  The whole shutdown path
    is bounded: a full ``terminate()``/``kill()`` cycle costs at most
    ``TERMINATE_GRACE + KILL_GRACE`` and the pipe pumps are joined for at most
    ``pump_join_timeout`` in total, so a cancelled import returns within the
    two-second acceptance budget.

    Args:
        process: The running child.
        stdout_sink: List receiving stdout lines.
        stderr_sink: List receiving stderr lines.
        on_line: Callback for each stdout line (used for progress messages).
        cancel_event: Object with ``is_set()``; when set the child is terminated.
        poll_interval: Seconds between cancellation checks.
        pump_join_timeout: Total seconds spent joining the two pump threads.
        on_stderr_line: Optional callback for each stderr line (typed progress).
        store_stderr_line: Optional filter deciding whether a stderr line is
            kept in ``stderr_sink``.  The callback still sees every line.
    """
    import threading

    stdout_thread = threading.Thread(
        target=_pump_stream, args=(process.stdout, stdout_sink), kwargs={"on_line": on_line},
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_pump_stream,
        args=(process.stderr, stderr_sink),
        kwargs={"on_line": on_stderr_line, "store_line": store_stderr_line},
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    try:
        while process.poll() is None:
            if cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)():
                _terminate(process)
                break
            try:
                process.wait(timeout=poll_interval)
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:  # pragma: no cover - safety net
            _terminate(process)
        # Give the pumps a short, bounded moment to flush buffered output.  They
        # are daemon threads reading pipes whose write ends die with the child,
        # so EOF arrives as soon as the child is reaped; the deadline keeps the
        # cancellation budget intact even if a grandchild still holds a pipe.
        deadline = time.monotonic() + pump_join_timeout
        for thread in (stdout_thread, stderr_thread):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=remaining)


def _terminate(process: subprocess.Popen, *, grace: float = TERMINATE_GRACE) -> None:
    """Terminate a child process, escalating to kill when needed.

    The call is best effort but bounded: ``grace`` seconds are allowed for
    ``terminate()`` to take effect and a shorter, fixed budget for the
    ``kill()`` fallback, so the caller never waits much longer than
    ``grace`` plus :data:`KILL_GRACE`.  When the function returns the child has
    normally been reaped and :meth:`subprocess.Popen.poll` reports an exit code.

    Args:
        process: The process to stop.
        grace: Seconds to wait for a graceful ``terminate()``.
    """
    try:
        if process.poll() is not None:
            logger.debug("LogT process already finished; nothing to terminate")
            return
    except OSError:
        # ``ProcessLookupError``/``PermissionError`` are ``OSError`` subclasses.
        logger.debug("Could not poll LogT process before terminating", exc_info=True)
        return

    try:
        process.terminate()
    except ProcessLookupError:
        logger.debug("LogT process exited before terminate()")
    except OSError:
        logger.debug("terminate() failed for LogT process", exc_info=True)

    try:
        process.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        logger.debug("LogT process ignored terminate(); sending kill()")
    except OSError:
        logger.debug("wait() failed after terminate()", exc_info=True)
        return

    try:
        process.kill()
    except ProcessLookupError:
        logger.debug("LogT process exited before kill()")
    except OSError:
        logger.debug("kill() failed for LogT process", exc_info=True)

    try:
        # SIGKILL/TerminateProcess is immediate, so a short wait is enough to
        # reap the child without stretching the cancellation budget.
        process.wait(timeout=min(grace, KILL_GRACE))
    except subprocess.TimeoutExpired:  # pragma: no cover - unkillable child
        logger.warning("LogT process did not exit after kill()")
    except OSError:
        logger.debug("wait() failed after kill()", exc_info=True)


def _close_streams(process: subprocess.Popen) -> None:
    """Close a process's pipes, ignoring errors.

    Args:
        process: Process whose streams should be closed.
    """
    for stream in (
        getattr(process, "stdout", None),
        getattr(process, "stderr", None),
        getattr(process, "stdin", None),
    ):
        try:
            if stream is not None:
                stream.close()
        except Exception:  # pragma: no cover - best effort
            pass


# ---------------------------------------------------------------------------
# Tab queries (BLOCKER 2) — all aggregation happens in ClickHouse
# ---------------------------------------------------------------------------

#: Event names treated as errors.  Kept in :mod:`ics_log_constants` so the
#: in-memory and ClickHouse paths share one definition; the values are bound as
#: query parameters.

#: Maximum page size accepted by the error list.
MAX_PAGE_SIZE = 500

#: Largest offset addressable by the ``{offset:UInt64}`` SQL placeholder.
_UINT64_MAX = 2**64 - 1

#: RE2 pattern capturing the value of the ``Descr`` field in a raw log event.
#:
#: The only capturing group returns the value *including* its surrounding
#: quotes.  Real 1C logs use all three forms — ``Descr="..."`` (comma and
#: newline safe), ``Descr='...'`` (multi-line exception text) and bare
#: ``Descr=...`` up to the next comma or line break.  Escaped characters
#: (``\"``, ``\'``, ``\\``) are accepted inside quotes, so an escaped quote
#: does not end the value early.
DESCRIPTION_VALUE_PATTERN = (
    r"(?:^|[\s,])\s*Descr=\s*"
    r"("
    r'"(?:[^"\\]|\\.)*"'
    r"|'(?:[^'\\]|\\.)*'"
    r"|[^,\r\n]*"
    r")"
)

#: RE2 pattern extracting the ``AppID`` value from a raw log event.
#:
#: ``AppID`` has no dedicated column in the ``events`` table; it is only
#: available inside ``event_string``, where it appears as a comma-separated
#: ``AppID=value`` field.  This pattern is a constant — the user-supplied
#: value is always bound as a query parameter.
APP_ID_VALUE_PATTERN = r"AppID=([^,]+)"

#: Upper bound for :attr:`Filters.min_duration_ms` (24 hours in milliseconds).
MAX_MIN_DURATION_MS = 86_400_000


def _quote_sql_string(value: str) -> str:
    """Render ``value`` as a ClickHouse string literal.

    Backslashes and single quotes are escaped.  Only internal constants pass
    through here; user-supplied data is always bound as a query parameter.

    Args:
        value: Literal text to quote.

    Returns:
        A single-quoted SQL string literal.
    """
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def _clean_description_value(expression: str) -> str:
    """Strip surrounding quotes/spaces and unescape a raw ``Descr`` value.

    Args:
        expression: SQL expression returning the value *with* its quotes.

    Returns:
        SQL expression returning the human-readable value.
    """
    expression = (
        f"trim(BOTH {_quote_sql_string(chr(34))} FROM "
        f"trim(BOTH {_quote_sql_string(chr(39))} FROM "
        f"trim({expression})))"
    )
    # Unescape quoted forms last-to-first: escaped quotes before backslashes,
    # otherwise a sequence such as ``\\\"`` would lose its literal backslash.
    for escaped, plain in ((r'\"', '"'), (r"\'", "'"), (r"\\", "\\")):
        expression = (
            f"replaceAll({expression}, {_quote_sql_string(escaped)}, "
            f"{_quote_sql_string(plain)})"
        )
    return expression


def build_description_expressions(
    event_string_column: str = EVENT_STRING_COLUMN,
) -> Tuple[str, str]:
    """Build SQL deriving a description and an "excerpt" flag from raw events.

    Schema gap (DEBT-007): the LogT ``events`` table stores the whole original
    log event in ``event_string`` and has no dedicated ``descr`` column.  Both
    expressions therefore stay inside ClickHouse; no row data is fetched or
    parsed in Python.

    Extraction order:

    1. ``Descr="..."`` — double-quoted value (may contain commas/newlines).
    2. ``Descr='...'`` — single-quoted value, common for exception text.
    3. ``Descr=...`` — unquoted value up to the next comma or line break.
    4. Otherwise the first 200 characters of ``event_string``.

    Args:
        event_string_column: Raw-event column name.  This is always an internal
            schema constant, never user input, so it is safe to interpolate.

    Returns:
        ``(description_sql, is_excerpt_sql)``.  The flag evaluates to ``1``
        when the description is the 200-character fallback (no non-empty
        ``Descr`` field) and ``0`` when it came from an explicit field.
    """
    column = event_string_column
    marker = _quote_sql_string("Descr=")
    pattern = _quote_sql_string(DESCRIPTION_VALUE_PATTERN)
    fallback = f"substring({column}, 1, 200)"
    raw = f"if(position({column}, {marker}) > 0, extract({column}, {pattern}), '')"
    cleaned = _clean_description_value(raw)
    description = f"if(length({cleaned}) > 0, {cleaned}, {fallback})"
    is_excerpt = f"length({cleaned}) = 0"
    return description, is_excerpt


#: Backwards-compatible value-only template; ``{es}`` stands for the raw-event
#: column.  New code should call :func:`build_description_expressions` so the
#: description and its excerpt flag stay consistent.
DESCRIPTION_EXPR = build_description_expressions("{es}")[0]


def _clean_optional_text(value: Any) -> Optional[str]:
    """Return ``value`` as stripped text, or ``None`` when it is blank.

    Args:
        value: Raw filter value from the GUI, a preset or a test.

    Returns:
        Stripped string, or ``None`` for blank/``None`` input.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_min_duration_ms(value: Any) -> Optional[int]:
    """Normalize a minimum-duration filter to a bounded ``int``.

    Accepts an ``int`` or an integer-like string; booleans are rejected because
    ``True`` would otherwise silently mean one millisecond.  Non-positive
    values and unparsable input yield ``None`` (no filter).  Values above
    :data:`MAX_MIN_DURATION_MS` are clamped so the bound parameter always fits
    the ``UInt64`` placeholder.

    Args:
        value: Raw minimum duration in milliseconds.

    Returns:
        Milliseconds in ``1..MAX_MIN_DURATION_MS``, or ``None``.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        number = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = int(text)
        except ValueError:
            return None
    else:
        return None
    if number <= 0:
        return None
    return min(number, MAX_MIN_DURATION_MS)


@dataclass(frozen=True)
class Filters:
    """User-selected filters shared by every tab query.

    Attributes:
        dataset: Restrict to one dataset (LogT import tag).
        date_from: Inclusive lower bound for the event timestamp.
        date_to: Inclusive upper bound for the event timestamp.
        user: Exact user name.
        event_name: Exact event name.
        search: Case-insensitive substring searched in ``event_string``.
        process_name: ``p:processName`` value; LogT stores it in ``db_name``
            already casefolded and stripped.
        app_id: ``AppID`` value; only present inside ``event_string``, so the
            comparison extracts it from the raw event text.
        min_duration_ms: Minimum ``duration_us`` expressed in milliseconds;
            events without a duration (for example errors) disappear.
    """

    dataset: Optional[str] = None
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None
    user: Optional[str] = None
    event_name: Optional[str] = None
    search: Optional[str] = None
    process_name: Optional[str] = None
    app_id: Optional[str] = None
    min_duration_ms: Optional[int] = None

    def normalized(self) -> "Filters":
        """Return a copy with blank strings converted to ``None``.

        ``process_name`` is casefolded because :mod:`logt_schema` stores
        ``db_name`` normalized that way; ``app_id`` keeps its case and is
        compared case-insensitively in SQL.  ``min_duration_ms`` accepts an
        integer or an integer-like string and is bounded to
        ``1..MAX_MIN_DURATION_MS``.
        """
        process_name = _clean_optional_text(self.process_name)
        return Filters(
            dataset=_clean_optional_text(self.dataset),
            date_from=self.date_from,
            date_to=self.date_to,
            user=_clean_optional_text(self.user),
            event_name=_clean_optional_text(self.event_name),
            search=_clean_optional_text(self.search),
            process_name=process_name.casefold() if process_name else None,
            app_id=_clean_optional_text(self.app_id),
            min_duration_ms=_coerce_min_duration_ms(self.min_duration_ms),
        )


def _where_clause(filters: Filters) -> Tuple[str, Dict[str, Any]]:
    """Build a parameterized WHERE clause for ``filters``.

    Every user-supplied value is bound as a server-side query parameter; no
    value is interpolated into SQL text.

    Args:
        filters: Normalized filters.

    Returns:
        Tuple of ``(sql_fragment, params)`` where ``sql_fragment`` starts with
        ``WHERE`` or is empty.
    """
    clauses: List[str] = []
    params: Dict[str, Any] = {}

    if filters.dataset:
        clauses.append(f"{DATASET_COLUMN} = {{f_dataset:String}}")
        params["f_dataset"] = filters.dataset
    if filters.date_from is not None:
        clauses.append(f"{TIMESTAMP_COLUMN} >= {{f_from:DateTime64(6)}}")
        params["f_from"] = _as_naive_utc(filters.date_from)
    if filters.date_to is not None:
        clauses.append(f"{TIMESTAMP_COLUMN} <= {{f_to:DateTime64(6)}}")
        params["f_to"] = _as_naive_utc(filters.date_to)
    if filters.user:
        clauses.append(f"{USER_COLUMN} = {{f_user:String}}")
        params["f_user"] = filters.user
    if filters.event_name:
        clauses.append(f"{EVENT_NAME_COLUMN} = {{f_event:String}}")
        params["f_event"] = filters.event_name
    if filters.search:
        clauses.append(
            f"positionCaseInsensitiveUTF8({EVENT_STRING_COLUMN}, {{f_search:String}}) > 0"
        )
        params["f_search"] = filters.search

    process_name = _clean_optional_text(filters.process_name)
    if process_name:
        # ``db_name`` is already stripped/casefolded at import time, so the
        # column must not be wrapped in ``lower()`` (that would prevent the
        # index from being used).
        clauses.append(f"{DB_NAME_COLUMN} = {{f_process:String}}")
        params["f_process"] = process_name.casefold()
    app_id = _clean_optional_text(filters.app_id)
    if app_id:
        # ``AppID`` exists only inside the raw event text; the pattern is a
        # constant and only the value is bound.
        pattern = _quote_sql_string(APP_ID_VALUE_PATTERN)
        clauses.append(
            f"lowerUTF8(trimBoth(extract({EVENT_STRING_COLUMN}, {pattern}))) "
            f"= lowerUTF8(trimBoth({{f_appid:String}}))"
        )
        params["f_appid"] = app_id
    min_duration_ms = _coerce_min_duration_ms(filters.min_duration_ms)
    if min_duration_ms is not None:
        clauses.append(f"{DURATION_US_COLUMN} >= {{f_min_duration_us:UInt64}}")
        params["f_min_duration_us"] = min_duration_ms * 1000

    if not clauses:
        return "", params
    return "WHERE " + " AND ".join(clauses), params


def _as_naive_utc(value: datetime) -> datetime:
    """Convert an aware datetime to naive UTC for ClickHouse DateTime64.

    Args:
        value: Datetime possibly carrying a timezone.

    Returns:
        Naive datetime in UTC (ClickHouse columns are declared ``UTC``).
    """
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _error_condition(alias_params: Optional[Dict[str, Any]] = None) -> Tuple[str, Dict[str, Any]]:
    """Return the SQL condition selecting error events.

    Args:
        alias_params: Optional dict to merge parameters into.

    Returns:
        Tuple of ``(condition, params)``.  The condition matches the legacy
        in-memory definition (``EXCP``) plus the extended ``EXCPCNTX`` code.
    """
    params: Dict[str, Any] = dict(alias_params or {})
    parts = []
    for index, name in enumerate(ERROR_EVENT_NAMES):
        key = f"err_{index}"
        parts.append(f"{EVENT_NAME_COLUMN} = {{{key}:String}}")
        params[key] = name
    return "(" + " OR ".join(parts) + ")", params


def _coerce_int(value: Any, default: int = 0) -> int:
    """Best-effort integer conversion for aggregate results."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any) -> Optional[float]:
    """Best-effort float conversion for aggregate results."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_dt(value: Any) -> str:
    """Render a datetime (or string) for the GUI, or an empty string."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def query_overview(
    config: Optional[Dict[str, Any]] = None,
    filters: Optional[Filters] = None,
    connection: Optional[ClickHouseConnection] = None,
    top_n: int = 10,
) -> Dict[str, Any]:
    """Return the Overview tab payload, aggregated server-side.

    Args:
        config: Connection overrides.
        filters: Optional shared filters.
        connection: Optional pre-built connection (used by tests).
        top_n: Number of event types / users to return.

    Returns:
        Dictionary with ``totals``, ``time_range``, ``top_events``,
        ``top_users``, ``errors`` and ``datasets`` keys.

    Raises:
        ClickHouseUnavailable: If ClickHouse cannot be reached.
        AdapterError: On any query failure.
    """
    resolved = connection.config if connection is not None else resolve_config(config)
    conn = connection or ClickHouseConnection(resolved)
    table = schema.qualified_table(resolved["database"], TABLE_EVENTS)
    active = (filters or Filters()).normalized()
    top_n = max(1, min(int(top_n or 10), 100))
    where, params = _where_clause(active)
    error_cond, error_params = _error_condition()
    try:
        totals_row = conn.execute(
            f"""
            SELECT count() AS events,
                   uniqExactIf({USER_COLUMN}, {USER_COLUMN} != '') AS users,
                   uniqExact({EVENT_NAME_COLUMN}) AS event_types,
                   uniqExact({DATASET_COLUMN}) AS datasets,
                   countIf({error_cond}) AS errors,
                   min({TIMESTAMP_COLUMN}) AS first_ts,
                   max({TIMESTAMP_COLUMN}) AS last_ts
            FROM {table}
            {where}
            """,
            {**params, **error_params},
        )
        totals = totals_row[0] if totals_row else (0, 0, 0, 0, 0, None, None)

        top_events = conn.execute(
            f"""
            SELECT {EVENT_NAME_COLUMN} AS name,
                   count() AS cnt,
                   sum({DURATION_US_COLUMN}) AS total_us,
                   max({DURATION_US_COLUMN}) AS max_us
            FROM {table}
            {where}
            GROUP BY {EVENT_NAME_COLUMN}
            ORDER BY cnt DESC
            LIMIT {{top_n:UInt32}}
            """,
            {**params, "top_n": top_n},
        )

        top_users = conn.execute(
            f"""
            SELECT {USER_COLUMN} AS usr,
                   count() AS cnt,
                   countIf({error_cond}) AS errs
            FROM {table}
            {where}
            {"AND" if where else "WHERE"} {USER_COLUMN} != ''
            GROUP BY {USER_COLUMN}
            ORDER BY cnt DESC
            LIMIT {{top_n:UInt32}}
            """,
            {**params, **error_params, "top_n": top_n},
        )

        datasets = conn.execute(
            f"""
            SELECT {DATASET_COLUMN} AS ds, count() AS cnt
            FROM {table}
            {where}
            GROUP BY {DATASET_COLUMN}
            ORDER BY cnt DESC
            LIMIT {{top_n:UInt32}}
            """,
            {**params, "top_n": top_n},
        )
    finally:
        if connection is None:
            conn.close()

    return {
        "totals": {
            "events": _coerce_int(totals[0]),
            "users": _coerce_int(totals[1]),
            "event_types": _coerce_int(totals[2]),
            "datasets": _coerce_int(totals[3]),
            "errors": _coerce_int(totals[4]),
        },
        "time_range": {
            "first": _format_dt(totals[5]),
            "last": _format_dt(totals[6]),
        },
        "top_events": [
            {
                "name": row[0] or "",
                "count": _coerce_int(row[1]),
                "total_ms": round(_coerce_int(row[2]) / 1000.0, 2),
                "max_ms": round(_coerce_int(row[3]) / 1000.0, 2),
            }
            for row in top_events
        ],
        "top_users": [
            {"user": row[0] or "", "count": _coerce_int(row[1]), "errors": _coerce_int(row[2])}
            for row in top_users
        ],
        "datasets": [{"dataset": row[0] or "", "count": _coerce_int(row[1])} for row in datasets],
    }


def count_errors(
    config: Optional[Dict[str, Any]] = None,
    filters: Optional[Filters] = None,
    connection: Optional[ClickHouseConnection] = None,
) -> int:
    """Return the number of error events matching ``filters``.

    Args:
        config: Connection overrides.
        filters: Optional shared filters.
        connection: Optional pre-built connection.

    Returns:
        Error row count, used to drive pagination.
    """
    resolved = connection.config if connection is not None else resolve_config(config)
    conn = connection or ClickHouseConnection(resolved)
    table = schema.qualified_table(resolved["database"], TABLE_EVENTS)
    active = (filters or Filters()).normalized()
    where, params = _where_clause(active)
    error_cond, error_params = _error_condition()
    joiner = "AND" if where else "WHERE"
    try:
        value = conn.execute_scalar(
            f"SELECT count() FROM {table} {where} {joiner} {error_cond}",
            {**params, **error_params},
        )
    finally:
        if connection is None:
            conn.close()
    return _coerce_int(value)


def _coerce_page_argument(value: Any, name: str) -> int:
    """Return ``value`` as ``int`` for a pagination argument.

    Integer-like values (including numeric strings and whole floats) are
    accepted; anything else raises :class:`AdapterError` instead of being
    silently coerced, so callers cannot smuggle in meaningless pagination.
    """
    if isinstance(value, bool):
        raise AdapterError(f"{name} must be an integer, got {value!r}")
    if isinstance(value, float) and not value.is_integer():
        raise AdapterError(f"{name} must be a whole number, got {value!r}")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise AdapterError(f"{name} must be an integer, got {value!r}") from exc


def query_errors(
    config: Optional[Dict[str, Any]] = None,
    filters: Optional[Filters] = None,
    page: int = 0,
    page_size: int = 100,
    connection: Optional[ClickHouseConnection] = None,
) -> List[Dict[str, Any]]:
    """Return one page of error events, ordered newest first.

    Aggregation and pagination happen in ClickHouse: only ``page_size`` rows
    ever cross the wire.

    Args:
        config: Connection overrides.
        filters: Optional shared filters.
        page: Zero-based page index; must be ``>= 0``.
        page_size: Rows per page; must be between ``1`` and
            :data:`MAX_PAGE_SIZE` inclusive (no silent capping).
        connection: Optional pre-built connection.

    Returns:
        List of error dictionaries with ``time``, ``event``, ``db_name``,
        ``user``, ``description`` and ``description_is_excerpt`` keys.

    Raises:
        AdapterError: On query failure, or when ``page`` is negative,
            ``page`` is not an integer, ``page_size`` falls outside
            ``1..MAX_PAGE_SIZE``, or the requested offset exceeds the
            ``UInt64`` range used by the SQL placeholder.
    """
    page = _coerce_page_argument(page, "page")
    page_size = _coerce_page_argument(page_size, "page_size")
    if page < 0:
        raise AdapterError(f"page must be >= 0, got {page}")
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise AdapterError(
            f"page_size must be between 1 and {MAX_PAGE_SIZE}, got {page_size}"
        )
    if page * page_size > _UINT64_MAX:
        raise AdapterError(
            f"page * page_size must fit in UInt64, got offset {page * page_size}"
        )
    resolved = connection.config if connection is not None else resolve_config(config)
    conn = connection or ClickHouseConnection(resolved)
    table = schema.qualified_table(resolved["database"], TABLE_EVENTS)
    active = (filters or Filters()).normalized()
    where, params = _where_clause(active)
    error_cond, error_params = _error_condition()
    joiner = "AND" if where else "WHERE"
    descr, descr_is_excerpt = build_description_expressions()
    try:
        rows = conn.execute(
            f"""
            SELECT {TIMESTAMP_COLUMN} AS ts,
                   {EVENT_NAME_COLUMN} AS ev,
                   {DB_NAME_COLUMN},
                   {USER_COLUMN} AS usr,
                   {descr} AS descr,
                   {descr_is_excerpt} AS descr_is_excerpt
            FROM {table}
            {where} {joiner} {error_cond}
            ORDER BY {TIMESTAMP_COLUMN} DESC, {TIMESTAMP_US_COLUMN} DESC
            LIMIT {{limit:UInt32}} OFFSET {{offset:UInt64}}
            """,
            {**params, **error_params, "limit": page_size, "offset": page * page_size},
        )
    finally:
        if connection is None:
            conn.close()

    results = []
    for row in rows:
        results.append(
            {
                "time": _format_dt(row[0]),
                "event": row[1] or "",
                "db_name": row[2] or "",
                "user": row[3] or "",
                "description": row[4] or "",
                "description_is_excerpt": bool(_coerce_int(row[5])),
            }
        )
    return results


def query_performance(
    config: Optional[Dict[str, Any]] = None,
    filters: Optional[Filters] = None,
    limit: int = 10,
    connection: Optional[ClickHouseConnection] = None,
) -> List[Dict[str, Any]]:
    """Return the slowest individual events, ordered by duration.

    Args:
        config: Connection overrides.
        filters: Optional shared filters.
        limit: Maximum rows to return (capped at :data:`MAX_PAGE_SIZE`).
        connection: Optional pre-built connection.

    Returns:
        List of dictionaries with ``time``, ``event``, ``duration_ms``,
        ``db_name``, ``user`` and ``context``.

    Raises:
        AdapterError: On query failure.
    """
    limit = max(1, min(int(limit or 10), MAX_PAGE_SIZE))
    resolved = connection.config if connection is not None else resolve_config(config)
    conn = connection or ClickHouseConnection(resolved)
    table = schema.qualified_table(resolved["database"], TABLE_EVENTS)
    active = (filters or Filters()).normalized()
    where, params = _where_clause(active)
    try:
        rows = conn.execute(
            f"""
            SELECT {TIMESTAMP_COLUMN} AS ts,
                   {EVENT_NAME_COLUMN} AS ev,
                   {DURATION_US_COLUMN} AS dur_us,
                   {DB_NAME_COLUMN},
                   {USER_COLUMN} AS usr,
                   {CONTEXT_COLUMN} AS ctx
            FROM {table}
            {where}
            ORDER BY {DURATION_US_COLUMN} DESC
            LIMIT {{limit:UInt32}}
            """,
            {**params, "limit": limit},
        )
    finally:
        if connection is None:
            conn.close()

    return [
        {
            "time": _format_dt(row[0]),
            "event": row[1] or "",
            "duration_ms": round(_coerce_int(row[2]) / 1000.0, 3),
            "db_name": row[3] or "",
            "user": row[4] or "",
            "context": row[5] or "",
        }
        for row in rows
    ]


def query_performance_by_event(
    config: Optional[Dict[str, Any]] = None,
    filters: Optional[Filters] = None,
    limit: int = 10,
    connection: Optional[ClickHouseConnection] = None,
) -> List[Dict[str, Any]]:
    """Return per-event performance aggregates computed in ClickHouse.

    Args:
        config: Connection overrides.
        filters: Optional shared filters.
        limit: Maximum event types to return.
        connection: Optional pre-built connection.

    Returns:
        List of dictionaries with ``event``, ``count``, ``sum_ms``, ``avg_ms``,
        ``max_ms`` and ``p95_ms``.
    """
    limit = max(1, min(int(limit or 10), MAX_PAGE_SIZE))
    resolved = connection.config if connection is not None else resolve_config(config)
    conn = connection or ClickHouseConnection(resolved)
    table = schema.qualified_table(resolved["database"], TABLE_EVENTS)
    active = (filters or Filters()).normalized()
    where, params = _where_clause(active)
    try:
        rows = conn.execute(
            f"""
            SELECT {EVENT_NAME_COLUMN} AS ev,
                   count() AS cnt,
                   sum({DURATION_US_COLUMN}) AS total_us,
                   avg({DURATION_US_COLUMN}) AS avg_us,
                   max({DURATION_US_COLUMN}) AS max_us,
                   quantile(0.95)({DURATION_US_COLUMN}) AS p95_us
            FROM {table}
            {where}
            GROUP BY {EVENT_NAME_COLUMN}
            ORDER BY total_us DESC
            LIMIT {{limit:UInt32}}
            """,
            {**params, "limit": limit},
        )
    finally:
        if connection is None:
            conn.close()

    return [
        {
            "event": row[0] or "",
            "count": _coerce_int(row[1]),
            "sum_ms": round(_coerce_int(row[2]) / 1000.0, 2),
            "avg_ms": round((_coerce_float(row[3]) or 0.0) / 1000.0, 3),
            "max_ms": round(_coerce_int(row[4]) / 1000.0, 2),
            "p95_ms": round((_coerce_float(row[5]) or 0.0) / 1000.0, 3),
        }
        for row in rows
    ]


def query_users(
    config: Optional[Dict[str, Any]] = None,
    filters: Optional[Filters] = None,
    connection: Optional[ClickHouseConnection] = None,
) -> List[Dict[str, Any]]:
    """Return per-user statistics aggregated in ClickHouse.

    Args:
        config: Connection overrides.
        filters: Optional shared filters.
        connection: Optional pre-built connection.

    Returns:
        List of dictionaries with ``user``, ``events``, ``errors``,
        ``first_seen`` and ``last_seen``, ordered by event count.
    """
    resolved = connection.config if connection is not None else resolve_config(config)
    conn = connection or ClickHouseConnection(resolved)
    table = schema.qualified_table(resolved["database"], TABLE_EVENTS)
    active = (filters or Filters()).normalized()
    where, params = _where_clause(active)
    error_cond, error_params = _error_condition()
    joiner = "AND" if where else "WHERE"
    try:
        rows = conn.execute(
            f"""
            SELECT {USER_COLUMN} AS usr,
                   count() AS cnt,
                   countIf({error_cond}) AS errs,
                   min({TIMESTAMP_COLUMN}) AS first_ts,
                   max({TIMESTAMP_COLUMN}) AS last_ts
            FROM {table}
            {where} {joiner} {USER_COLUMN} != ''
            GROUP BY {USER_COLUMN}
            ORDER BY cnt DESC
            """,
            {**params, **error_params},
        )
    finally:
        if connection is None:
            conn.close()

    return [
        {
            "user": row[0] or "",
            "events": _coerce_int(row[1]),
            "errors": _coerce_int(row[2]),
            "first_seen": _format_dt(row[3]),
            "last_seen": _format_dt(row[4]),
        }
        for row in rows
    ]


def query_user_events(
    user: str,
    config: Optional[Dict[str, Any]] = None,
    filters: Optional[Filters] = None,
    limit: int = 200,
    connection: Optional[ClickHouseConnection] = None,
) -> List[Dict[str, Any]]:
    """Return recent events for one user.

    Args:
        user: User name to filter by (bound as a query parameter).
        config: Connection overrides.
        filters: Optional shared filters (the ``user`` field is replaced).
        limit: Maximum rows returned.
        connection: Optional pre-built connection.

    Returns:
        List of dictionaries with ``time``, ``event``, ``db_name``,
        ``description``, ``description_is_excerpt`` and ``duration_ms`` keys.
    """
    limit = max(1, min(int(limit or 200), MAX_PAGE_SIZE))
    active = filters or Filters()
    active = Filters(
        dataset=active.dataset,
        date_from=active.date_from,
        date_to=active.date_to,
        user=user,
        event_name=active.event_name,
        search=active.search,
        process_name=active.process_name,
        app_id=active.app_id,
        min_duration_ms=active.min_duration_ms,
    ).normalized()
    resolved = connection.config if connection is not None else resolve_config(config)
    conn = connection or ClickHouseConnection(resolved)
    table = schema.qualified_table(resolved["database"], TABLE_EVENTS)
    where, params = _where_clause(active)
    descr, descr_is_excerpt = build_description_expressions()
    try:
        rows = conn.execute(
            f"""
            SELECT {TIMESTAMP_COLUMN} AS ts,
                   {EVENT_NAME_COLUMN} AS ev,
                   {DB_NAME_COLUMN},
                   {descr} AS descr,
                   {DURATION_US_COLUMN} AS dur_us,
                   {descr_is_excerpt} AS descr_is_excerpt
            FROM {table}
            {where}
            ORDER BY {TIMESTAMP_COLUMN} DESC, {TIMESTAMP_US_COLUMN} DESC
            LIMIT {{limit:UInt32}}
            """,
            {**params, "limit": limit},
        )
    finally:
        if connection is None:
            conn.close()

    return [
        {
            "time": _format_dt(row[0]),
            "event": row[1] or "",
            "db_name": row[2] or "",
            "description": row[3] or "",
            "duration_ms": round(_coerce_int(row[4]) / 1000.0, 3),
            "description_is_excerpt": bool(_coerce_int(row[5])),
        }
        for row in rows
    ]
