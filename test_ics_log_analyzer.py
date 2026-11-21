"""GUI smoke tests for ``ics_log_analyzer``.

These tests use a mocked adapter, so no ClickHouse server is required.  They
verify the review requirements that can be checked without I/O:

* the four data tabs read from ClickHouse (never from ``self.events``) once an
  import has succeeded;
* every database call is dispatched to a worker thread, and results are
  delivered through the queue and applied on the main thread;
* load controls are disabled during an import and re-enabled afterwards;
* a failed or unreachable backend produces a clear error dialog, not a crash.
"""

import csv
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

import pytest

import export
import ics_log_analyzer as gui
import logt_adapter
from gui_test_support import AppTestBase, FakeAdapter, TK_AVAILABLE
from import_progress import ImportProgress, ImportStage
from logt_adapter import Filters, ImportResult, SchemaStatus

# The shared infrastructure lives in ``gui_test_support`` so this module and
# ``test_gui_buttons`` no longer depend on each other's import order.  The names
# are re-exported here for backwards compatibility with existing importers.
__all__ = ("AppTestBase", "FakeAdapter", "TK_AVAILABLE")

pytestmark = pytest.mark.gui


class TestTabsQueryClickHouse(AppTestBase):
    """BLOCKER 2: the four tabs must read from ClickHouse, not ``self.events``."""

    def test_overview_queries_clickhouse_not_memory(self):
        self.enter_clickhouse_mode()
        self.app.events = [{"name": "SHOULD_NOT_BE_USED"}]
        self.app.refresh_overview()
        self.assertTrue(self.wait_until(lambda: self.app.event_tree.get_children()))
        self.assertIn("overview", self.fake.kinds())
        self.assertEqual(len(self.app.event_tree.get_children()), 2)
        self.assertEqual(len(self.app.user_tree.get_children()), 1)

    def test_overview_applies_server_aggregates(self):
        self.enter_clickhouse_mode()
        self.app.refresh_overview()
        self.assertTrue(self.wait_until(lambda: self.app.event_tree.get_children()))
        values = self.app.event_tree.item(self.app.event_tree.get_children()[0], "values")
        self.assertEqual(values[0], "CALL")
        self.assertEqual(values[1], "20")

    def test_errors_queries_clickhouse(self):
        self.enter_clickhouse_mode()
        self.app.events = [{"name": "SHOULD_NOT_BE_USED"}]
        self.app.refresh_errors()
        self.assertTrue(self.wait_until(lambda: self.app.error_tree.get_children()))
        self.assertEqual(len(self.app.error_tree.get_children()), 1)
        row = self.app.error_tree.item(self.app.error_tree.get_children()[0], "values")
        self.assertEqual(row[1], "EXCP")
        self.assertEqual(row[4], "Ошибка SQL")

    def test_performance_queries_clickhouse(self):
        self.enter_clickhouse_mode()
        self.app.refresh_performance()
        self.assertTrue(self.wait_until(lambda: self.app.performance_tree.get_children()))
        self.assertEqual(len(self.app.performance_tree.get_children()), 1)
        self.assertEqual(len(self.app.performance_agg_tree.get_children()), 1)

    def test_users_queries_clickhouse(self):
        self.enter_clickhouse_mode()
        self.app.refresh_users()
        self.assertTrue(self.wait_until(lambda: self.app.user_stats_tree.get_children()))
        self.assertEqual(len(self.app.user_stats_tree.get_children()), 1)

    def test_user_selection_queries_clickhouse(self):
        self.enter_clickhouse_mode()
        self.app.refresh_users()
        self.assertTrue(self.wait_until(lambda: self.app.user_stats_tree.get_children()))
        first = self.app.user_stats_tree.get_children()[0]
        self.app.user_stats_tree.selection_set(first)
        self.pump(0.3)
        self.assertTrue(self.wait_until(lambda: self.app.user_events_tree.get_children()))
        self.assertEqual(len(self.app.user_events_tree.get_children()), 1)

    def test_filters_are_passed_to_backend(self):
        self.enter_clickhouse_mode()
        self.app.filter_user_entry.insert(0, "Петров")
        self.app.apply_filters()
        self.assertTrue(self.wait_until(lambda: "overview" in self.fake.kinds()))
        filters = [call[1] for call in self.fake.calls if call[0] == "overview"][0]
        self.assertEqual(filters.user, "Петров")

    def test_memory_mode_does_not_query_clickhouse(self):
        self.app.source = "memory"
        self.app.events = [{"name": "EXCP", "descr": "boom", "user": "u", "timestamp": "x"}]
        self.app.refresh_overview()
        self.pump(0.2)
        self.assertNotIn("overview", self.fake.kinds())


class TestImportFlow(AppTestBase):
    """Import control wiring and the switch to ClickHouse as source of truth."""

    def test_import_disables_controls_and_reenables(self):
        self.app.file_path = "C:/logs"
        self.app.dataset_var.set("test_ds")
        self.app.load_via_logt()
        # Controls are disabled synchronously, before the worker finishes.
        self.assertEqual(str(self.app.load_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.logt_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.cancel_button.cget("state")), "normal")
        self.assertTrue(self.wait_until(lambda: not self.app.busy, timeout=15))
        self.app.pump(0.2) if hasattr(self.app, "pump") else None
        self.assertEqual(str(self.app.load_button.cget("state")), "normal")
        self.assertEqual(str(self.app.logt_button.cget("state")), "normal")
        self.assertEqual(str(self.app.cancel_button.cget("state")), "disabled")

    def test_import_switches_source_to_clickhouse_and_refreshes(self):
        self.app.file_path = "C:/logs"
        self.app.dataset_var.set("test_ds")
        self.app.load_via_logt()
        self.assertTrue(self.wait_until(lambda: self.app.source == "clickhouse", timeout=15))
        self.pump(0.5)
        self.assertIn("import_logs", self.fake.kinds())
        self.assertIn("overview", self.fake.kinds())
        self.assertIn("users", self.fake.kinds())

    def test_import_scopes_filters_to_dataset(self):
        self.app.file_path = "C:/logs"
        self.app.dataset_var.set("test_ds")
        self.app.load_via_logt()
        self.assertTrue(self.wait_until(lambda: self.app.source == "clickhouse", timeout=15))
        self.assertEqual(self.app.filters.dataset, "test_ds")

    def test_failed_import_reports_error_and_keeps_memory_mode(self):
        self.fake.import_result = ImportResult(
            success=False,
            exit_code=1,
            error="LogT failed: boom",
            stderr="Traceback (most recent call last):\nboom\n",
            duration=2.5,
        )
        self.app.file_path = "C:/logs"
        self.app.dataset_var.set("test_ds")
        self.app.load_via_logt()
        self.assertTrue(
            self.wait_until(lambda: self.app._import_error_dialog is not None, timeout=15)
        )
        self.pump(0.3)
        self.assertEqual(self.app.source, "memory")
        gui.messagebox.showerror.assert_not_called()
        dialog = self.app._import_error_dialog
        self.assertTrue(dialog.is_open)
        self.assertIn("boom", dialog.details)
        self.assertIn("Импорт LogT", dialog.details)
        self.assertIn("test_ds", dialog.details)
        self.assertIn("Код возврата LogT: 1", dialog.details)
        self.assertIn("boom", dialog.text.get("1.0", tk.END))
        self.assertIn("Ошибка импорта", self.app.status_bar.cget("text"))

    def test_cancelled_import_is_reported(self):
        self.fake.import_result = ImportResult(
            success=False, exit_code=1, cancelled=True, error="cancelled"
        )
        self.app.file_path = "C:/logs"
        self.app.dataset_var.set("test_ds")
        self.app.load_via_logt()
        self.assertTrue(self.wait_until(lambda: not self.app.busy, timeout=15))
        self.pump(0.3)
        self.assertEqual(self.app.source, "memory")

    def test_import_without_folder_warns(self):
        self.app.file_path = ""
        self.app.load_via_logt()
        gui.messagebox.showwarning.assert_called()

    def test_import_does_not_block_main_thread(self):
        """The call must return immediately, not wait for the import."""
        self.app.file_path = "C:/logs"
        self.app.dataset_var.set("test_ds")
        started = time.time()
        self.app.load_via_logt()
        elapsed = time.time() - started
        self.assertLess(elapsed, 1.0)
        self.assertTrue(self.app.busy)

    def test_import_ensures_schema_before_loading(self):
        """BUG-004: an empty database must be prepared automatically."""
        self.app.file_path = "C:/logs"
        self.app.dataset_var.set("test_ds")
        self.app.load_via_logt()
        self.assertTrue(self.wait_until(lambda: not self.app.busy, timeout=15))
        kinds = self.fake.kinds()
        self.assertIn("ensure_schema", kinds)
        self.assertIn("import_logs", kinds)
        self.assertLess(kinds.index("ensure_schema"), kinds.index("import_logs"))

    def test_import_aborts_when_schema_is_not_ready(self):
        self.fake.schema = SchemaStatus(ready=False, created=[], message="broken schema")
        self.app.file_path = "C:/logs"
        self.app.dataset_var.set("test_ds")
        self.app.load_via_logt()
        self.assertTrue(
            self.wait_until(lambda: self.app._import_error_dialog is not None, timeout=15)
        )
        self.pump(0.3)
        self.assertIn("ensure_schema", self.fake.kinds())
        self.assertNotIn("import_logs", self.fake.kinds())
        self.assertEqual(self.app.source, "memory")
        gui.messagebox.showerror.assert_not_called()
        dialog = self.app._import_error_dialog
        self.assertIn("Подготовка схемы ClickHouse", dialog.details)
        self.assertIn("broken schema", dialog.details)
        self.assertIn("Подготовка схемы ClickHouse", self.app.status_bar.cget("text"))

    def test_schema_stage_exception_reports_stage_without_password(self):
        """An exception before LogT starts is diagnosable and leaks no secret."""
        secret = "S3cret-Passw0rd"
        self.app.clickhouse_config["password"] = secret

        def failing_schema(config=None):
            raise logt_adapter.AdapterError(
                f"ClickHouse unavailable (password={secret})"
            )

        with mock.patch.object(gui.logt_adapter, "ensure_schema", failing_schema):
            self.app.file_path = "C:/logs"
            self.app.dataset_var.set("test_ds")
            self.app.load_via_logt()
            self.assertTrue(
                self.wait_until(
                    lambda: self.app._import_error_dialog is not None, timeout=15
                )
            )
        self.pump(0.3)
        dialog = self.app._import_error_dialog
        self.assertIn("Подготовка схемы ClickHouse", dialog.details)
        self.assertIn("ClickHouse unavailable", dialog.details)
        self.assertNotIn(secret, dialog.details)
        self.assertNotIn(secret, dialog.text.get("1.0", tk.END))
        self.assertNotIn(secret, self.app.summary_label.cget("text"))
        self.assertNotIn(secret, self.app.status_bar.cget("text"))
        self.assertNotIn("import_logs", self.fake.kinds())
        self.assertEqual(self.app.source, "memory")
        gui.messagebox.showerror.assert_not_called()


class TestImportProgress(AppTestBase):
    """P1-04: determinate import progress with an indeterminate fallback."""

    def start_import(self):
        """Run one import through the fake adapter."""
        self.app.file_path = "C:/logs"
        self.app.dataset_var.set("test_ds")
        self.app.load_via_logt()

    def test_progress_widgets_have_idle_defaults(self):
        self.assertEqual(float(self.app.progress.cget("maximum")), 100.0)
        self.assertEqual(float(self.app.progress.cget("value")), 0.0)
        self.assertEqual(str(self.app.progress.cget("mode")), "determinate")
        self.assertEqual(self.app.import_stage_label.cget("text"), "Этап: —")
        self.assertEqual(self.app.import_count_label.cget("text"), "Файлов: —")
        self.assertEqual(self.app.import_percent_label.cget("text"), "")
        self.assertTrue(self.app.count_lines_var.get())

    def test_typed_progress_is_determinate_with_counts(self):
        self.app.busy = True
        self.app._on_import_progress(
            ImportProgress(ImportStage.SCANNING, "Сканирование логов", 1, 4)
        )
        self.assertEqual(str(self.app.progress.cget("mode")), "determinate")
        self.assertEqual(float(self.app.progress.cget("maximum")), 4.0)
        self.assertEqual(float(self.app.progress.cget("value")), 1.0)
        self.assertEqual(self.app.import_count_label.cget("text"), "Файлов: 1/4")
        self.assertEqual(self.app.import_percent_label.cget("text"), "25 %")
        self.assertIn("Сканирование", self.app.import_stage_label.cget("text"))

    def test_unknown_percent_falls_back_to_indeterminate(self):
        self.app.busy = True
        self.app._on_import_progress(
            ImportProgress(ImportStage.PROCESSING, "Обработка логов")
        )
        self.assertEqual(str(self.app.progress.cget("mode")), "indeterminate")
        self.assertEqual(self.app.import_count_label.cget("text"), "Файлов: —")
        self.assertEqual(self.app.import_percent_label.cget("text"), "")
        self.assertIn("Обработка логов", self.app.import_stage_label.cget("text"))

    def test_known_percent_without_total_uses_100_scale(self):
        self.app.busy = True
        self.app._on_import_progress(
            ImportProgress(ImportStage.PROCESSING, "Обработка", percent=42.0)
        )
        self.assertEqual(str(self.app.progress.cget("mode")), "determinate")
        self.assertEqual(float(self.app.progress.cget("maximum")), 100.0)
        self.assertEqual(float(self.app.progress.cget("value")), 42.0)
        self.assertEqual(self.app.import_percent_label.cget("text"), "42 %")
        self.assertEqual(self.app.import_count_label.cget("text"), "Файлов: —")

    def test_status_has_no_info_prefix_or_line_breaks(self):
        self.app.busy = True
        self.app._on_import_progress(
            ImportProgress(ImportStage.PROCESSING, "[INFO] Обработка\r\nлогов")
        )
        status = self.app.status_bar.cget("text")
        self.assertEqual(status, "Обработка логов")
        self.assertNotIn("[INFO]", status)
        self.assertNotIn("\r", status)
        self.assertNotIn("\n", status)

    def test_success_marks_done_at_100_percent(self):
        self.start_import()
        self.assertTrue(self.wait_until(lambda: not self.app.busy, timeout=15))
        self.pump(0.3)
        self.assertEqual(str(self.app.progress.cget("mode")), "determinate")
        self.assertEqual(float(self.app.progress.cget("maximum")), 100.0)
        self.assertEqual(float(self.app.progress.cget("value")), 100.0)
        self.assertEqual(self.app.import_percent_label.cget("text"), "100 %")
        self.assertIn("Готово", self.app.import_stage_label.cget("text"))

    def test_repeat_import_starts_from_zero(self):
        self.start_import()
        self.assertTrue(self.wait_until(lambda: not self.app.busy, timeout=15))
        self.pump(0.3)
        self.assertEqual(float(self.app.progress.cget("value")), 100.0)

        self.start_import()
        # The Tk thread has not drained the new events yet, so the bar still
        # shows the reset state of the second run.
        self.assertEqual(float(self.app.progress.cget("value")), 0.0)
        self.assertEqual(self.app.import_stage_label.cget("text"), "Этап: —")
        self.assertEqual(self.app.import_count_label.cget("text"), "Файлов: —")
        self.assertTrue(self.wait_until(lambda: not self.app.busy, timeout=15))

    def test_schema_progress_is_posted_before_ensure_schema(self):
        events = []
        original_post = self.app.dispatcher.post

        def recording_post(kind, payload=None):
            if kind == "import_progress" and isinstance(payload, ImportProgress):
                events.append(("progress", payload.stage))
            original_post(kind, payload)

        self.app.dispatcher.post = recording_post
        real_ensure = self.fake.ensure_schema

        def ensure(*args, **kwargs):
            events.append(("ensure_schema", None))
            return real_ensure(*args, **kwargs)

        with mock.patch.object(gui.logt_adapter, "ensure_schema", ensure):
            self.start_import()
            self.assertTrue(self.wait_until(lambda: not self.app.busy, timeout=15))

        self.assertIn(("progress", ImportStage.SCHEMA), events)
        self.assertLess(
            events.index(("progress", ImportStage.SCHEMA)),
            events.index(("ensure_schema", None)),
        )

    def test_count_lines_checkbox_is_forwarded(self):
        self.app.count_lines_var.set(False)
        self.start_import()
        self.assertTrue(self.wait_until(lambda: not self.app.busy, timeout=15))
        self.assertEqual(self.fake.import_count_lines[-1], False)
        self.assertTrue(self.fake.import_has_on_progress[-1])

    def test_count_lines_defaults_to_true(self):
        self.start_import()
        self.assertTrue(self.wait_until(lambda: not self.app.busy, timeout=15))
        self.assertEqual(self.fake.import_count_lines[-1], True)

    def test_cancel_disables_button_and_changes_stage(self):
        self.app.cancel_event = threading.Event()
        self.app._set_busy(True, action="import")
        try:
            self.app.cancel_import()
            self.assertTrue(self.app.cancel_event.is_set())
            self.assertEqual(str(self.app.cancel_button.cget("state")), "disabled")
            self.assertEqual(
                self.app.import_stage_label.cget("text"), "Этап: Остановка LogT…"
            )
            self.assertIn("Отмена запрошена", self.app.status_bar.cget("text"))
        finally:
            self.app._set_busy(False)

    def test_cancelled_result_marks_cancelled(self):
        self.app._set_busy(True, action="import")
        self.app._on_import_done(
            ImportResult(success=False, exit_code=1, cancelled=True, error="cancelled")
        )
        self.assertEqual(
            self.app.import_stage_label.cget("text"), "Этап: Импорт отменён"
        )
        self.assertIn("отмен", self.app.status_bar.cget("text").lower())
        self.assertEqual(str(self.app.progress.cget("mode")), "determinate")
        gui.messagebox.showerror.assert_not_called()

    def test_cancel_with_silent_adapter_stops_progress_and_reenables_controls(self):
        """P1-06: cancelling an adapter that emits no progress still settles."""
        started = threading.Event()

        def silent_import(
            log_dir,
            config,
            progress_callback=None,
            cancel_event=None,
            **kwargs,
        ):
            started.set()
            while not cancel_event.is_set():
                time.sleep(0.01)
            return ImportResult(
                success=False, exit_code=1, cancelled=True, error="cancelled"
            )

        try:
            with mock.patch.object(
                gui.logt_adapter, "run_logt_import", silent_import
            ):
                self.start_import()
                self.assertTrue(
                    self.wait_until(
                        lambda: started.is_set() and self.app.busy, timeout=10
                    )
                )
                self.app.cancel_button.invoke()
                self.assertTrue(self.app.cancel_event.is_set())
                self.assertEqual(str(self.app.cancel_button.cget("state")), "disabled")
                self.assertEqual(
                    self.app.import_stage_label.cget("text"),
                    "Этап: Остановка LogT…",
                )
                self.assertIn(
                    "Отмена запрошена", self.app.status_bar.cget("text")
                )
                self.assertTrue(
                    self.wait_until(lambda: not self.app.busy, timeout=10)
                )
        finally:
            if self.app.cancel_event is not None:
                self.app.cancel_event.set()

        self.pump(0.3)
        self.assertEqual(
            self.app.import_stage_label.cget("text"), "Этап: Импорт отменён"
        )
        self.assertIn("отмен", self.app.status_bar.cget("text").lower())
        self.assertEqual(str(self.app.cancel_button.cget("state")), "disabled")
        self.assertEqual(str(self.app.load_button.cget("state")), "normal")
        # The indeterminate animation is stopped in favour of the terminal state.
        self.assertEqual(str(self.app.progress.cget("mode")), "determinate")
        self.assertIsNone(self.app.cancel_event)
        self.assertEqual(self.app.source, "memory")
        gui.messagebox.showerror.assert_not_called()

    def test_retry_after_failed_import_starts_from_zero_and_succeeds(self):
        """P1-06: a failed run does not poison the next one."""
        self.fake.import_result = ImportResult(
            success=False,
            exit_code=1,
            error="LogT failed: boom",
            stderr="boom\n",
            duration=1.0,
        )
        self.start_import()
        self.assertTrue(
            self.wait_until(
                lambda: self.app._import_error_dialog is not None
                and not self.app.busy,
                timeout=15,
            )
        )
        self.pump(0.2)
        self.assertEqual(self.app.source, "memory")
        self.app._close_import_error_dialog()

        self.fake.import_result = ImportResult(
            success=True,
            exit_code=0,
            duration=1.0,
            rows_loaded=42,
            table_counts={"events": 42},
        )
        self.start_import()
        # The second run resets the widgets before the worker reports anything.
        self.assertEqual(float(self.app.progress.cget("value")), 0.0)
        self.assertEqual(self.app.import_stage_label.cget("text"), "Этап: —")
        self.assertEqual(self.app.import_count_label.cget("text"), "Файлов: —")
        self.assertTrue(self.wait_until(lambda: not self.app.busy, timeout=15))
        self.pump(0.3)
        self.assertEqual(self.app.source, "clickhouse")
        self.assertEqual(float(self.app.progress.cget("value")), 100.0)
        self.assertIn("Готово", self.app.import_stage_label.cget("text"))

    def test_failed_result_marks_failed_stage(self):
        self.app._set_busy(True, action="import")
        self.app.last_import_progress = ImportProgress(
            ImportStage.PROCESSING, "Обработка логов"
        )
        self.app._on_import_done(
            ImportResult(success=False, exit_code=1, error="LogT failed")
        )
        stage = self.app.import_stage_label.cget("text")
        self.assertIn("Обработка логов", stage)
        self.assertIn("ошибка", stage)
        self.assertIsNotNone(self.app._import_error_dialog)
        self.assertIn("LogT failed", self.app._import_error_dialog.details)
        gui.messagebox.showerror.assert_not_called()

    def test_late_progress_is_ignored_after_done(self):
        self.start_import()
        self.assertTrue(self.wait_until(lambda: not self.app.busy, timeout=15))
        self.pump(0.3)
        self.app._on_import_progress(
            ImportProgress(ImportStage.SCANNING, "Позднее событие", 1, 2)
        )
        self.assertEqual(float(self.app.progress.cget("value")), 100.0)
        self.assertIn("Готово", self.app.import_stage_label.cget("text"))

    def test_string_payload_is_cleaned_and_shown(self):
        self.app.busy = True
        self.app._on_import_progress("  [INFO] загрузка\r\nданных  ")
        self.assertEqual(self.app.status_bar.cget("text"), "загрузка данных")

    def test_coalescing_delivers_latest_progress_before_done(self):
        dispatcher = gui.ResultDispatcher(self.root)
        delivered = []
        dispatcher.post(
            "import_progress", ImportProgress(ImportStage.SCANNING, "scan", 0, 3)
        )
        dispatcher.post(
            "import_progress", ImportProgress(ImportStage.PROCESSING, "work", 1, 3)
        )
        dispatcher.post(
            "import_progress", ImportProgress(ImportStage.INSERTING, "insert", 2, 3)
        )
        dispatcher.post("import_done", "done")
        dispatcher.drain(
            lambda kind, payload: delivered.append((kind, payload)),
            coalesce_kinds={"import_progress"},
        )
        self.assertEqual([kind for kind, _ in delivered], ["import_progress", "import_done"])
        self.assertIs(delivered[0][1].stage, ImportStage.INSERTING)
        self.assertEqual(delivered[1][1], "done")

    def test_trailing_progress_is_delivered_after_drain(self):
        dispatcher = gui.ResultDispatcher(self.root)
        delivered = []
        dispatcher.post("import_progress", ImportProgress(ImportStage.SCANNING, "a"))
        dispatcher.post("import_progress", ImportProgress(ImportStage.PROCESSING, "b"))
        dispatcher.drain(
            lambda kind, payload: delivered.append((kind, payload)),
            coalesce_kinds={"import_progress"},
        )
        self.assertEqual(len(delivered), 1)
        self.assertIs(delivered[0][1].stage, ImportStage.PROCESSING)


class TestImportPreflight(AppTestBase):
    """P1-00c / BUG-006: LogT runtime is checked before the import starts."""

    def test_missing_logt_runtime_blocks_import(self):
        """A frozen build without Python/vendor/logt must fail before workers."""
        self.app.file_path = "C:/logs"
        self.app.dataset_var.set("test_ds")
        failure = logt_adapter.AdapterError(
            "LogT cannot be started: set LOGT_ROOT and LOGT_PYTHON."
        )
        with mock.patch.object(
            gui.logt_adapter, "check_logt_runtime", side_effect=failure
        ):
            self.app.load_via_logt()

        gui.messagebox.showerror.assert_called_once()
        title, message = gui.messagebox.showerror.call_args[0][:2]
        self.assertEqual(title, "LogT")
        self.assertIn("LOGT_ROOT", message)
        self.assertFalse(self.app.busy)
        self.assertIsNone(self.app.import_thread)
        self.assertIsNone(self.app.cancel_event)
        self.assertNotIn("import_logs", self.fake.kinds())


class TestConnectionAndSchema(AppTestBase):
    """Connectivity, schema validation and the guarded reset path."""

    def test_connection_check_runs_off_main_thread(self):
        self.app.check_connection_async()
        self.assertTrue(self.wait_until(lambda: "test" in self.fake.kinds()))

    def test_unreachable_backend_shows_dialog(self):
        self.fake.reachable = False
        self.app.check_connection_async()
        self.assertTrue(self.wait_until(lambda: "test" in self.fake.kinds()))
        self.pump(0.3)
        gui.messagebox.showerror.assert_called()

    def test_schema_validation_is_read_only(self):
        self.app.validate_schema_async()
        self.assertTrue(self.wait_until(lambda: self.app.schema_status is not None))
        self.assertIn("validate_schema", self.fake.kinds())
        self.assertNotIn("ensure_schema", self.fake.kinds())

    def test_reset_requires_confirmation(self):
        with mock.patch.object(gui.messagebox, "askyesno", return_value=False) as ask:
            self.app.reset_schema_dialog()
            ask.assert_called_once()
        self.assertNotIn("reset_schema", self.fake.kinds())

    def test_reset_requires_typed_confirmation(self):
        with mock.patch.object(gui.messagebox, "askyesno", return_value=True):
            with mock.patch.object(gui.simpledialog, "askstring", return_value="nope"):
                with mock.patch.object(gui.logt_adapter, "reset_schema") as reset:
                    self.app.reset_schema_dialog()
                    reset.assert_not_called()

    def test_reset_runs_only_after_double_confirmation(self):
        with mock.patch.object(gui.messagebox, "askyesno", return_value=True):
            with mock.patch.object(gui.simpledialog, "askstring", return_value="RESET"):
                with mock.patch.object(
                    gui.logt_adapter,
                    "reset_schema",
                    return_value=SchemaStatus(ready=True, created=[], message="reset"),
                ) as reset:
                    self.app.reset_schema_dialog()
                    self.assertTrue(self.wait_until(lambda: reset.called))
                    reset.assert_called_once()
                    self.assertTrue(reset.call_args.kwargs.get("confirm"))


class TestWorkerThreadContract(AppTestBase):
    """Queries must never run on the Tk main thread."""

    def test_queries_run_on_worker_thread(self):
        main_thread = threading.current_thread().ident
        seen = []

        def slow_overview(config, filters, top_n=10):
            seen.append(threading.current_thread().ident)
            return self.fake.overview_payload

        with mock.patch.object(gui.logt_adapter, "query_overview", slow_overview):
            self.enter_clickhouse_mode()
            self.app.refresh_overview()
            self.assertTrue(self.wait_until(lambda: seen))
        self.assertNotEqual(seen[0], main_thread)

    def test_worker_exception_is_reported(self):
        def boom(config, filters, top_n=10):
            raise logt_adapter.ClickHouseUnavailable("ClickHouse is down")

        with mock.patch.object(gui.logt_adapter, "query_overview", boom):
            self.enter_clickhouse_mode()
            self.app.refresh_overview()
            self.pump(0.6)
        gui.messagebox.showerror.assert_called()

    def test_queue_is_used_for_delivery(self):
        self.assertIsInstance(self.app.dispatcher.queue, type(gui.queue.Queue()))


class TestMemoryAggregationCache(AppTestBase):
    """PERF-004/PERF-003: memory aggregation is shared and runs off-thread."""

    def test_aggregates_are_reused_across_tabs(self):
        self.app.source = "memory"
        self.app.events = [
            {"name": "CALL", "user": "u1", "timestamp": "01.01.2024 10:00:00.000"},
            {"name": "EXCP", "user": "u1", "timestamp": "01.01.2024 10:00:01.000"},
        ]
        with mock.patch.object(
            gui, "aggregate_by_event", wraps=gui.aggregate_by_event
        ) as aggregate:
            self.app.refresh_overview()
            self.app.refresh_errors()
            self.app.refresh_performance()
            self.app.refresh_users()
            # Rendering happens on the main thread after the worker replies.
            self.assertTrue(
                self.wait_until(
                    lambda: self.app.user_stats_tree.get_children()
                    and self.app.event_tree.get_children()
                    and self.app.error_tree.get_children()
                    and self.app.performance_tree.get_children()
                )
            )
            self.assertEqual(aggregate.call_count, 1)
        values = self.app.user_stats_tree.item(
            self.app.user_stats_tree.get_children()[0], "values"
        )
        self.assertEqual(values[0], "u1")
        self.assertEqual(values[1], "2")
        self.assertEqual(values[2], "1")

    def test_cache_is_invalidated_when_events_are_replaced(self):
        self.app.source = "memory"
        self.app.events = [{"name": "CALL"}]
        with mock.patch.object(
            gui, "aggregate_by_event", wraps=gui.aggregate_by_event
        ) as aggregate:
            self.app.refresh_overview()
            self.assertTrue(self.wait_until(lambda: self.app.event_tree.get_children()))
            self.app.events = [{"name": "EXCP"}]
            self.app.refresh_overview()
            self.assertTrue(self.wait_until(lambda: self.app.event_tree.get_children()))
            self.assertEqual(aggregate.call_count, 2)

    def test_memory_aggregation_runs_on_worker_thread(self):
        """PERF-003: aggregation must not block the Tk main thread."""
        main_thread = threading.current_thread().ident
        seen = []

        def recording_aggregate(events):
            seen.append(threading.current_thread().ident)
            return {"CALL": len(events)}

        self.app.source = "memory"
        self.app.events = [{"name": "CALL"}]
        with mock.patch.object(gui, "aggregate_by_event", recording_aggregate):
            self.app.refresh_overview()
            self.assertTrue(self.wait_until(lambda: seen))
        self.assertNotEqual(seen[0], main_thread)

    def test_memory_errors_are_paginated(self):
        self.app.source = "memory"
        self.app.error_page_size = 100
        self.app.events = [
            {"name": "EXCP", "user": "u1", "descr": f"error {index}"}
            for index in range(150)
        ]
        self.app.refresh_errors()
        self.assertTrue(self.wait_until(lambda: self.app.error_tree.get_children()))
        self.assertEqual(len(self.app.error_tree.get_children()), 100)
        self.assertEqual(self.app.error_total, 150)
        self.app.error_next_page()
        self.assertTrue(self.wait_until(lambda: self.app.error_page == 1))
        self.assertTrue(self.wait_until(lambda: len(self.app.error_tree.get_children()) == 50))
        first = self.app.error_tree.item(self.app.error_tree.get_children()[0], "values")
        self.assertEqual(first[4], "error 100")


class TestReferenceWarning(AppTestBase):
    """BUG-003: a broken registry must be visible in the GUI."""

    def test_registry_error_is_shown_in_reference_tab(self):
        with mock.patch.object(gui, "load_registry", return_value={"events": []}):
            with mock.patch.object(gui, "registry_error", return_value="events.json: boom"):
                self.app._populate_reference_tree()
        text = self.app.registry_warning.cget("text")
        self.assertIn("не загружен", text)
        self.assertIn("events.json: boom", text)

    def test_no_warning_when_registry_is_healthy(self):
        with mock.patch.object(gui, "load_registry", return_value={"events": []}):
            with mock.patch.object(gui, "registry_error", return_value=None):
                self.app._populate_reference_tree()
        self.assertEqual(self.app.registry_warning.cget("text"), "")


class TestExport(AppTestBase):
    """CSV export writes exactly the displayed rows."""

    def test_errors_csv_matches_displayed_rows(self):
        self.enter_clickhouse_mode()
        self.app.refresh_errors()
        self.assertTrue(self.wait_until(lambda: self.app.error_tree.get_children()))
        with mock.patch.object(gui.filedialog, "asksaveasfilename", return_value="") as ask:
            self.app.save_errors_csv()
            ask.assert_called_once()

    def test_export_without_data_warns(self):
        self.app.error_tree.delete(*self.app.error_tree.get_children())
        self.app.save_errors_csv()
        gui.messagebox.showwarning.assert_called()

    def test_write_csv_uses_export_module_with_utf8_sig(self):
        with tempfile.TemporaryDirectory(prefix="gui-export-") as work:
            path = Path(work) / "export.csv"
            with mock.patch.object(
                gui.filedialog, "asksaveasfilename", return_value=str(path)
            ):
                result = self.app._write_csv(["A", "B"], [["1", "2"]], "export.csv")

            self.assertEqual(result, str(path))
            self.assertEqual(path.read_bytes()[:3], b"\xef\xbb\xbf")
            with path.open(encoding="utf-8-sig", newline="") as handle:
                self.assertEqual(
                    list(csv.reader(handle)), [["A", "B"], ["1", "2"]]
                )
            self.assertIn("Сохранено:", self.app.status_bar.cget("text"))

    def test_write_csv_defuses_formulas(self):
        with tempfile.TemporaryDirectory(prefix="gui-export-") as work:
            path = Path(work) / "formulas.csv"
            with mock.patch.object(
                gui.filedialog, "asksaveasfilename", return_value=str(path)
            ):
                self.app._write_csv(["Value"], [["=SUM(A1:A2)"]], "formulas.csv")
            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.reader(handle))
            self.assertEqual(rows[1][0], "'=SUM(A1:A2)")

    def test_write_csv_failure_is_atomic_and_reports_error(self):
        with tempfile.TemporaryDirectory(prefix="gui-export-") as work:
            path = Path(work) / "existing.csv"
            path.write_text("keep me", encoding="utf-8")
            with mock.patch.object(
                gui.filedialog, "asksaveasfilename", return_value=str(path)
            ):
                result = self.app._write_csv(
                    ["A"], [["too", "many", "values"]], "existing.csv"
                )
            self.assertIsNone(result)
            self.assertEqual(path.read_text(encoding="utf-8"), "keep me")
            title, message = gui.messagebox.showerror.call_args[0][:2]
            self.assertEqual(title, "Ошибка")
            self.assertIn("Не удалось сохранить CSV", message)

    def test_export_tree_and_format_helpers(self):
        self.assertEqual(
            self.app._export_tree_for(gui.EXPORT_TARGET_ERRORS),
            (self.app.error_tree, "errors"),
        )
        self.assertEqual(
            self.app._export_tree_for(gui.EXPORT_TARGET_USERS),
            (self.app.user_stats_tree, "users"),
        )
        self.assertEqual(
            self.app._export_tree_for(gui.EXPORT_TARGET_OVERVIEW),
            (self.app.event_tree, "overview"),
        )
        self.assertEqual(
            self.app._export_tree_for(gui.EXPORT_TARGET_PERFORMANCE),
            (self.app.performance_tree, "performance"),
        )

        self.app.export_format_var.set("JSON")
        self.assertEqual(
            self.app._selected_export_format(), export.ExportFormat.JSON
        )
        self.app.export_format_var.set("bogus")
        self.assertEqual(
            self.app._selected_export_format(), export.ExportFormat.CSV
        )

    def test_fetch_error_export_page_maps_display_values(self):
        self.enter_clickhouse_mode()
        rows = self.app._fetch_error_export_page(
            self.app.clickhouse_config, self.app.filters, 0, 500
        )
        self.assertEqual(
            rows,
            [["2026-01-01 00:10:00", "EXCP", "db1", "Иванов", "Ошибка SQL"]],
        )
        self.assertIn("query_errors", self.fake.kinds())
        call = [item for item in self.fake.calls if item[0] == "query_errors"][-1]
        self.assertEqual(call[2:], (0, gui.EXPORT_PAGE_SIZE))

    def test_confirm_export_size_thresholds(self):
        with mock.patch.object(
            gui.messagebox, "askyesno", return_value=True
        ) as ask:
            self.assertEqual(self.app._confirm_export_size(5_000), (True, False))
            ask.assert_not_called()
            self.assertEqual(self.app._confirm_export_size(20_000), (True, False))
            ask.assert_called_once()
        with mock.patch.object(
            gui.messagebox, "askyesno", return_value=True
        ) as ask:
            self.assertEqual(
                self.app._confirm_export_size(200_000), (True, True)
            )
            self.assertEqual(ask.call_count, 2)
        with mock.patch.object(gui.messagebox, "askyesno", return_value=False):
            self.assertEqual(self.app._confirm_export_size(20_000), (False, False))


if __name__ == "__main__":
    unittest.main()
