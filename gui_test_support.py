"""Shared infrastructure for the Tk GUI tests.

The filename starts with ``gui_`` (not ``test_``), so pytest never collects this
module as a test file.  The GUI test modules import :class:`AppTestBase` and
:class:`FakeAdapter` from here instead of importing one test module from
another (F-11 in the testing plan), which keeps the suite independent of module
import order.

The module owns the single interpreter-wide Tk root, the mocked adapter and the
``pump``/``wait_until`` helpers used for bounded, sleep-free synchronisation.
"""

import os
import shutil
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

import ics_log_analyzer as gui
import presets
from import_progress import ImportProgress, ImportStage
from logt_adapter import Filters, ImportResult, SchemaStatus


# A single interpreter-wide Tk root is shared by every test case.  Creating and
# destroying ``tk.Tk()`` repeatedly inside one process intermittently fails with
# "Can't find a usable init.tcl": Tcl caches its library search path on first
# initialisation, so a second fresh root may lose it.  The probe root created at
# import time is therefore kept and reused rather than destroyed.
_SHARED_ROOT: "tk.Tk | None" = None
_TK_PROBE_FAILED = False


def _shared_root() -> "tk.Tk":
    """Return (creating on first use) the process-wide hidden Tk root."""
    global _SHARED_ROOT, _TK_PROBE_FAILED
    if _SHARED_ROOT is None or not _SHARED_ROOT.winfo_exists():
        try:
            _SHARED_ROOT = tk.Tk()
            _SHARED_ROOT.withdraw()
        except Exception:  # pragma: no cover - headless CI without Tk
            _TK_PROBE_FAILED = True
            raise
    return _SHARED_ROOT


def _tk_available() -> bool:
    """Return whether the shared Tk root can be created here."""
    try:
        _shared_root()
    except Exception:
        return False
    return True


TK_AVAILABLE = _tk_available()


class FakeAdapter:
    """Stand-in for :mod:`logt_adapter`'s query surface.

    Records every call so tests can assert that the GUI queried the backend
    rather than aggregating in Python.
    """

    def __init__(self):
        self.calls = []
        self.overview_payload = {
            "totals": {"events": 42, "users": 3, "event_types": 4, "errors": 2, "datasets": 1},
            "time_range": {"first": "2026-01-01 00:00:00", "last": "2026-01-01 01:00:00"},
            "top_events": [
                {"name": "CALL", "count": 20, "total_ms": 12.5, "max_ms": 3.0},
                {"name": "EXCP", "count": 2, "total_ms": 0.5, "max_ms": 0.3},
            ],
            "top_users": [{"user": "Иванов", "count": 30, "errors": 1}],
        }
        self.error_rows = [
            {
                "time": "2026-01-01 00:10:00",
                "event": "EXCP",
                "db_name": "db1",
                "user": "Иванов",
                "description": "Ошибка SQL",
            }
        ]
        self.error_total = 1
        self.slowest = [
            {
                "time": "2026-01-01 00:20:00",
                "event": "DBMSSQL",
                "duration_ms": 8.0,
                "db_name": "db1",
                "user": "Иванов",
                "context": "ctx",
            }
        ]
        self.aggregates = [
            {
                "event": "DBMSSQL",
                "count": 1,
                "sum_ms": 8.0,
                "avg_ms": 8.0,
                "max_ms": 8.0,
                "p95_ms": 8.0,
            }
        ]
        self.user_rows = [
            {
                "user": "Иванов",
                "events": 30,
                "errors": 1,
                "first_seen": "2026-01-01 00:00:00",
                "last_seen": "2026-01-01 01:00:00",
            }
        ]
        self.user_events_rows = [
            {
                "time": "2026-01-01 00:05:00",
                "event": "CALL",
                "db_name": "db1",
                "duration_ms": 1.5,
                "description": "вызов",
            }
        ]
        self.import_result = ImportResult(
            success=True,
            exit_code=0,
            stdout="",
            stderr="",
            duration=1.0,
            rows_loaded=42,
            table_counts={"operations": 3, "events": 42, "event_stats": 2, "calls": 1},
        )
        self.reachable = True
        self.schema = SchemaStatus(ready=True, created=[], message="Schema OK")
        # P1-04: recorded typed progress wiring.
        self.import_progress_events = []
        self.import_count_lines = []
        self.import_has_on_progress = []

    # -- methods mirroring ClickHouseClient --------------------------------
    def test(self):
        self.calls.append(("test",))
        return self.reachable

    # The GUI patches module-level adapter functions, so the fake needs the
    # module-level names too.
    def check_clickhouse_connection(self, config=None):
        return self.test()

    def ensure_schema(self, config=None):
        self.calls.append(("ensure_schema",))
        return self.schema

    def validate_schema(self, config=None):
        self.calls.append(("validate_schema",))
        return self.schema

    def overview(self, filters, top_n=10):
        self.calls.append(("overview", filters))
        return self.overview_payload

    def query_overview(self, config, filters, top_n=10):
        return self.overview(filters, top_n=top_n)

    def errors(self, filters, page, page_size):
        self.calls.append(("errors", filters, page, page_size))
        return {
            "rows": self.error_rows,
            "total": self.error_total,
            "page": page,
            "page_size": page_size,
        }

    def count_errors(self, config=None, filters=None):
        self.calls.append(("count_errors", filters))
        return self.error_total

    def query_errors(self, config, filters, page=0, page_size=100):
        self.calls.append(("query_errors", filters, page, page_size))
        return self.error_rows

    def performance(self, filters, limit):
        self.calls.append(("performance", filters, limit))
        return {"slowest": self.slowest, "by_event": self.aggregates}

    def query_performance(self, config, filters, limit=20):
        self.calls.append(("performance", filters, limit))
        return self.slowest

    def query_performance_by_event(self, config, filters, limit=20):
        return self.aggregates

    def users(self, filters):
        self.calls.append(("users", filters))
        return self.user_rows

    def query_users(self, config, filters):
        self.calls.append(("users", filters))
        return self.user_rows

    def user_events(self, user, filters, limit=200):
        self.calls.append(("user_events", user, filters))
        return self.user_events_rows

    def query_user_events(self, user, config, filters, limit=200):
        self.calls.append(("user_events", user, filters))
        return self.user_events_rows

    def import_logs(
        self,
        log_dir,
        dataset,
        cancel_event,
        progress,
        on_progress=None,
        count_lines_for_progress=True,
        **kwargs,
    ):
        self.calls.append(("import_logs", log_dir, dataset))
        self.import_has_on_progress.append(on_progress is not None)
        self.import_count_lines.append(count_lines_for_progress)
        if progress is not None:
            progress("stub progress")
        if on_progress is not None:
            # A short typed sequence mirroring the real adapter: a determinate
            # scan phase, a processing phase and the terminal 100 % snapshot.
            events = (
                ImportProgress(ImportStage.SCANNING, "Сканирование логов…", 0, 2),
                ImportProgress(ImportStage.PROCESSING, "Обработка логов…", 1, 2),
                ImportProgress(ImportStage.DONE, "Готово", 2, 2),
            )
            for event in events:
                self.import_progress_events.append(event)
                on_progress(event)
        return self.import_result

    def check_logt_runtime(self, logt_root=None):
        """Preflight stand-in: tests must not depend on vendor/logt in CI."""
        return "python"

    def run_logt_import(self, log_dir, config, progress_callback=None, cancel_event=None,
                        dataset_name="default", **kwargs):
        return self.import_logs(
            log_dir,
            dataset_name,
            cancel_event,
            progress_callback or (lambda message: None),
            on_progress=kwargs.get("on_progress"),
            count_lines_for_progress=kwargs.get("count_lines_for_progress", True),
        )

    def kinds(self):
        """Return the sequence of recorded method names."""
        return [call[0] for call in self.calls]


class AppTestBase(unittest.TestCase):
    """Build a real Tk application with every adapter call mocked."""

    def setUp(self):
        if not TK_AVAILABLE:  # pragma: no cover - headless CI without Tk
            self.skipTest("Tk is not available in this environment")
        self.root = tk.Toplevel(_shared_root())
        self.root.withdraw()

        # Presets must never touch the real project config: point the store at a
        # throwaway file for the whole test and restore the environment after.
        presets_dir = tempfile.mkdtemp(prefix="gui-presets-")
        self.presets_path = Path(presets_dir) / "presets.json"
        previous_presets_path = os.environ.get(presets.PRESETS_ENV_VAR)
        os.environ[presets.PRESETS_ENV_VAR] = str(self.presets_path)
        self.addCleanup(shutil.rmtree, presets_dir, True)

        def restore_presets_env() -> None:
            if previous_presets_path is None:
                os.environ.pop(presets.PRESETS_ENV_VAR, None)
            else:
                os.environ[presets.PRESETS_ENV_VAR] = previous_presets_path

        self.addCleanup(restore_presets_env)

        self.fake = FakeAdapter()
        self.patchers = [
            mock.patch.object(gui.logt_adapter, name, getattr(self.fake, name))
            for name in (
                "check_clickhouse_connection",
                "ensure_schema",
                "validate_schema",
                "query_overview",
                "query_errors",
                "count_errors",
                "query_performance",
                "query_performance_by_event",
                "query_users",
                "query_user_events",
                "run_logt_import",
                "check_logt_runtime",
            )
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        for name in ("showinfo", "showerror", "showwarning", "askyesno"):
            kwargs = {"return_value": True} if name == "askyesno" else {}
            patcher = mock.patch.object(gui.messagebox, name, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.app = gui.LogAnalyzerApp(self.root)
        self.app.clickhouse_config = {
            "host": "127.0.0.1",
            "port": 9000,
            "database": "tracelog",
            "user": "default",
            "password": "secret",
        }
        self.app.dataset = "test_ds"

    def tearDown(self):
        try:
            self.app.shutdown()
        except Exception:
            pass
        self.pump(0.05)
        try:
            self.root.update()
        except tk.TclError:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        # Drop the widget tree on the main thread so Tk variables are not
        # finalised later by a worker thread's garbage collection.
        self.app = None

    def pump(self, seconds=1.0):
        """Run the Tk event loop so queued results are applied."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.root.update()
            time.sleep(0.01)

    def wait_until(self, predicate, timeout=10.0):
        """Pump the event loop until ``predicate`` is true or the timeout ends."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.root.update()
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def enter_clickhouse_mode(self):
        """Switch the app into the ClickHouse-backed state."""
        self.app.source = "clickhouse"
        self.app.filters = Filters(dataset="test_ds").normalized()
