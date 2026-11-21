"""GUI failure-path tests for ``ics_log_analyzer``.

These complement the happy-path smoke tests in ``test_ics_log_analyzer`` and
``test_gui_buttons``.  They cover the failure branches that the testing plan
lists under F-05 / TS-10:

* a cancelled import stops the worker and re-enables every control;
* a failed LogT preflight shows a dialog and never starts a worker;
* a parser failure shows a dialog and leaves the window usable;
* the memory-mode user selection path renders events without a backend query;
* a failed CSV write shows a dialog instead of raising;
* closing the window while an import runs cancels it and joins the worker;
* an unhandled Tk callback exception is logged and reported.

Synchronisation always goes through ``wait_until`` / ``Event.wait`` with bounded
timeouts; there is no bare ``time.sleep`` in this module.
"""

import tempfile
import threading
from pathlib import Path
from unittest import mock

import pytest

import ics_log_analyzer as gui
from gui_test_support import AppTestBase
from logt_adapter import AdapterError, ImportResult

pytestmark = pytest.mark.gui


class TestImportCancellation(AppTestBase):
    """TS-10: cancelling from the GUI stops the worker cleanly."""

    def test_cancel_import_reenables_controls_and_reports_cancellation(self):
        started = threading.Event()
        observed = {}

        def cancellable_import(log_dir, config, progress_callback=None,
                               cancel_event=None, dataset_name="default", **kwargs):
            started.set()
            observed["cancel_requested"] = cancel_event.wait(15)
            return ImportResult(
                success=False,
                exit_code=1,
                cancelled=True,
                error="Импорт отменён пользователем",
            )

        self.app.file_path = "C:/logs"
        self.app.dataset_var.set("test_ds")
        with mock.patch.object(gui.logt_adapter, "run_logt_import", cancellable_import):
            self.app.load_via_logt()
            self.assertTrue(self.wait_until(started.is_set, timeout=15))
            self.assertTrue(self.app.busy)
            self.assertEqual(str(self.app.cancel_button.cget("state")), "normal")

            # Cancel through the button the user would click.
            self.app.cancel_button.invoke()
            self.assertTrue(self.wait_until(lambda: not self.app.busy, timeout=15))

        self.assertTrue(observed.get("cancel_requested"), "worker did not see the cancel event")
        self.assertFalse(self.app.import_thread.is_alive())
        self.assertEqual(self.app.source, "memory")
        self.assertEqual(str(self.app.load_button.cget("state")), "normal")
        self.assertEqual(str(self.app.logt_button.cget("state")), "normal")
        self.assertEqual(str(self.app.select_button.cget("state")), "normal")
        self.assertEqual(str(self.app.cancel_button.cget("state")), "disabled")
        self.assertIn("отменён", self.app.status_bar.cget("text").lower())
        self.assertIn("отменён", self.app.summary_label.cget("text").lower())
        gui.messagebox.showerror.assert_not_called()
        gui.messagebox.showinfo.assert_not_called()


class TestPreflightFailure(AppTestBase):
    """TS-10: ``check_logt_runtime`` failing must not start a worker."""

    def test_logt_runtime_error_shows_dialog_and_starts_no_worker(self):
        self.app.file_path = "C:/logs"
        self.app.dataset_var.set("test_ds")
        failure = AdapterError("LogT cannot be started: set LOGT_ROOT and LOGT_PYTHON.")

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
        self.assertEqual(self.fake.kinds(), [])
        self.assertNotIn("logt-import", {thread.name for thread in threading.enumerate()})


class TestParseFailure(AppTestBase):
    """TS-10: a parser exception is a dialog, not a broken window."""

    def test_parse_error_shows_dialog_and_keeps_app_usable(self):
        self.app.file_path = "C:/logs"
        with mock.patch.object(
            gui, "parse_log_directory", side_effect=OSError("cannot read log")
        ):
            self.app.load_and_parse()
            self.assertTrue(self.wait_until(lambda: not self.app.busy, timeout=15))

        gui.messagebox.showerror.assert_called_once()
        title, message = gui.messagebox.showerror.call_args[0][:2]
        self.assertEqual(title, "Ошибка")
        self.assertIn("cannot read log", message)
        self.assertEqual(self.app.source, "memory")
        self.assertEqual(self.app.events, [])
        self.assertTrue(self.app.summary_label.cget("text").startswith("Ошибка"))
        for widget in (self.app.load_button, self.app.logt_button, self.app.select_button):
            self.assertEqual(str(widget.cget("state")), "normal")

        # A later successful parse must still work.
        events = [{"name": "CALL", "user": "u1", "timestamp": "2026-01-01 00:00:00"}]
        with mock.patch.object(gui, "parse_log_directory", return_value=events):
            self.app.load_and_parse()
            self.assertTrue(self.wait_until(lambda: self.app.events == events, timeout=15))
        self.assertIn("Событий: 1", self.app.summary_label.cget("text"))


class TestMemoryUserEvents(AppTestBase):
    """TS-10: ``_memory_user_events`` is reached through user selection."""

    def test_user_selection_renders_events_from_memory(self):
        self.app.source = "memory"
        self.app.events = [
            {
                "name": "CALL",
                "user": "Иванов",
                "timestamp": "2026-01-01 00:00:00",
                "process": "p1",
                "descr": "вызов",
            },
            {
                "name": "EXCP",
                "user": "Иванов",
                "timestamp": "2026-01-01 00:00:01",
                "process": "p1",
                "descr": "boom",
            },
            {
                "name": "CALL",
                "user": "Петров",
                "timestamp": "2026-01-01 00:00:02",
                "process": "p2",
                "descr": "чужое событие",
            },
        ]
        self.app.refresh_users()
        self.assertTrue(self.wait_until(lambda: self.app.user_stats_tree.get_children()))

        first = self.app.user_stats_tree.get_children()[0]
        values = self.app.user_stats_tree.item(first, "values")
        self.assertEqual(values[0], "Иванов")
        self.assertEqual(values[1], "2")
        self.assertEqual(values[2], "1")

        self.app.user_stats_tree.selection_set(first)
        self.app.on_user_click()
        self.assertTrue(self.wait_until(lambda: self.app.user_events_tree.get_children()))

        self.assertEqual(self.app.selected_user, "Иванов")
        self.assertEqual(len(self.app.user_events_tree.get_children()), 2)
        row = self.app.user_events_tree.item(
            self.app.user_events_tree.get_children()[0], "values"
        )
        self.assertEqual(row[0], "2026-01-01 00:00:00")
        self.assertEqual(row[1], "CALL")
        self.assertEqual(row[2], "p1")
        self.assertEqual(row[4], "вызов")
        self.assertNotIn("user_events", self.fake.kinds())


class TestCsvExportFailure(AppTestBase):
    """TS-10: a failed write is reported and leaves no partial state."""

    def test_csv_write_error_shows_dialog_and_does_not_crash(self):
        self.enter_clickhouse_mode()
        self.app.refresh_errors()
        self.assertTrue(self.wait_until(lambda: self.app.error_tree.get_children()))

        with tempfile.TemporaryDirectory(prefix="gui-csv-") as work:
            # Writing below a non-existent directory raises a real OSError.
            broken = Path(work) / "missing" / "errors.csv"
            with mock.patch.object(
                gui.filedialog, "asksaveasfilename", return_value=str(broken)
            ):
                self.app.save_errors_csv()

            self.assertFalse(broken.exists())
            gui.messagebox.showerror.assert_called_once()
            title, message = gui.messagebox.showerror.call_args[0][:2]
            self.assertEqual(title, "Ошибка")
            self.assertIn("Не удалось сохранить CSV", message)
            self.assertNotIn("Сохранено:", self.app.status_bar.cget("text"))

            # The app is still usable: a valid path writes the file.
            good = Path(work) / "errors.csv"
            with mock.patch.object(
                gui.filedialog, "asksaveasfilename", return_value=str(good)
            ):
                self.app.save_errors_csv()
            self.assertTrue(good.exists())
            self.assertIn("Сохранено:", self.app.status_bar.cget("text"))

        self.assertEqual(gui.messagebox.showerror.call_count, 1)
        self.assertTrue(self.app.error_tree.get_children())


class TestExportWorkerFailurePaths(AppTestBase):
    """P2-03: export worker failures and cancellation never strand controls."""

    def test_export_failed_reenables_controls_and_reports(self):
        self.app._set_export_busy(True)
        self.app.export_cancel_event = threading.Event()
        self.app.dispatcher.post("export_failed", "boom")
        self.assertTrue(self.wait_until(lambda: not self.app.export_busy))

        self.assertEqual(str(self.app.export_button.cget("state")), "normal")
        self.assertEqual(str(self.app.export_cancel_button.cget("state")), "disabled")
        self.assertIsNone(self.app.export_cancel_event)
        self.assertIn("Ошибка экспорта", self.app.export_status_label.cget("text"))
        title, message = gui.messagebox.showerror.call_args[0][:2]
        self.assertEqual(title, "Ошибка")
        self.assertIn("Не удалось сохранить экспорт", message)
        self.assertIn("boom", message)

    def test_export_cancelled_reenables_controls(self):
        self.app._set_export_busy(True)
        event = threading.Event()
        self.app.export_cancel_event = event

        self.app.cancel_export()
        self.assertTrue(event.is_set())
        self.assertIn("Отмена", self.app.export_status_label.cget("text"))
        self.assertEqual(str(self.app.export_cancel_button.cget("state")), "disabled")

        self.app.dispatcher.post("export_cancelled", None)
        self.assertTrue(self.wait_until(lambda: not self.app.export_busy))
        self.assertEqual(str(self.app.export_button.cget("state")), "normal")
        self.assertEqual(str(self.app.export_cancel_button.cget("state")), "disabled")
        self.assertIsNone(self.app.export_cancel_event)
        self.assertEqual(
            self.app.export_status_label.cget("text"), "Экспорт отменён."
        )
        gui.messagebox.showerror.assert_not_called()

    def test_worker_export_error_reaches_failure_handler(self):
        self.enter_clickhouse_mode()
        self.app.error_total = 1
        self.app.export_target_var.set(gui.EXPORT_TARGET_ERRORS)
        self.app.export_scope_var.set(gui.EXPORT_SCOPE_ALL)
        with tempfile.TemporaryDirectory(prefix="gui-export-") as work:
            path = Path(work) / "broken.csv"
            with mock.patch.object(
                gui.export, "export_rows", side_effect=gui.export.ExportError("disk full")
            ):
                with mock.patch.object(
                    gui.filedialog, "asksaveasfilename", return_value=str(path)
                ):
                    self.app.start_export()
                    self.assertTrue(self.wait_until(lambda: not self.app.export_busy))

            self.assertFalse(path.exists())
            self.assertIn(
                "Ошибка экспорта", self.app.export_status_label.cget("text")
            )
            title, message = gui.messagebox.showerror.call_args[0][:2]
            self.assertEqual(title, "Ошибка")
            self.assertIn("disk full", message)
            self.assertEqual(str(self.app.export_button.cget("state")), "normal")


class TestShutdownWithRunningWorker(AppTestBase):
    """TS-10: closing the window while a worker runs stops it cleanly."""

    def test_close_window_cancels_import_and_joins_worker(self):
        started = threading.Event()
        finished = threading.Event()
        observed = {}

        def cancellable_import(log_dir, config, progress_callback=None,
                               cancel_event=None, dataset_name="default", **kwargs):
            started.set()
            observed["cancel_requested"] = cancel_event.wait(15)
            finished.set()
            return ImportResult(
                success=False, exit_code=1, cancelled=True, error="cancelled"
            )

        self.app.file_path = "C:/logs"
        self.app.dataset_var.set("test_ds")
        with mock.patch.object(gui.logt_adapter, "run_logt_import", cancellable_import):
            self.app.load_via_logt()
            self.assertTrue(self.wait_until(started.is_set, timeout=15))
            event = self.app.cancel_event
            thread = self.app.import_thread
            self.assertIsNotNone(event)
            self.assertIsNotNone(thread)
            self.assertTrue(thread.is_alive())
            self.assertTrue(self.app.busy)

            self.app.on_close()

        self.assertFalse(self.app._running)
        self.assertTrue(event.is_set())
        self.assertTrue(observed.get("cancel_requested"), "cancel event was never observed")
        self.assertTrue(finished.is_set(), "worker did not finish")
        self.assertFalse(thread.is_alive())
        self.assertFalse(self.root.winfo_exists())
        gui.messagebox.showerror.assert_not_called()


class TestCallbackExceptionReporting(AppTestBase):
    """DEBT-003: Tk callback errors are logged and shown, never swallowed."""

    def test_unhandled_callback_exception_is_logged_and_reported(self):
        error = ValueError("boom in callback")
        with self.assertLogs(gui.logger, level="ERROR") as captured:
            self.app._report_callback_exception(ValueError, error, None)

        self.assertIn("Unhandled Tk callback exception", "\n".join(captured.output))
        gui.messagebox.showerror.assert_called_once()
        title, message = gui.messagebox.showerror.call_args[0][:2]
        self.assertEqual(title, "Ошибка")
        self.assertIn("boom in callback", message)
