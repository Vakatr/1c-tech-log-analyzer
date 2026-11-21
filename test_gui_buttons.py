"""Exhaustive GUI button smoke tests for 1C Log Analyzer.

Every clickable button in the main window and in the connection dialog is
invoked through Tk itself.  ClickHouse is replaced with ``FakeAdapter`` from
``gui_test_support``, so the check is offline and deterministic.

Run with::

    python -m pytest test_gui_buttons.py -q
"""

import csv
import json
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from unittest import mock

import pytest

import ics_log_analyzer as gui
from gui_test_support import AppTestBase
from logt_adapter import Filters, ImportResult

pytestmark = pytest.mark.gui


def walk_buttons(widget):
    """Return every ``Button``/``TButton`` below ``widget``.

    Args:
        widget: Root of the widget tree to inspect.

    Returns:
        Flat list of button widgets.
    """
    found = []
    for child in widget.winfo_children():
        if isinstance(child, (tk.Button, ttk.Button)):
            found.append(child)
        found.extend(walk_buttons(child))
    return found


def find_button(widget, label):
    """Return the first button whose visible text equals ``label``.

    Args:
        widget: Root of the widget tree to search.
        label: Exact button caption.

    Returns:
        The matching button widget.

    Raises:
        AssertionError: When no button with that caption exists.
    """
    for button in walk_buttons(widget):
        if str(button.cget("text")) == label:
            return button
    labels = [str(button.cget("text")) for button in walk_buttons(widget)]
    raise AssertionError(f"button {label!r} not found; have {labels}")


class TestAllButtons(AppTestBase):
    """Invoke each button and assert its observable effect."""

    def setUp(self):
        super().setUp()
        self.work = Path(tempfile.mkdtemp(prefix="gui-smoke-"))
        self.log_dir = self.work / "logs"
        self.log_dir.mkdir()
        (self.log_dir / "a.log").write_text("", encoding="utf-8")
        (self.log_dir / "b.log").write_text("", encoding="utf-8")
        nested = self.log_dir / "nested"
        nested.mkdir()
        (nested / "c.log").write_text("", encoding="utf-8")

    def tearDown(self):
        super().tearDown()
        import shutil

        shutil.rmtree(self.work, ignore_errors=True)

    def connection_dialog_factory(self, holder):
        """Return a factory that records the real dialog instance."""
        original = gui.ConnectionDialog

        def factory(parent, config):
            dialog = original(parent, config)
            holder["dialog"] = dialog
            return dialog

        return factory

    # -- Загрузка ---------------------------------------------------------
    def test_select_folder_button(self):
        with mock.patch.object(
            gui.filedialog, "askdirectory", return_value=str(self.log_dir)
        ):
            find_button(self.app.load_frame, "Выбрать папку с логами").invoke()
        self.assertEqual(self.app.file_path, str(self.log_dir))
        self.assertIn("Файлов: 2", self.app.file_count_label.cget("text"))
        self.assertIn(str(self.log_dir), self.app.path_label.cget("text"))

    def test_load_and_parse_button(self):
        events = [
            {"name": "CALL", "user": "u1", "timestamp": "2026-01-01 00:00:00"},
            {
                "name": "EXCP",
                "user": "u1",
                "timestamp": "2026-01-01 00:00:01",
                "descr": "boom",
            },
        ]
        self.app.file_path = str(self.log_dir)
        with mock.patch.object(gui, "parse_log_directory", return_value=events):
            find_button(self.app.load_frame, "Загрузить и распарсить").invoke()
            self.assertTrue(
                self.wait_until(lambda: self.app.events == events and not self.app.busy)
            )
        self.assertEqual(self.app.source, "memory")
        self.assertIn("Событий: 2", self.app.summary_label.cget("text"))

    def test_load_into_clickhouse_button(self):
        self.app.file_path = str(self.log_dir)
        self.app.dataset_var.set("test_ds")
        find_button(self.app.load_frame, "Загрузить в ClickHouse (LogT)").invoke()
        self.assertTrue(
            self.wait_until(
                lambda: self.app.source == "clickhouse" and not self.app.busy
            )
        )
        self.assertIn("import_logs", self.fake.kinds())
        self.assertIn("ensure_schema", self.fake.kinds())

    def test_cancel_import_button(self):
        event = threading.Event()
        self.app.cancel_event = event
        self.app._set_busy(True, action="import")
        button = find_button(self.app.load_frame, "Отменить")
        self.assertFalse(button.instate(["disabled"]))
        button.invoke()
        self.assertTrue(event.is_set())
        self.assertIn("Отмена", self.app.status_bar.cget("text"))
        self.app._set_busy(False)

    # -- ClickHouse connection / schema -----------------------------------
    def test_check_connection_button(self):
        find_button(self.app.load_frame, "Проверить подключение").invoke()
        self.assertTrue(self.wait_until(lambda: "test" in self.fake.kinds()))
        self.assertTrue(
            self.wait_until(lambda: "доступен" in self.app.status_bar.cget("text"))
        )

    def test_validate_schema_button(self):
        find_button(self.app.load_frame, "Проверить схему").invoke()
        self.assertTrue(self.wait_until(lambda: self.app.schema_status is not None))
        self.assertIn("validate_schema", self.fake.kinds())

    def test_connection_settings_button_cancel(self):
        before = dict(self.app.clickhouse_config)
        holder = {}
        self.root.after(
            100,
            lambda: find_button(holder["dialog"].window, "Отмена").invoke(),
        )
        with mock.patch.object(
            gui, "ConnectionDialog", side_effect=self.connection_dialog_factory(holder)
        ):
            find_button(self.app.load_frame, "Настройки ClickHouse…").invoke()
        self.assertEqual(self.app.clickhouse_config, before)

    def test_connection_settings_button_save(self):
        holder = {}

        def save_dialog():
            holder["dialog"].vars["host"].set("ch.example")
            find_button(holder["dialog"].window, "Сохранить").invoke()

        self.root.after(100, save_dialog)
        with mock.patch.object(
            gui, "ConnectionDialog", side_effect=self.connection_dialog_factory(holder)
        ):
            find_button(self.app.load_frame, "Настройки ClickHouse…").invoke()
        self.assertEqual(self.app.clickhouse_config["host"], "ch.example")
        self.assertTrue(self.wait_until(lambda: "test" in self.fake.kinds()))

    # -- Filters ----------------------------------------------------------
    def test_apply_filters_button(self):
        self.enter_clickhouse_mode()
        self.app.filter_user_entry.insert(0, "Петров")
        find_button(self.app.load_frame, "Применить").invoke()
        self.assertTrue(self.wait_until(lambda: "overview" in self.fake.kinds()))
        filters = [call[1] for call in self.fake.calls if call[0] == "overview"][-1]
        self.assertEqual(filters.user, "Петров")

    def test_clear_filters_button(self):
        self.enter_clickhouse_mode()
        entries = (
            (self.app.filter_user_entry, "u"),
            (self.app.filter_event_entry, "EXCP"),
            (self.app.filter_search_entry, "boom"),
            (self.app.filter_from_entry, "2026-01-01"),
            (self.app.filter_to_entry, "2026-12-31"),
        )
        for entry, value in entries:
            entry.delete(0, tk.END)
            entry.insert(0, value)
        find_button(self.app.load_frame, "Сбросить").invoke()
        for entry, _ in entries:
            self.assertEqual(entry.get(), "")

    # -- Ошибки -----------------------------------------------------------
    def test_extended_filter_entries_reach_adapter_and_clear(self):
        self.enter_clickhouse_mode()
        self.app.filter_user_entry.insert(0, "Петров")
        self.app.filter_process_entry.insert(0, "Srv")
        self.app.filter_appid_entry.insert(0, "app-1")
        self.app.filter_min_duration_entry.insert(0, "250")
        find_button(self.app.load_frame, "Применить").invoke()
        self.assertTrue(self.wait_until(lambda: "overview" in self.fake.kinds()))
        filters = [call[1] for call in self.fake.calls if call[0] == "overview"][-1]
        self.assertEqual(filters.user, "Петров")
        self.assertEqual(filters.process_name, "srv")
        self.assertEqual(filters.app_id, "app-1")
        self.assertEqual(filters.min_duration_ms, 250)

        find_button(self.app.load_frame, "Сбросить").invoke()
        for entry in (
            self.app.filter_user_entry,
            self.app.filter_process_entry,
            self.app.filter_appid_entry,
            self.app.filter_min_duration_entry,
        ):
            self.assertEqual(entry.get(), "")

    def test_invalid_min_duration_is_ignored(self):
        self.enter_clickhouse_mode()
        self.app.filter_min_duration_entry.insert(0, "not-a-number")
        find_button(self.app.load_frame, "Применить").invoke()
        self.assertTrue(self.wait_until(lambda: "overview" in self.fake.kinds()))
        filters = [call[1] for call in self.fake.calls if call[0] == "overview"][-1]
        self.assertIsNone(filters.min_duration_ms)

    def test_error_pagination_buttons(self):
        self.fake.error_total = 250
        self.enter_clickhouse_mode()
        self.app.refresh_errors()
        self.assertTrue(self.wait_until(lambda: self.app.error_tree.get_children()))
        next_button = find_button(self.app.errors_frame, "Вперёд ▶")
        prev_button = find_button(self.app.errors_frame, "◀ Назад")
        self.assertFalse(next_button.instate(["disabled"]))
        self.assertTrue(prev_button.instate(["disabled"]))
        next_button.invoke()
        self.assertTrue(
            self.wait_until(lambda: "Стр. 2" in self.app.page_label.cget("text"))
        )
        prev_button.invoke()
        self.assertTrue(
            self.wait_until(lambda: "Стр. 1" in self.app.page_label.cget("text"))
        )

    def test_error_search_and_double_click(self):
        self.enter_clickhouse_mode()
        self.app.refresh_errors()
        self.assertTrue(self.wait_until(lambda: self.app.error_tree.get_children()))
        self.app.search_entry.insert(0, "boom")
        self.app.on_search_errors()
        self.assertTrue(self.wait_until(lambda: self.app.filters.search == "boom"))
        first = self.app.error_tree.get_children()[0]
        self.app.error_tree.selection_set(first)
        self.app.on_error_double_click()
        gui.messagebox.showinfo.assert_called()

    # -- Производительность ----------------------------------------------
    def test_performance_refresh_button(self):
        self.enter_clickhouse_mode()
        self.app.top_n_spinbox.set("7")
        find_button(self.app.performance_frame, "Обновить").invoke()
        self.assertTrue(
            self.wait_until(lambda: self.app.performance_tree.get_children())
        )
        limits = [call[2] for call in self.fake.calls if call[0] == "performance"]
        self.assertIn(7, limits)
        self.assertTrue(self.app.performance_agg_tree.get_children())

    # -- По пользователям ------------------------------------------------
    def test_user_selection_loads_events(self):
        self.enter_clickhouse_mode()
        self.app.refresh_users()
        self.assertTrue(
            self.wait_until(lambda: self.app.user_stats_tree.get_children())
        )
        first = self.app.user_stats_tree.get_children()[0]
        self.app.user_stats_tree.selection_set(first)
        self.assertTrue(
            self.wait_until(lambda: self.app.user_events_tree.get_children())
        )

    # -- Экспорт ----------------------------------------------------------
    def test_export_buttons(self):
        self.enter_clickhouse_mode()
        self.app.refresh_all_tabs()
        self.assertTrue(
            self.wait_until(
                lambda: self.app.event_tree.get_children()
                and self.app.error_tree.get_children()
                and self.app.performance_tree.get_children()
            )
        )

        def target(**kwargs):
            return str(self.work / kwargs["initialfile"])

        with mock.patch.object(
            gui.filedialog, "asksaveasfilename", side_effect=target
        ):
            for label in (
                "Сохранить обзор в CSV",
                "Сохранить ошибки в CSV",
                "Сохранить производительность в CSV",
            ):
                find_button(self.app.export_frame, label).invoke()
        for name in ("overview.csv", "errors.csv", "performance.csv"):
            path = self.work / name
            self.assertTrue(path.exists(), name)
            text = path.read_text(encoding="utf-8-sig")
            self.assertTrue(text.strip(), name)

    def test_export_current_table_csv_and_json(self):
        self.enter_clickhouse_mode()
        self.app.refresh_all_tabs()
        self.assertTrue(
            self.wait_until(
                lambda: self.app.error_tree.get_children()
                and self.app.event_tree.get_children()
            )
        )
        self.app.export_target_var.set(gui.EXPORT_TARGET_ERRORS)
        self.app.export_scope_var.set(gui.EXPORT_SCOPE_CURRENT)

        csv_path = self.work / "current_errors.csv"
        self.app.export_format_var.set("CSV")
        with mock.patch.object(
            gui.filedialog, "asksaveasfilename", return_value=str(csv_path)
        ):
            find_button(self.app.export_frame, "Экспортировать…").invoke()
        self.assertTrue(csv_path.exists())
        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            parsed = list(csv.reader(handle))
        self.assertEqual(parsed[0][1], "Тип")
        self.assertEqual(parsed[1][1], "EXCP")
        self.assertIn("Ошибка SQL", parsed[1][-1])

        json_path = self.work / "current_errors.json"
        self.app.export_format_var.set("JSON")
        with mock.patch.object(
            gui.filedialog, "asksaveasfilename", return_value=str(json_path)
        ):
            find_button(self.app.export_frame, "Экспортировать…").invoke()
        document = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(document["row_count"], 1)
        self.assertEqual(document["rows"][0]["Тип"], "EXCP")

    def test_export_current_table_without_data_warns(self):
        self.app.export_target_var.set(gui.EXPORT_TARGET_ERRORS)
        self.app.export_scope_var.set(gui.EXPORT_SCOPE_CURRENT)
        self.app.export_format_var.set("CSV")
        with mock.patch.object(gui.filedialog, "asksaveasfilename") as ask:
            find_button(self.app.export_frame, "Экспортировать…").invoke()
            ask.assert_not_called()
        title, message = gui.messagebox.showwarning.call_args[0][:2]
        self.assertEqual(message, "Нет данных для экспорта!")

    def test_export_all_errors_pages_through_query_errors(self):
        self.enter_clickhouse_mode()
        self.fake.error_total = 3
        self.app.refresh_errors()
        self.assertTrue(self.wait_until(lambda: self.app.error_tree.get_children()))
        self.assertEqual(self.app.error_total, 3)

        self.app.export_target_var.set(gui.EXPORT_TARGET_ERRORS)
        self.app.export_scope_var.set(gui.EXPORT_SCOPE_ALL)
        self.app.export_format_var.set("CSV")
        path = self.work / "all_errors.csv"
        with mock.patch.object(
            gui.filedialog, "asksaveasfilename", return_value=str(path)
        ):
            find_button(self.app.export_frame, "Экспортировать…").invoke()
            self.assertTrue(
                self.wait_until(
                    lambda: path.exists() and not self.app.export_busy, timeout=15
                )
            )
        self.assertIn("query_errors", self.fake.kinds())
        self.assertIn("Ошибка SQL", path.read_text(encoding="utf-8-sig"))
        self.assertIn("Сохранено:", self.app.export_status_label.cget("text"))
        self.assertEqual(str(self.app.export_cancel_button.cget("state")), "disabled")

    def test_export_all_users_writes_aggregates(self):
        self.enter_clickhouse_mode()
        self.app.export_target_var.set(gui.EXPORT_TARGET_USERS)
        self.app.export_scope_var.set(gui.EXPORT_SCOPE_ALL)
        self.app.export_format_var.set("CSV")
        path = self.work / "all_users.csv"
        with mock.patch.object(
            gui.filedialog, "asksaveasfilename", return_value=str(path)
        ):
            find_button(self.app.export_frame, "Экспортировать…").invoke()
            self.assertTrue(
                self.wait_until(
                    lambda: path.exists() and not self.app.export_busy, timeout=15
                )
            )
        self.assertIn("users", self.fake.kinds())
        text = path.read_text(encoding="utf-8-sig")
        self.assertIn("Иванов", text)

    def test_export_all_is_rejected_for_aggregates_and_memory(self):
        self.app.export_scope_var.set(gui.EXPORT_SCOPE_ALL)
        self.app.export_target_var.set(gui.EXPORT_TARGET_OVERVIEW)

        # Memory mode explains the scope limitation first.
        self.app.source = "memory"
        self.app.start_export()
        self.assertIn("только для ClickHouse", gui.messagebox.showwarning.call_args[0][1])

        self.enter_clickhouse_mode()
        self.app.start_export()
        self.assertIn("top-N", gui.messagebox.showwarning.call_args[0][1])

    # -- Пресеты ----------------------------------------------------------
    def test_preset_save_load_delete_and_reset(self):
        self.enter_clickhouse_mode()
        self.app.filter_user_entry.insert(0, "Петров")
        self.app.filter_process_entry.insert(0, "Srv")
        self.app.filter_appid_entry.insert(0, "app-1")
        self.app.filter_min_duration_entry.insert(0, "250")

        with mock.patch.object(
            gui.simpledialog, "askstring", return_value="Мой пресет"
        ):
            find_button(self.app.load_frame, "Сохранить как…").invoke()

        self.assertTrue(self.presets_path.exists())
        self.assertEqual(self.app.preset_store.names(), ["Мой пресет"])
        stored = self.app.preset_store.get("Мой пресет")
        self.assertEqual(stored["filters"]["user"], "Петров")
        self.assertEqual(stored["filters"]["process_name"], "srv")
        self.assertEqual(stored["filters"]["app_id"], "app-1")
        self.assertEqual(stored["filters"]["min_duration_ms"], 250)
        self.assertIn("Мой пресет", self.app.preset_combo["values"])
        self.assertIn("сохранён", self.app.preset_status_label.cget("text"))

        for entry in (
            self.app.filter_user_entry,
            self.app.filter_process_entry,
            self.app.filter_appid_entry,
            self.app.filter_min_duration_entry,
        ):
            entry.delete(0, tk.END)
        self.app.preset_var.set("Мой пресет")
        find_button(self.app.load_frame, "Загрузить").invoke()
        self.assertEqual(self.app.filter_user_entry.get(), "Петров")
        self.assertEqual(self.app.filter_process_entry.get(), "srv")
        self.assertEqual(self.app.filter_appid_entry.get(), "app-1")
        self.assertEqual(self.app.filter_min_duration_entry.get(), "250")
        self.assertTrue(self.wait_until(lambda: "overview" in self.fake.kinds()))

        find_button(self.app.load_frame, "Удалить").invoke()
        self.assertEqual(self.app.preset_store.names(), [])
        self.assertIn("удалён", self.app.preset_status_label.cget("text"))

        with mock.patch.object(
            gui.simpledialog, "askstring", return_value="Ещё один"
        ):
            find_button(self.app.load_frame, "Сохранить как…").invoke()
        find_button(self.app.load_frame, "Сбросить все…").invoke()
        self.assertEqual(self.app.preset_store.names(), [])
        self.assertIn("сброшены", self.app.preset_status_label.cget("text"))

    def test_preset_round_trip_after_restart(self):
        self.enter_clickhouse_mode()
        self.app.filter_user_entry.insert(0, "Петров")
        self.app.filter_search_entry.insert(0, "boom")
        with mock.patch.object(
            gui.simpledialog, "askstring", return_value="Перезапуск"
        ):
            self.app.save_preset()

        second = tk.Toplevel(self.root)
        second.withdraw()
        app2 = gui.LogAnalyzerApp(second)
        try:
            self.assertEqual(app2.filter_user_entry.get(), "Петров")
            self.assertEqual(app2.filter_search_entry.get(), "boom")
            self.assertEqual(app2.preset_var.get(), "Перезапуск")
            # Startup restores widgets without querying or applying filters.
            self.assertEqual(app2.filters, Filters())
        finally:
            app2.shutdown()
            second.destroy()

    # -- Справочник -------------------------------------------------------
    def test_reference_selection_fills_details(self):
        first = self.app.ref_tree.get_children()[0]
        self.app.ref_tree.selection_set(first)
        self.pump(0.2)
        text = self.app.ref_text.get("1.0", tk.END)
        self.assertIn("Описание:", text)
        self.assertEqual(self.app.registry_warning.cget("text"), "")

    # -- Диалог подключения ----------------------------------------------
    def test_connection_dialog_buttons(self):
        config = {
            "host": "127.0.0.1",
            "port": 9000,
            "database": "tracelog",
            "user": "default",
            "password": "",
        }
        dialog = gui.ConnectionDialog(self.root, config)
        dialog.window.update()
        dialog.vars["port"].set("not-a-number")
        find_button(dialog.window, "Сохранить").invoke()
        gui.messagebox.showerror.assert_called()
        self.assertTrue(dialog.window.winfo_exists())
        dialog.vars["port"].set("9440")
        dialog.vars["host"].set("ch.local")
        find_button(dialog.window, "Сохранить").invoke()
        self.assertIsNotNone(dialog.result)
        self.assertEqual(dialog.result["port"], 9440)
        self.assertEqual(dialog.result["host"], "ch.local")

        cancel = gui.ConnectionDialog(self.root, config)
        cancel.window.update()
        find_button(cancel.window, "Отмена").invoke()
        self.assertFalse(cancel.window.winfo_exists())

    # -- Меню ClickHouse (те же обработчики, что у кнопок) ----------------
    def test_schema_reset_menu_is_guarded(self):
        with mock.patch.object(gui.messagebox, "askyesno", return_value=True) as ask:
            with mock.patch.object(gui.simpledialog, "askstring", return_value="nope"):
                with mock.patch.object(gui.logt_adapter, "reset_schema") as reset:
                    self.app.reset_schema_dialog()
                    ask.assert_called_once()
                    reset.assert_not_called()
        self.assertIn("отмен", self.app.status_bar.cget("text").lower())

    # -- Диалог ошибки импорта -------------------------------------------
    def test_import_error_dialog_buttons(self):
        self.fake.import_result = ImportResult(
            success=False,
            exit_code=1,
            error="LogT failed: boom",
            stderr="boom",
            duration=1.0,
        )
        self.app.file_path = str(self.log_dir)
        self.app.dataset_var.set("test_ds")
        find_button(self.app.load_frame, "Загрузить в ClickHouse (LogT)").invoke()
        self.assertTrue(
            self.wait_until(lambda: self.app._import_error_dialog is not None)
        )
        dialog = self.app._import_error_dialog
        dialog.window.update()
        self.assertIn("boom", dialog.details)
        find_button(dialog.window, "Скопировать детали").invoke()
        self.assertIn("скопирован", dialog.copy_status.cget("text").casefold())
        find_button(dialog.window, "Закрыть").invoke()
        dialog.window.update()
        self.assertFalse(dialog.is_open)

    def test_shutdown_closes_import_error_dialog(self):
        self.app._show_import_error(
            gui.ImportFailure(
                stage="Импорт LogT",
                error="boom",
                dataset="test_ds",
                log_dir=str(self.log_dir),
            )
        )
        dialog = self.app._import_error_dialog
        self.assertIsNotNone(dialog)
        self.assertTrue(dialog.is_open)
        self.app.shutdown()
        self.assertFalse(dialog.is_open)

    def test_window_close_button(self):
        self.app.shutdown()
        self.app.on_close()
        self.assertFalse(self.root.winfo_exists())


if __name__ == "__main__":
    import unittest

    sys.exit(unittest.main(verbosity=2))
