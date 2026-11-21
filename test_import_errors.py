"""Tests for :mod:`import_errors` — diagnosable import failures.

The model and the formatter are pure Python; the dialog needs Tk, so those
cases are skipped on a headless interpreter exactly like ``test_ics_log_analyzer``.
"""

import os
import tkinter as tk
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import import_errors
from import_errors import (
    ImportErrorDialog,
    ImportFailure,
    find_latest_logt_log,
    format_import_failure,
)
from gui_test_support import TK_AVAILABLE, _shared_root
from logt_adapter import ImportResult


def _result(**overrides):
    """Build an :class:`ImportResult` with sensible failure defaults."""
    payload = {
        "success": False,
        "exit_code": 1,
        "stdout": "",
        "stderr": "Traceback (most recent call last):\nboom\n",
        "duration": 3.5,
        "error": "LogT failed: boom",
    }
    payload.update(overrides)
    return ImportResult(**payload)


class TestImportFailureFromResult(unittest.TestCase):
    """``ImportFailure.from_result`` copies every diagnostic field."""

    def test_copies_stage_dataset_and_result_fields(self):
        result = _result(duration=12.5)
        failure = ImportFailure.from_result(
            result,
            stage="Импорт LogT",
            dataset="test_ds",
            log_dir="C:/logs",
            log_path="C:/logs/.logt_logs/trace-vrs-1.log",
        )
        self.assertEqual(failure.stage, "Импорт LogT")
        self.assertEqual(failure.dataset, "test_ds")
        self.assertEqual(failure.log_dir, "C:/logs")
        self.assertEqual(failure.log_path, "C:/logs/.logt_logs/trace-vrs-1.log")
        self.assertEqual(failure.error, "LogT failed: boom")
        self.assertEqual(failure.exit_code, 1)
        self.assertEqual(failure.duration, 12.5)
        self.assertEqual(failure.stderr_lines, result.stderr_tail)

    def test_keeps_only_the_last_twenty_stdout_lines(self):
        stdout = "\n".join(f"line {index}" for index in range(50))
        failure = ImportFailure.from_result(
            _result(stdout=stdout),
            stage="Импорт LogT",
            dataset="ds",
            log_dir="C:/logs",
        )
        self.assertEqual(len(failure.stdout_lines), import_errors.STDOUT_TAIL_LINES)
        self.assertEqual(failure.stdout_lines[-1], "line 49")
        self.assertEqual(failure.stdout_lines[0], "line 30")

    def test_blank_stdout_lines_are_dropped(self):
        failure = ImportFailure.from_result(
            _result(stdout="a\n\n   \nb\n"),
            stage="Импорт LogT",
            dataset="ds",
            log_dir="C:/logs",
        )
        self.assertEqual(failure.stdout_lines, ["a", "b"])

    def test_missing_optional_attributes_use_safe_defaults(self):
        class Sparse:
            """Duck-typed result with nothing but success."""

            success = False

        failure = ImportFailure.from_result(
            Sparse(), stage="Подготовка", dataset="ds", log_dir="C:/logs"
        )
        self.assertEqual(failure.exit_code, -1)
        self.assertEqual(failure.duration, 0.0)
        self.assertEqual(failure.error, "")
        self.assertEqual(failure.stderr_lines, [])
        self.assertEqual(failure.stdout_lines, [])


class TestFormatImportFailure(unittest.TestCase):
    """The rendered report is what the user copies into a bug report."""

    def setUp(self):
        self.failure = ImportFailure(
            stage="Импорт LogT",
            error="LogT exited with code 1",
            dataset="tracelog",
            log_dir="C:/logs",
            log_path="C:/logs/.logt_logs/trace-vrs-2026.log",
            exit_code=1,
            duration=7.25,
            stderr_lines=["Traceback (most recent call last):", "boom"],
            stdout_lines=["[INFO] Найдено файлов rphost: 3"],
        )

    def test_includes_stage_dataset_code_and_journal(self):
        details = format_import_failure(self.failure)
        self.assertIn("Этап: Импорт LogT", details)
        self.assertIn("Dataset: tracelog", details)
        self.assertIn("Папка с логами: C:/logs", details)
        self.assertIn("Код возврата LogT: 1", details)
        self.assertIn("Длительность: 7.2 с", details)
        self.assertIn("C:/logs/.logt_logs/trace-vrs-2026.log", details)

    def test_includes_stderr_and_stdout_tails(self):
        details = format_import_failure(self.failure)
        self.assertIn("stderr", details)
        self.assertIn("Traceback (most recent call last):", details)
        self.assertIn("boom", details)
        self.assertIn("stdout", details)
        self.assertIn("[INFO] Найдено файлов rphost: 3", details)

    def test_reports_a_missing_journal_with_the_expected_folder(self):
        self.failure.log_path = None
        details = format_import_failure(self.failure)
        self.assertIn("не найден в", details)
        self.assertIn(str(Path("C:/logs") / ".logt_logs"), details)

    def test_empty_failure_still_renders(self):
        details = format_import_failure(ImportFailure())
        self.assertIn("Неизвестная ошибка", details)
        self.assertIn("stderr: пусто", details)
        self.assertIn("Журнал LogT: не найден в .logt_logs", details)


class TestFindLatestLogtLog(unittest.TestCase):
    """The newest ``trace-vrs-*.log`` is the one worth opening."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.log_dir = Path(self._tmp.name)
        self.logt_dir = self.log_dir / ".logt_logs"
        self.logt_dir.mkdir()

    def _write(self, name, mtime):
        path = self.logt_dir / name
        path.write_text("log", encoding="utf-8")
        os.utime(path, (mtime, mtime))
        return path

    def test_returns_the_newest_trace_file(self):
        self._write("trace-vrs-1.log", 1_000.0)
        newest = self._write("trace-vrs-2.log", 2_000.0)
        self._write("unrelated.log", 3_000.0)
        self.assertEqual(find_latest_logt_log(str(self.log_dir)), str(newest))

    def test_newer_than_filters_older_runs(self):
        self._write("trace-vrs-1.log", 1_000.0)
        new = self._write("trace-vrs-2.log", 2_000.0)
        self.assertEqual(
            find_latest_logt_log(str(self.log_dir), newer_than=1_500.0), str(new)
        )

    def test_newer_than_can_filter_everything(self):
        self._write("trace-vrs-1.log", 1_000.0)
        self.assertIsNone(find_latest_logt_log(str(self.log_dir), newer_than=2_000.0))

    def test_missing_directory_returns_none(self):
        self.assertIsNone(find_latest_logt_log(str(self.log_dir / "missing")))

    def test_empty_log_dir_returns_none(self):
        self.assertIsNone(find_latest_logt_log(""))

    def test_filesystem_error_returns_none(self):
        with mock.patch.object(Path, "glob", side_effect=OSError("denied")):
            self.assertIsNone(find_latest_logt_log(str(self.log_dir)))


@unittest.skipUnless(TK_AVAILABLE, "Tk is not available in this environment")
class TestImportErrorDialog(unittest.TestCase):
    """The dialog shows selectable details and copies them on demand."""

    def setUp(self):
        self.root = tk.Toplevel(_shared_root())
        self.root.withdraw()
        self.failure = ImportFailure(
            stage="Импорт LogT",
            error="LogT failed: boom",
            dataset="test_ds",
            log_dir="C:/logs",
            log_path=None,
            exit_code=2,
            duration=1.5,
            stderr_lines=["boom"],
            stdout_lines=["progress"],
        )
        self.dialog = ImportErrorDialog(self.root, self.failure)
        self.addCleanup(self._destroy)
        self.dialog.window.update()

    def _destroy(self):
        self.dialog.close()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def test_dialog_shows_read_only_but_selectable_details(self):
        self.assertTrue(self.dialog.is_open)
        self.assertEqual(str(self.dialog.text.cget("state")), "disabled")
        text = self.dialog.text.get("1.0", tk.END)
        self.assertIn("Этап: Импорт LogT", text)
        self.assertIn("boom", text)
        self.assertIn("Код возврата LogT: 2", text)

    def test_dialog_is_not_modal(self):
        self.assertFalse(self.dialog.window.grab_current())
        self.assertTrue(self.dialog.window.bind("<Escape>"))
        self.assertTrue(self.dialog.window.protocol("WM_DELETE_WINDOW"))

    def test_copy_button_puts_details_on_the_clipboard(self):
        try:
            copied = self.dialog.copy_details()
            clipboard = self.dialog.window.clipboard_get()
        except tk.TclError:  # pragma: no cover - headless clipboard
            self.skipTest("clipboard is not available in this environment")
        self.assertTrue(copied)
        self.assertEqual(clipboard, self.dialog.details)
        self.assertIn("скопирован", self.dialog.copy_status.cget("text").casefold())

    def test_close_is_idempotent(self):
        self.dialog.close()
        self.dialog.close()
        self.assertFalse(self.dialog.is_open)

    def test_redact_callback_is_applied_to_details(self):
        dialog = ImportErrorDialog(
            self.root,
            self.failure,
            redact=lambda text: text.replace("boom", "***"),
        )
        self.addCleanup(dialog.close)
        dialog.window.update()
        try:
            self.assertNotIn("boom", dialog.details)
            self.assertIn("***", dialog.details)
            self.assertNotIn("boom", dialog.text.get("1.0", tk.END))
        finally:
            dialog.close()

    def test_failure_with_unknown_values_still_opens(self):
        dialog = ImportErrorDialog(self.root, ImportFailure())
        self.addCleanup(dialog.close)
        dialog.window.update()
        self.assertIn("не найден в", dialog.details)
        self.assertIn("Неизвестная ошибка", dialog.details)


if __name__ == "__main__":
    unittest.main()
