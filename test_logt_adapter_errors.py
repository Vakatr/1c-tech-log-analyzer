"""Failure-path tests for :mod:`logt_adapter`.

Stage 1 of the testing plan: every test here is offline and deterministic.  No
ClickHouse server is contacted — connections are provided as small fakes or the
client is injected directly — and filesystem behaviour is exercised either in a
``tempfile`` directory or with mocked ``Path``/``os``/``json`` operations.

The focus is the branches that decide what the user sees when something goes
wrong: whether a failure is reported as "ClickHouse is unavailable" or as a
query error, whether a password-bearing file survives a failed write, and
whether an import is reported as successful when ClickHouse row counts cannot
be read.
"""

import datetime as dt
import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import logt_adapter
import logt_schema

SECRET = "sup3r-s3cret-pw"

CONFIG = {
    "host": "127.0.0.1",
    "port": 9000,
    "database": "tracelog",
    "user": "default",
    "password": SECRET,
    "secure": False,
}


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class NetworkError(Exception):
    """Stand-in for ``clickhouse_driver.errors.NetworkError``."""


class SocketTimeoutError(Exception):
    """Stand-in for ``clickhouse_driver.errors.SocketTimeoutError``."""


class ServerException(Exception):
    """Stand-in for ``clickhouse_driver.errors.ServerException``."""


class _FakeClient:
    """Minimal native client: returns scripted rows or raises a scripted error."""

    def __init__(self, rows=None, error=None):
        self.rows = [] if rows is None else rows
        self.error = error
        self.calls = []
        self.disconnected = False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if self.error is not None:
            raise self.error
        return self.rows

    def disconnect(self):
        self.disconnected = True


class _FakeConnection:
    """Connection usable by :func:`logt_adapter.count_tables`.

    Resolves the table name from the rendered ``database.table`` reference so
    the same fake can both answer counts and fail for selected tables.
    """

    def __init__(self, config=None, values=None, failing=()):
        self.config = dict(config or CONFIG)
        self.values = dict(values or {})
        self.failing = set(failing)
        self.calls = []
        self.closed = False

    def close(self):
        """Record that the caller closed a connection it did not own."""
        self.closed = True

    def execute_scalar(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), dict(params or {})))
        for table in self.failing:
            if f".`{table}`" in sql:
                raise logt_adapter.AdapterError(f"cannot read {table}")
        for table, value in self.values.items():
            if f".`{table}`" in sql:
                return value
        return 0


class _Pipe:
    """File-like stub yielding scripted lines, then EOF."""

    def __init__(self, lines):
        self._lines = list(lines)

    def readline(self):
        return self._lines.pop(0) if self._lines else ""

    def read(self):
        return ""

    def close(self):
        pass


def _popen_factory(exit_code=0, stdout=(), stderr=()):
    """Build a ``subprocess.Popen`` stand-in class for the given script.

    Args:
        exit_code: Exit code reported by the fake child.
        stdout: Lines the child writes to stdout.
        stderr: Lines the child writes to stderr.

    Returns:
        A class accepted by ``mock.patch.object(logt_adapter.subprocess, "Popen")``.
    """

    class FakeProcess:
        def __init__(self, argv, **kwargs):
            self.argv = argv
            self.stdout = _Pipe(stdout)
            self.stderr = _Pipe(stderr)
            self.stdin = None
            self.pid = 1
            self.returncode = exit_code

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def terminate(self):
            pass

        def kill(self):
            pass

    return FakeProcess


def _popen_running_then_exited(wait_hook):
    """Build a fake child that is running for the first poll, then exits.

    ``wait_hook`` runs in ``wait`` *after* the child has set its exit code,
    modelling a cancel event that is flipped only once the process is gone.

    Args:
        wait_hook: Callback invoked after the fake child exits.

    Returns:
        A ``subprocess.Popen`` stand-in class.
    """

    class FakeProcess(_RunningThenExitedPopen):
        def wait(self, timeout=None):
            result = super().wait(timeout)
            wait_hook()
            return result

    return FakeProcess


class _RunningThenExitedPopen:
    """Fake child that is still running for the first poll, then exits.

    Used to prove that a cancel event flipped *after* the child exited still
    yields ``cancelled=True`` rather than a successful import.
    """

    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.stdout = _Pipe(())
        self.stderr = _Pipe(())
        self.stdin = None
        self.pid = 1
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def terminate(self):
        raise AssertionError("the child exited on its own; it must not be terminated")

    def kill(self):
        raise AssertionError("the child exited on its own; it must not be killed")


class _CancelEvent:
    """Event with ``is_set``/``set`` and an inspectable flag."""

    def __init__(self):
        self.flag = False

    def set(self):
        """Mark the import as cancelled."""
        self.flag = True

    def is_set(self):
        return self.flag


def _run_import(counts=None, counts_error=None, exit_code=0, cancel_after_exit=False):
    """Run :func:`logt_adapter.run_logt_import` against a fake child.

    Args:
        counts: Value returned by the stubbed ``count_tables``; defaults to a
            single event so the import would succeed.
        counts_error: When given, ``count_tables`` raises this exception.
        exit_code: Exit code of the fake child.
        cancel_after_exit: When true, the process exits normally and only then
            sets the cancel event (an import cancelled too late).

    Returns:
        The :class:`logt_adapter.ImportResult`.
    """
    event = _CancelEvent()
    popen = (
        _popen_running_then_exited(event.set)
        if cancel_after_exit
        else _popen_factory(exit_code=exit_code)
    )
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "fake_main.py"
        source.write_text("print('fake LogT')\n", encoding="utf-8")
        work = Path(tmp) / "work"
        work.mkdir()

        counter = (
            mock.patch.object(logt_adapter, "count_tables", side_effect=counts_error)
            if counts_error is not None
            else mock.patch.object(
                logt_adapter,
                "count_tables",
                return_value={"events": 1} if counts is None else counts,
            )
        )
        with mock.patch.dict(
            os.environ, {logt_adapter.ENV_WORK_DIR: str(work)}, clear=False
        ):
            os.environ.pop(logt_adapter.ENV_PASSWORD, None)
            with mock.patch.object(logt_adapter, "load_local_config", return_value={}):
                with mock.patch.object(
                    logt_adapter, "_resolve_logt_entrypoint", return_value=source
                ):
                    with mock.patch.object(
                        logt_adapter.subprocess, "Popen", popen
                    ):
                        with counter:
                            return logt_adapter.run_logt_import(
                                ".",
                                CONFIG,
                                dataset_name="ds",
                                cancel_event=event,
                            )


# ---------------------------------------------------------------------------
# _is_connection_error
# ---------------------------------------------------------------------------


class TestIsConnectionError(unittest.TestCase):
    """Transport failures must be distinguished from server-side rejections."""

    def test_transport_exception_types_are_connection_errors(self):
        """Driver transport exceptions are classified as unreachable."""
        for exc in (
            NetworkError("boom"),
            SocketTimeoutError("timed out"),
            ConnectionRefusedError("refused"),
            TimeoutError("timed out"),
        ):
            with self.subTest(exc=type(exc).__name__):
                self.assertTrue(logt_adapter._is_connection_error(exc))

    def test_gaierror_like_oserror_subclass_is_connection_error(self):
        """A name-lookup failure (``gaierror``) is a network error."""

        class gaierror(OSError):
            """Stand-in for ``socket.gaierror``."""

        self.assertTrue(logt_adapter._is_connection_error(gaierror("no host")))

    def test_network_flavoured_oserror_subclass_is_connection_error(self):
        """Reset/abort subclasses are network-flavoured and must match."""
        self.assertTrue(
            logt_adapter._is_connection_error(ConnectionResetError("reset"))
        )
        self.assertTrue(
            logt_adapter._is_connection_error(ConnectionAbortedError("aborted"))
        )

    def test_plain_oserror_is_not_a_connection_error(self):
        """A bare ``OSError`` says nothing about the network."""
        self.assertFalse(logt_adapter._is_connection_error(OSError("x")))

    def test_plain_oserror_with_a_network_errno_is_a_connection_error(self):
        """Drivers may raise plain ``OSError`` with a transport errno."""
        self.assertTrue(
            logt_adapter._is_connection_error(
                OSError(errno.ECONNREFUSED, "connection refused")
            )
        )
        self.assertFalse(
            logt_adapter._is_connection_error(
                OSError(errno.ENOENT, "missing file")
            )
        )

    def test_local_filesystem_oserror_subclasses_are_not_connection_errors(self):
        """ENOENT/EACCES are local failures, not network failures.

        The docstring promises that only network-flavoured ``OSError``
        subclasses count, so a missing or unreadable local file must not be
        reported as "ClickHouse is unavailable".
        """
        for exc in (FileNotFoundError("missing"), PermissionError("denied")):
            with self.subTest(exc=type(exc).__name__):
                self.assertFalse(logt_adapter._is_connection_error(exc))

    def test_cause_chain_is_followed(self):
        """A wrapped transport error still classifies the outer exception."""
        outer = logt_adapter.AdapterError("query failed")
        outer.__cause__ = NetworkError("socket closed")
        self.assertTrue(logt_adapter._is_connection_error(outer))

    def test_context_chain_is_followed(self):
        """``__context__`` is consulted when there is no ``__cause__``."""
        outer = RuntimeError("wrapper")
        outer.__context__ = SocketTimeoutError("timed out")
        self.assertTrue(logt_adapter._is_connection_error(outer))

    def test_cyclic_cause_chain_terminates(self):
        """A cause cycle must not hang and must not invent a failure."""
        first = RuntimeError("first")
        second = RuntimeError("second")
        first.__cause__ = second
        second.__cause__ = first
        self.assertFalse(logt_adapter._is_connection_error(first))


# ---------------------------------------------------------------------------
# ClickHouseConnection
# ---------------------------------------------------------------------------


class TestClickHouseConnection(unittest.TestCase):
    """``execute`` must translate driver errors and drop a dead client."""

    def _connection(self, **client_kwargs):
        """Build a connection with an injected fake client."""
        connection = logt_adapter.ClickHouseConnection(CONFIG)
        connection._client = _FakeClient(**client_kwargs)
        return connection

    def test_execute_delegates_to_client(self):
        """A successful statement returns the client's rows unchanged."""
        rows = [("events", "MergeTree")]
        connection = self._connection(rows=rows)
        result = connection.execute("SELECT 1", {"p": 1})
        self.assertEqual(result, rows)
        self.assertEqual(connection._client.calls, [("SELECT 1", {"p": 1})])
        self.assertIsNotNone(connection._client)

    def test_execute_defaults_params_to_empty_dict(self):
        """``params=None`` is forwarded as an empty binding map."""
        connection = self._connection(rows=[])
        connection.execute("SELECT 1")
        self.assertEqual(connection._client.calls, [("SELECT 1", {})])

    def test_transport_error_maps_to_unavailable_and_resets_client(self):
        """A network failure becomes ``ClickHouseUnavailable``, with no client."""
        error = NetworkError(f"cannot connect with password {SECRET}")
        connection = self._connection(error=error)
        with self.assertRaises(logt_adapter.ClickHouseUnavailable) as caught:
            connection.execute("SELECT 1")
        message = str(caught.exception)
        self.assertIn("Cannot connect to ClickHouse", message)
        self.assertIn("127.0.0.1:9000", message)
        self.assertNotIn(SECRET, message)
        self.assertIsNone(connection._client)

    def test_server_error_maps_to_adapter_error_and_redacts_password(self):
        """A server rejection is a query error, not an availability failure."""
        error = ServerException(f"Code: 62. Syntax error near password={SECRET}")
        connection = self._connection(error=error)
        with self.assertRaises(logt_adapter.AdapterError) as caught:
            connection.execute("SELECT broken")
        self.assertNotIsInstance(caught.exception, logt_adapter.ClickHouseUnavailable)
        message = str(caught.exception)
        self.assertIn("ClickHouse query failed", message)
        self.assertIn("Code: 62", message)
        self.assertNotIn(SECRET, message)
        self.assertIsNone(connection._client)

    def test_execute_scalar_returns_none_for_empty_rows(self):
        """No rows means ``None``, not an ``IndexError``."""
        connection = self._connection(rows=[])
        self.assertIsNone(connection.execute_scalar("SELECT max(x)"))

    def test_execute_scalar_returns_first_column(self):
        """Only the first column of the first row is returned."""
        connection = self._connection(rows=[(42, "ignored"), (7, "ignored")])
        self.assertEqual(connection.execute_scalar("SELECT count()"), 42)

    def test_ping_returns_false_on_adapter_error(self):
        """``ping`` never raises, even for a server-side failure."""
        connection = logt_adapter.ClickHouseConnection(CONFIG)
        with mock.patch.object(
            connection, "execute", side_effect=logt_adapter.AdapterError("nope")
        ):
            self.assertFalse(connection.ping())

    def test_ping_returns_true_on_success(self):
        """A successful trivial query reports connectivity."""
        connection = logt_adapter.ClickHouseConnection(CONFIG)
        with mock.patch.object(connection, "execute", return_value=[(1,)]):
            self.assertTrue(connection.ping())

    def test_table_engines_maps_rows(self):
        """``table_engines`` turns ``(name, engine)`` rows into a dict."""
        connection = self._connection(
            rows=[("events", "MergeTree"), ("calls", "Log")]
        )
        self.assertEqual(
            connection.table_engines(),
            {"events": "MergeTree", "calls": "Log"},
        )


# ---------------------------------------------------------------------------
# load_local_config
# ---------------------------------------------------------------------------


class TestLoadLocalConfig(unittest.TestCase):
    """A broken local config yields ``{}`` and never leaks the password."""

    def test_missing_file_returns_empty_dict(self):
        """An absent file is simply "not configured"."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "absent.json"
            self.assertEqual(logt_adapter.load_local_config(path), {})

    def test_malformed_json_returns_empty_dict(self):
        """Invalid JSON is ignored, not raised."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{not json", encoding="utf-8")
            # Point the default path at a *different* missing file: the explicit
            # ``path`` argument is what the caller asked about.
            with mock.patch.object(
                logt_adapter, "LOCAL_CONFIG_PATH", Path(tmp) / "absent.json"
            ):
                self.assertEqual(logt_adapter.load_local_config(path), {})

    def test_malformed_default_config_returns_empty_dict(self):
        """A broken config at the real path returns ``{}`` instead of recursing.

        ``load_local_config`` logs through :func:`redact`, which calls
        ``load_local_config`` again.  Without a recursion guard a malformed
        default file re-enters the failing path until ``RecursionError``.
        The recursion limit is lowered so the test stays fast; it is always
        restored.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{not json", encoding="utf-8")
            with mock.patch.object(logt_adapter, "LOCAL_CONFIG_PATH", path):
                previous_limit = sys.getrecursionlimit()
                sys.setrecursionlimit(200)
                result = None
                recursed = False
                try:
                    with mock.patch.object(logt_adapter.logger, "warning"):
                        try:
                            result = logt_adapter.load_local_config()
                        except RecursionError:
                            recursed = True
                finally:
                    sys.setrecursionlimit(previous_limit)
                if recursed:
                    self.fail(
                        "load_local_config() recursed instead of returning {}: "
                        "redact() calls load_local_config() again for the same "
                        "malformed default config"
                    )
        self.assertEqual(result, {})

    def test_json_array_and_scalar_return_empty_dict(self):
        """Only an object is accepted as configuration."""
        with tempfile.TemporaryDirectory() as tmp:
            for index, payload in enumerate(("[1, 2, 3]", "42", "true")):
                path = Path(tmp) / f"shape-{index}.json"
                path.write_text(payload, encoding="utf-8")
                with self.subTest(payload=payload):
                    self.assertEqual(logt_adapter.load_local_config(path), {})

    def test_unreadable_file_returns_empty_dict_without_password_in_warning(self):
        """An ``OSError`` is logged, redacted, and downgraded to ``{}``."""
        with tempfile.TemporaryDirectory() as tmp:
            secret_path = Path(tmp) / "secret.json"
            secret_path.write_text("{}", encoding="utf-8")
            missing = Path(tmp) / "missing.json"
            with mock.patch.object(logt_adapter, "LOCAL_CONFIG_PATH", missing):
                with mock.patch.dict(
                    os.environ, {logt_adapter.ENV_PASSWORD: SECRET}, clear=False
                ):
                    with mock.patch(
                        "builtins.open",
                        side_effect=PermissionError(f"denied while reading {SECRET}"),
                    ):
                        with self.assertLogs(
                            logt_adapter.logger, level="WARNING"
                        ) as captured:
                            result = logt_adapter.load_local_config(secret_path)
        self.assertEqual(result, {})
        warning = "\n".join(captured.output)
        self.assertNotIn(SECRET, warning)
        self.assertIn("***", warning)


# ---------------------------------------------------------------------------
# check_clickhouse_connection
# ---------------------------------------------------------------------------


class TestCheckClickHouseConnection(unittest.TestCase):
    """Preflight connectivity checks never raise."""

    def test_invalid_database_name_returns_false(self):
        """An invalid identifier is rejected before any client is created."""
        with mock.patch.object(logt_adapter, "load_local_config", return_value={}):
            with mock.patch.object(
                logt_adapter.ClickHouseConnection, "ping"
            ) as ping:
                result = logt_adapter.check_clickhouse_connection(
                    {"database": "bad name; DROP TABLE events"}
                )
        self.assertFalse(result)
        ping.assert_not_called()


# ---------------------------------------------------------------------------
# resolve_work_dir
# ---------------------------------------------------------------------------


class TestResolveWorkDir(unittest.TestCase):
    """Staging directory resolution probes writability instead of assuming it."""

    def test_environment_override_is_honoured(self):
        """``LOGT_WORK_DIR`` wins over the system temp directory."""
        with tempfile.TemporaryDirectory() as tmp:
            preferred = Path(tmp) / "preferred"
            with mock.patch.dict(
                os.environ, {logt_adapter.ENV_WORK_DIR: str(preferred)}, clear=False
            ):
                result = logt_adapter.resolve_work_dir()
            self.assertEqual(result, preferred)
            self.assertTrue(result.is_dir())

    def test_falls_back_when_first_candidate_cannot_be_created(self):
        """An unusable override falls through to the system temp directory."""
        with tempfile.TemporaryDirectory() as tmp:
            blocked = Path(tmp) / "blocked"
            blocked.write_text("not a directory", encoding="utf-8")
            fallback = Path(tmp) / "system-temp"
            fallback.mkdir()
            with mock.patch.dict(
                os.environ, {logt_adapter.ENV_WORK_DIR: str(blocked)}, clear=False
            ):
                with mock.patch.object(
                    logt_adapter.tempfile, "gettempdir", return_value=str(fallback)
                ):
                    result = logt_adapter.resolve_work_dir()
            self.assertEqual(result, fallback)
            self.assertTrue(result.is_dir())

    def test_all_candidates_unwritable_raises_listing_paths(self):
        """When nothing is writable the message names every path that was tried."""
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first-file"
            second = Path(tmp) / "second-file"
            third = Path(tmp) / "third-file"
            for path in (first, second, third):
                path.write_text("not a directory", encoding="utf-8")
            with mock.patch.dict(
                os.environ, {logt_adapter.ENV_WORK_DIR: str(first)}, clear=False
            ):
                with mock.patch.object(
                    logt_adapter.tempfile, "gettempdir", return_value=str(second)
                ):
                    with mock.patch.object(logt_adapter, "LOGT_CONFIG_DIR", third):
                        with self.assertRaises(logt_adapter.AdapterError) as caught:
                            logt_adapter.resolve_work_dir()
        message = str(caught.exception)
        self.assertIn("LOGT_WORK_DIR", message)
        self.assertIn("Tried:", message)
        for path in (first, second, third):
            self.assertIn(str(path), message)


# ---------------------------------------------------------------------------
# _write_private_config
# ---------------------------------------------------------------------------


class TestWritePrivateConfig(unittest.TestCase):
    """A failed write must never leave the password-bearing file on disk."""

    def _payload(self):
        return {"clickhouse": {"password": SECRET}}

    def test_os_open_failure_raises_and_leaves_no_file(self):
        """A failure while creating the file is reported as an ``AdapterError``."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "stage"
            with mock.patch.object(logt_adapter, "load_local_config", return_value={}):
                with mock.patch.object(
                    logt_adapter.os, "open", side_effect=OSError("access denied")
                ):
                    with self.assertRaises(logt_adapter.AdapterError) as caught:
                        logt_adapter._write_private_config(directory, self._payload())
            self.assertIn("Cannot write LogT config", str(caught.exception))
            self.assertFalse((directory / "config.json").exists())

    def test_json_dump_failure_removes_file_and_raises(self):
        """A partial write is deleted before the error escapes."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "stage"
            with mock.patch.object(logt_adapter, "load_local_config", return_value={}):
                with mock.patch.object(
                    logt_adapter.json, "dump", side_effect=OSError("disk full")
                ):
                    with self.assertRaises(logt_adapter.AdapterError):
                        logt_adapter._write_private_config(directory, self._payload())
            self.assertFalse((directory / "config.json").exists())

    def test_non_serializable_payload_cleans_up(self):
        """A ``TypeError`` from ``json.dump`` also removes the secret file."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "stage"
            payload = {"clickhouse": {"password": SECRET}, "bad": object()}
            with mock.patch.object(logt_adapter, "load_local_config", return_value={}):
                with self.assertRaises(TypeError):
                    logt_adapter._write_private_config(directory, payload)
            self.assertFalse((directory / "config.json").exists())


# ---------------------------------------------------------------------------
# count_tables
# ---------------------------------------------------------------------------


class TestCountTables(unittest.TestCase):
    """Per-table counts are diagnostics: one failure must not abort the rest."""

    def test_counts_every_table_without_dataset(self):
        """Without a dataset every table is counted with no predicate."""
        connection = _FakeConnection(values={"events": 5})
        counts = logt_adapter.count_tables(connection=connection)
        self.assertEqual(set(counts), set(logt_schema.TABLE_SPECS))
        self.assertEqual(counts["events"], 5)
        self.assertEqual(len(connection.calls), len(logt_schema.TABLE_SPECS))
        for sql, params in connection.calls:
            self.assertEqual(params, {})
            self.assertNotIn("WHERE", sql)
        self.assertFalse(connection.closed)

    def test_counts_with_dataset_binds_the_filter(self):
        """A dataset restricts every count through a bound parameter."""
        connection = _FakeConnection(values={"events": 2})
        counts = logt_adapter.count_tables(connection=connection, dataset="ds")
        self.assertEqual(counts["events"], 2)
        for sql, params in connection.calls:
            self.assertIn("{dataset:String}", sql)
            self.assertEqual(params, {"dataset": "ds"})
            self.assertIn("WHERE dataset =", sql)

    def test_failing_table_is_reported_as_minus_one(self):
        """An unreadable table yields ``-1``; the others keep their counts."""
        connection = _FakeConnection(values={"events": 4}, failing={"calls"})
        counts = logt_adapter.count_tables(connection=connection)
        self.assertEqual(counts["calls"], -1)
        self.assertEqual(counts["events"], 4)
        self.assertEqual(counts["operations"], 0)

    def test_none_result_is_zero(self):
        """A ``NULL``/``None`` count is reported as 0, not ``None``."""
        connection = _FakeConnection(values={"events": None})
        counts = logt_adapter.count_tables(connection=connection)
        self.assertEqual(counts["events"], 0)


# ---------------------------------------------------------------------------
# run_logt_import failure modes
# ---------------------------------------------------------------------------


class TestRunLogtImportFailures(unittest.TestCase):
    """LogT exit code 0 is not success: ClickHouse counts decide."""

    def test_count_tables_raising_adapter_error_fails_with_diagnostics(self):
        """A counting failure is surfaced instead of being treated as success."""
        result = _run_import(
            counts_error=logt_adapter.AdapterError("cannot count rows")
        )
        self.assertFalse(result.success)
        self.assertEqual(result.rows_loaded, 0)
        self.assertIn("cannot count rows", result.stderr)
        self.assertIn("nothing was imported", result.error)

    def test_missing_events_table_sums_other_readable_counts(self):
        """Without an ``events`` table the other counts are the import result."""
        result = _run_import(
            counts={"operations": 3, "calls": 2, "event_stats": -1}
        )
        self.assertTrue(result.success)
        self.assertEqual(result.rows_loaded, 5)
        self.assertEqual(result.table_counts["operations"], 3)

    def test_all_counts_unreadable_is_a_failure(self):
        """When no count can be read the import cannot be verified."""
        result = _run_import(
            counts={"events": -1, "operations": -1, "calls": -1, "event_stats": -1}
        )
        self.assertFalse(result.success)
        self.assertEqual(result.rows_loaded, 0)
        self.assertIn("could not be read", result.error)
        self.assertIn("cannot be verified", result.error)

    def test_cancel_after_process_exit_is_reported_as_cancelled(self):
        """A cancel event set after the child exited still marks cancellation."""
        result = _run_import(cancel_after_exit=True)
        self.assertTrue(result.cancelled)
        self.assertFalse(result.success)
        self.assertIn("cancelled", result.error)
        self.assertEqual(result.rows_loaded, None)


# ---------------------------------------------------------------------------
# Small contracts
# ---------------------------------------------------------------------------


class TestSmallHelpers(unittest.TestCase):
    """Conversion, formatting and filter helpers accept garbage safely."""

    def test_page_argument_accepts_numeric_string_and_whole_float(self):
        """Numeric strings and whole floats are valid pagination values."""
        self.assertEqual(logt_adapter._coerce_page_argument("3", "page"), 3)
        self.assertEqual(logt_adapter._coerce_page_argument(2.0, "page"), 2)
        self.assertEqual(logt_adapter._coerce_page_argument(0, "page"), 0)

    def test_page_argument_rejects_bool(self):
        """``True`` must not silently become page 1."""
        for value in (True, False):
            with self.subTest(value=value):
                with self.assertRaises(logt_adapter.AdapterError) as caught:
                    logt_adapter._coerce_page_argument(value, "page")
                self.assertIn("must be an integer", str(caught.exception))

    def test_page_argument_rejects_fractional_float(self):
        """A fractional page number is meaningless and must be rejected."""
        with self.assertRaises(logt_adapter.AdapterError) as caught:
            logt_adapter._coerce_page_argument(1.5, "page_size")
        self.assertIn("whole number", str(caught.exception))

    def test_page_argument_rejects_none_and_garbage(self):
        """``None`` and non-numeric text are rejected, not defaulted."""
        for value in (None, "abc", object()):
            with self.subTest(value=value):
                with self.assertRaises(logt_adapter.AdapterError):
                    logt_adapter._coerce_page_argument(value, "page")

    def test_coerce_int_and_float_use_defaults_for_garbage(self):
        """Aggregate helpers degrade to a default instead of raising."""
        self.assertEqual(logt_adapter._coerce_int("garbage"), 0)
        self.assertEqual(logt_adapter._coerce_int(None, default=7), 7)
        self.assertEqual(logt_adapter._coerce_int("12"), 12)
        self.assertIsNone(logt_adapter._coerce_float("garbage"))
        self.assertIsNone(logt_adapter._coerce_float(None))
        self.assertEqual(logt_adapter._coerce_float("1.25"), 1.25)

    def test_format_dt(self):
        """Datetimes are rendered for the GUI; ``None`` becomes an empty string."""
        self.assertEqual(logt_adapter._format_dt(None), "")
        moment = dt.datetime(2026, 1, 2, 3, 4, 5)
        self.assertEqual(logt_adapter._format_dt(moment), "2026-01-02 03:04:05")
        self.assertEqual(logt_adapter._format_dt("already text"), "already text")

    def test_where_clause_with_date_to_and_all_filters(self):
        """Every filter is bound and ``date_to`` becomes an upper bound."""
        filters = logt_adapter.Filters(
            dataset="ds",
            date_from=dt.datetime(2026, 1, 1, 0, 0, 0),
            date_to=dt.datetime(2026, 1, 2, 0, 0, 0),
            user="user1",
            event_name="EXCP",
            search="timeout",
        )
        clause, params = logt_adapter._where_clause(filters)
        self.assertTrue(clause.startswith("WHERE "))
        self.assertEqual(
            set(params),
            {"f_dataset", "f_from", "f_to", "f_user", "f_event", "f_search"},
        )
        self.assertIn("{f_to:DateTime64(6)}", clause)
        self.assertIn(f"{logt_adapter.TIMESTAMP_COLUMN} <= {{f_to:DateTime64(6)}}", clause)
        self.assertEqual(params["f_to"], dt.datetime(2026, 1, 2, 0, 0, 0))
        self.assertEqual(params["f_dataset"], "ds")
        self.assertEqual(params["f_search"], "timeout")
        for value in ("ds", "user1", "EXCP", "timeout"):
            self.assertNotIn(value, clause)


if __name__ == "__main__":
    unittest.main()
