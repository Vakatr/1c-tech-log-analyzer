"""1C Log Analyzer - tabbed Tkinter GUI.

The Overview, Errors, Performance and Users tabs are backed by ClickHouse via
:mod:`logt_adapter`.  Every database call runs on a worker thread and its result
is handed back to the Tk main thread through a :class:`queue.Queue` drained by
``root.after``.  The legacy in-memory parser (``ics_log_parser``) is kept for
offline, non-ClickHouse usage.

Threading contract
------------------
* The Tk main thread never performs I/O.
* Workers never touch Tk widgets; they push ``(kind, payload)`` tuples onto
  ``self.result_queue``.
* ``_drain_results`` runs on the main thread via ``root.after`` and applies the
  payload to widgets.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import tkinter as tk
from collections import Counter
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable, Collection, Dict, List, Optional

import export
import logt_adapter
import presets
from ics_events_registry import get_event, load_registry, registry_error
from ics_log_parser import detect_format, parse_log_directory
from ics_log_stats import aggregate_by_event, aggregate_by_user, find_errors, top_slowest
from ics_logging import configure_logging
from import_errors import (
    ImportErrorDialog,
    ImportFailure,
    find_latest_logt_log,
    format_import_failure,
)
from import_progress import ImportProgress, ImportStage
from logt_adapter import Filters, ImportResult, SchemaStatus

logger = logging.getLogger(__name__)

#: How often the main thread polls the result queue, in milliseconds.
QUEUE_POLL_MS = 100

#: Default number of rows per page in the Errors tab.
DEFAULT_PAGE_SIZE = 100

#: Rows per page when paging through every filtered error for export.
EXPORT_PAGE_SIZE = 500

#: Row count above which "export all" asks for confirmation.
WARN_ROWS = 10_000

#: Hard row cap for "export all" when the user agrees to truncation.
DEFAULT_MAX_ROWS = 100_000

#: Human-readable export targets, in combobox order.
EXPORT_TARGET_ERRORS = "Ошибки"
EXPORT_TARGET_USERS = "Пользователи"
EXPORT_TARGET_OVERVIEW = "Обзор"
EXPORT_TARGET_PERFORMANCE = "Производительность"

#: Targets offered by the export tab.
EXPORT_TARGETS = (
    EXPORT_TARGET_ERRORS,
    EXPORT_TARGET_USERS,
    EXPORT_TARGET_OVERVIEW,
    EXPORT_TARGET_PERFORMANCE,
)

#: Supported export formats, in combobox order.
EXPORT_FORMATS = ("CSV", "JSON", "XLSX")

#: Scope values used by the export radiobuttons.
EXPORT_SCOPE_CURRENT = "current"
EXPORT_SCOPE_ALL = "all"

#: Hint shown for the extended filters and the memory-mode limitation.
FILTER_HINT = (
    "AppID ищется по тексту события — сузьте даты. Мин. длительность скрывает "
    "события без Duration, включая ошибки."
)

#: Note explaining that filters only work against ClickHouse.
FILTER_SCOPE_NOTE = (
    "Фильтры применяются только в режиме ClickHouse; в режиме памяти они "
    "не фильтруют таблицы."
)

#: Label shown for the description column, which is derived rather than stored.
DESCRIPTION_COLUMN_LABEL = "Описание (из текста события)"

#: Suffix appended when a description is the 200-character event-text excerpt.
DESCRIPTION_EXCERPT_MARK = "…"

#: Human-readable titles for the typed import stages.  The adapter normally
#: sends its own message; this table is the fallback for events without one and
#: for the terminal states drawn by the GUI (done/cancelled/failed).
IMPORT_STAGE_TITLES: Dict[ImportStage, str] = {
    ImportStage.PREPARING: "Подготовка",
    ImportStage.SCHEMA: "Схема ClickHouse",
    ImportStage.SCANNING: "Сканирование",
    ImportStage.PROCESSING: "Обработка логов",
    ImportStage.INSERTING: "Вставка",
    ImportStage.COUNTING: "Подсчёт строк",
    ImportStage.DONE: "Готово",
}

#: Log-level markers that must never reach the status bar.
_STATUS_PREFIXES = (
    "[INFO]",
    "[DEBUG]",
    "[WARNING]",
    "[ERROR]",
    "INFO:",
    "DEBUG:",
    "WARNING:",
    "ERROR:",
)


def clean_status_text(value: Any) -> str:
    """Return a single-line status string without log noise.

    LogT writes progress with carriage returns, and older code paths prefixed
    lines with ``[INFO]``.  The status bar is one line, so the text is flattened
    and the leading level markers are dropped.  Empty input yields ``""``.

    Args:
        value: Arbitrary payload (usually ``str``).

    Returns:
        Whitespace-normalised text safe to show in a Tk label.
    """
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    changed = True
    while changed:
        changed = False
        for prefix in _STATUS_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip()
                changed = True
    return " ".join(text.split())


def import_stage_title(stage: Any) -> str:
    """Return the Russian title of ``stage`` (or its cleaned string form)."""
    if isinstance(stage, ImportStage):
        return IMPORT_STAGE_TITLES.get(stage, stage.value)
    text = clean_status_text(stage)
    try:
        return IMPORT_STAGE_TITLES[ImportStage(text)]
    except (TypeError, ValueError):
        return text


def format_import_percent(percent: Optional[float]) -> str:
    """Format a completion percentage for the percent label (``25 %``)."""
    if percent is None:
        return ""
    return f"{percent:.0f} %"


def format_description(row: Dict[str, Any]) -> str:
    """Render a result row's description, marking excerpts with an ellipsis.

    Args:
        row: Row dictionary produced by :func:`logt_adapter.query_errors` or
            :func:`logt_adapter.query_user_events`.

    Returns:
        The description text.  When ``description_is_excerpt`` is true the
        value came from the raw event text rather than an explicit ``Descr``
        field, so a trailing ellipsis tells the user it may be cut off.
    """
    text = str(row.get("description", "") or "")
    if text and row.get("description_is_excerpt"):
        return text + DESCRIPTION_EXCERPT_MARK
    return text


class ResultDispatcher:
    """Deliver worker results to the Tk main thread.

    Workers call :meth:`post`; the GUI polls :meth:`drain` from ``root.after``.

    Attributes:
        queue: Thread-safe queue carrying ``(kind, payload)`` tuples.
    """

    def __init__(self, root: tk.Misc):
        """Store the root window used to schedule polling.

        Args:
            root: Tk root (or any widget) offering ``after``.
        """
        self.root = root
        self.queue: "queue.Queue" = queue.Queue()

    def post(self, kind: str, payload: Any = None) -> None:
        """Enqueue a result for the main thread.

        Args:
            kind: Message kind understood by the app's handler map.
            payload: Arbitrary result object.
        """
        self.queue.put((kind, payload))

    def drain(
        self,
        handler: Callable[[str, Any], None],
        coalesce_kinds: Collection[str] = (),
    ) -> None:
        """Deliver every queued message to ``handler``.

        Messages whose kind is listed in ``coalesce_kinds`` are collapsed: only
        the newest payload of such a kind is kept.  Before a message of any
        other kind is delivered, the accumulated payload is delivered first.
        This ordering matters for progress: a burst of ``import_progress``
        events followed by ``import_done`` must not apply the stale progress
        after the completion handler has already reset or finished the bar.
        The remaining coalesced payload is delivered once the queue is empty.

        Args:
            handler: Callback invoked as ``handler(kind, payload)`` per message.
            coalesce_kinds: Kinds for which only the latest payload is kept.
        """
        coalesce = set(coalesce_kinds)
        pending: Dict[str, Any] = {}
        pending_order: List[str] = []

        def deliver(kind: str, payload: Any) -> None:
            try:
                handler(kind, payload)
            except Exception:  # pragma: no cover - a bad payload must not kill the loop
                logger.exception("Result handler failed for message kind %r", kind)

        def flush_pending() -> None:
            for pending_kind in pending_order:
                deliver(pending_kind, pending.pop(pending_kind))
            pending_order.clear()

        while True:
            try:
                kind, payload = self.queue.get_nowait()
            except queue.Empty:
                break
            if kind in coalesce:
                if kind not in pending:
                    pending_order.append(kind)
                pending[kind] = payload
                continue
            if pending:
                flush_pending()
            deliver(kind, payload)

        if pending:
            flush_pending()


class ClickHouseClient:
    """GUI-side façade over :mod:`logt_adapter` that reports progress.

    All methods are blocking and must be called from a worker thread.
    """

    def __init__(self, config: Dict[str, Any]):
        """Store connection settings.

        Args:
            config: Resolved ClickHouse connection settings.
        """
        self.config = config

    # -- connection --------------------------------------------------------
    def test(self) -> bool:
        """Return whether ClickHouse is reachable."""
        return logt_adapter.check_clickhouse_connection(self.config)

    def ensure_schema(self) -> SchemaStatus:
        """Create missing tables (idempotent, never destructive)."""
        return logt_adapter.ensure_schema(self.config)

    def validate_schema(self) -> SchemaStatus:
        """Validate the schema without changing it."""
        return logt_adapter.validate_schema(self.config)

    # -- tabs --------------------------------------------------------------
    def overview(self, filters: Filters, top_n: int = 10) -> Dict[str, Any]:
        """Return the Overview payload."""
        return logt_adapter.query_overview(self.config, filters, top_n=top_n)

    def errors(self, filters: Filters, page: int, page_size: int) -> Dict[str, Any]:
        """Return one page of errors plus the total count.

        Pagination is clamped defensively: :func:`logt_adapter.query_errors`
        rejects out-of-range values, while the UI owns its own counters and
        must never hand it a meaningless page.
        """
        page = max(0, int(page))
        page_size = max(1, min(int(page_size), logt_adapter.MAX_PAGE_SIZE))
        total = logt_adapter.count_errors(self.config, filters)
        rows = logt_adapter.query_errors(self.config, filters, page=page, page_size=page_size)
        return {"rows": rows, "total": total, "page": page, "page_size": page_size}

    def performance(self, filters: Filters, limit: int) -> Dict[str, Any]:
        """Return the slowest events and per-event aggregates."""
        return {
            "slowest": logt_adapter.query_performance(self.config, filters, limit=limit),
            "by_event": logt_adapter.query_performance_by_event(self.config, filters, limit=limit),
        }

    def users(self, filters: Filters) -> List[Dict[str, Any]]:
        """Return per-user statistics."""
        return logt_adapter.query_users(self.config, filters)

    def user_events(self, user: str, filters: Filters, limit: int = 200) -> List[Dict[str, Any]]:
        """Return recent events for one user."""
        return logt_adapter.query_user_events(user, self.config, filters, limit=limit)

    # -- import ------------------------------------------------------------
    def import_logs(
        self,
        log_dir: str,
        dataset: str,
        cancel_event: threading.Event,
        progress: Callable[[str], None],
        min_duration_ms: int = 0,
        min_events: int = 0,
        *,
        on_progress: Optional[Callable[[ImportProgress], None]] = None,
        count_lines_for_progress: bool = True,
    ) -> ImportResult:
        """Run a LogT import.

        Args:
            log_dir: Directory containing the 1C technological logs.
            dataset: Dataset tag for this import.
            cancel_event: Set to abort the import; the child is terminated.
            progress: Legacy ``callback(message)`` receiving raw status lines.
            min_duration_ms: LogT filtering threshold.
            min_events: LogT filtering threshold.
            on_progress: Optional typed, throttled progress callback (P1-03).
                The GUI wires it to its own dispatcher in P1-04.
            count_lines_for_progress: Makes LogT count input lines first so the
                progress bar is determinate; slower start, opt-out in the GUI.

        Returns:
            The :class:`ImportResult` produced by the adapter.
        """
        return logt_adapter.run_logt_import(
            log_dir,
            self.config,
            progress_callback=progress,
            cancel_event=cancel_event,
            dataset_name=dataset,
            min_operation_duration_ms=min_duration_ms,
            min_operation_events=min_events,
            on_progress=on_progress,
            count_lines_for_progress=count_lines_for_progress,
        )


class LogAnalyzerApp:
    """Main application window."""

    def __init__(self, root: tk.Misc):
        """Build the window and all tabs.

        Args:
            root: Tk root window.
        """
        self.root = root
        self.root.title("1C Log Analyzer")
        self.root.geometry("1000x680")
        self.root.minsize(900, 600)
        # DEBT-003: Tk swallows callback exceptions by default.  Route them
        # through the logger and show one dialog instead of dying silently.
        self.root.report_callback_exception = self._report_callback_exception

        # Data storage: the in-memory list is only used by the legacy parser.
        self.events: List[Dict[str, Any]] = []
        self.file_path: str = ""
        # Cached in-memory aggregates; keyed by the current list identity so a
        # new parse invalidates it automatically (PERF-004).  The lock keeps
        # concurrent tab refreshes from computing the same aggregates twice
        # (PERF-003 moves the computation off the Tk main thread).
        self._memory_cache: Optional[tuple] = None
        self._memory_lock = threading.Lock()

        # ClickHouse state ---------------------------------------------------
        self.clickhouse_config: Dict[str, Any] = logt_adapter.resolve_config()
        self.source: str = "memory"            # "memory" | "clickhouse"
        self.dataset: str = logt_adapter.DEFAULT_DATASET
        self.filters = Filters()
        self.cancel_event: Optional[threading.Event] = None
        self.import_thread: Optional[threading.Thread] = None
        self.busy = False

        # Export state -------------------------------------------------------
        #: Set while an "export all" worker is running; read on the Tk thread.
        self.export_busy = False
        #: Cancellation flag shared with the export worker (``threading.Event``).
        self.export_cancel_event: Optional[threading.Event] = None
        #: The running export worker, so shutdown can wait for it.
        self.export_thread: Optional[threading.Thread] = None

        # Presets state ------------------------------------------------------
        #: Created once the filter widgets exist (see ``_init_presets``).
        self.preset_store: Optional[presets.PresetStore] = None
        #: Last typed progress snapshot applied on the Tk thread (P1-04).
        self.last_import_progress: Optional[ImportProgress] = None
        self.error_page = 0
        self.error_page_size = DEFAULT_PAGE_SIZE
        self.error_total = 0
        self.selected_user: Optional[str] = None
        self.schema_status: Optional[SchemaStatus] = None
        self.ch_client = ClickHouseClient(self.clickhouse_config)

        # P1-05 diagnostics: the failure dialog is owned by the app so a new
        # import (or shutdown) can close the previous report.  ``_import_stage``
        # and ``_import_log_path`` are written by the import worker and read on
        # the main thread only after the queued result is delivered.
        self._import_error_dialog: Optional[ImportErrorDialog] = None
        self._import_stage = "Подготовка"
        self._import_log_path: Optional[str] = None

        self.dispatcher = ResultDispatcher(root)
        self.handlers: Dict[str, Callable[[Any], None]] = {
            "import_done": self._on_import_done,
            "import_failed": self._on_import_failed,
            "import_progress": self._on_import_progress,
            "parse_done": self.on_parse_complete,
            "parse_error": self.on_parse_error,
            "overview": self._on_overview,
            "errors": self._on_errors,
            "performance": self._on_performance,
            "users": self._on_users,
            "user_events": self._on_user_events,
            "memory_overview": self._on_memory_overview,
            "memory_errors": self._on_memory_errors,
            "memory_performance": self._on_memory_performance,
            "memory_users": self._on_memory_users,
            "connection": self._on_connection,
            "schema": self._on_schema,
            "error": self._on_error,
            "status": self._on_status,
            "export_progress": self._on_export_progress,
            "export_done": self._on_export_done,
            "export_cancelled": self._on_export_cancelled,
            "export_failed": self._on_export_failed,
        }

        self.status_bar = tk.Label(root, text="Готово", bd=1, relief=tk.SUNKEN, anchor=tk.W)
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self._build_menu()
        self.create_load_tab()
        self.create_overview_tab()
        self.create_errors_tab()
        self.create_performance_tab()
        self.create_users_tab()
        self.create_export_tab()
        self.create_reference_tab()

        self.notebook.add(self.load_frame, text="Загрузка")
        self.notebook.add(self.overview_frame, text="Обзор")
        self.notebook.add(self.errors_frame, text="Ошибки")
        self.notebook.add(self.performance_frame, text="Производительность")
        self.notebook.add(self.users_frame, text="По пользователям")
        self.notebook.add(self.export_frame, text="Экспорт")
        self.notebook.add(self.reference_frame, text="Справочник")

        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._running = True
        self._poll_results()
        self._init_presets()

    # ==================================================================
    # Menu
    # ==================================================================
    def _build_menu(self) -> None:
        """Create the menu bar, including the guarded schema reset."""
        menubar = tk.Menu(self.root)
        ch_menu = tk.Menu(menubar, tearoff=0)
        ch_menu.add_command(label="Настройки подключения…", command=self.open_connection_settings)
        ch_menu.add_command(label="Проверить подключение", command=self.check_connection_async)
        ch_menu.add_command(label="Проверить схему", command=self.validate_schema_async)
        ch_menu.add_separator()
        ch_menu.add_command(
            label="Сбросить схему ClickHouse…",
            command=self.reset_schema_dialog,
        )
        menubar.add_cascade(label="ClickHouse", menu=ch_menu)
        self.root.configure(menu=menubar)
        self.menubar = menubar

    # ==================================================================
    # Status + result plumbing
    # ==================================================================
    def set_status(self, message: str) -> None:
        """Show ``message`` in the status bar (main thread only).

        Args:
            message: Text to display.
        """
        try:
            self.status_bar.config(text=str(message)[:400])
        except tk.TclError:  # pragma: no cover - during shutdown
            pass

    def _report_callback_exception(self, exc_type, exc_value, exc_tb) -> None:
        """Log an unhandled Tk callback error and inform the user.

        Tkinter's default handler prints the traceback to a stream that may not
        exist in a windowed build.  Logging it and showing a single dialog keeps
        the failure diagnosable and the application alive.
        """
        logger.error(
            "Unhandled Tk callback exception", exc_info=(exc_type, exc_value, exc_tb)
        )
        try:
            messagebox.showerror("Ошибка", str(exc_value)[:1000])
        except tk.TclError:  # pragma: no cover - window already destroyed
            pass

    def _poll_results(self) -> None:
        """Drain worker results on the main thread and reschedule."""
        if not self._running:
            return
        self.dispatcher.drain(
            self._dispatch,
            coalesce_kinds={"import_progress", "export_progress"},
        )
        try:
            self.root.after(QUEUE_POLL_MS, self._poll_results)
        except tk.TclError:  # pragma: no cover - window already destroyed
            self._running = False

    def shutdown(self) -> None:
        """Stop the result poller and cancel any running import.

        Called by :meth:`on_close` and, in tests, between test cases so a stale
        ``root.after`` callback can never fire against a destroyed widget.
        """
        self._running = False
        if self.cancel_event is not None:
            self.cancel_event.set()
        if self.export_cancel_event is not None:
            self.export_cancel_event.set()
        self._close_import_error_dialog()

    def _dispatch(self, kind: str, payload: Any) -> None:
        """Route one queued message to its handler.

        Args:
            kind: Message kind.
            payload: Attached result.
        """
        handler = self.handlers.get(kind)
        if handler is not None:
            handler(payload)

    def run_async(self, work: Callable[[], Any], kind: str, on_done: Optional[Callable] = None) -> None:
        """Run ``work`` on a daemon thread and post its result.

        Args:
            work: Zero-argument callable performing blocking I/O.
            kind: Message kind used when the call succeeds.
            on_done: Optional extra callback invoked on the worker thread.
        """
        def runner() -> None:
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - reported to the user
                self.dispatcher.post("error", logt_adapter.redact(exc, self.clickhouse_config.get("password")))
                return
            self.dispatcher.post(kind, result)
            if on_done is not None:
                try:
                    on_done(result)
                except Exception:  # pragma: no cover
                    logger.exception("on_done callback failed for message kind %r", kind)

        threading.Thread(target=runner, daemon=True).start()

    # ==================================================================
    # TAB 1: Загрузка
    # ==================================================================
    def create_load_tab(self) -> None:
        """Build the load tab (folder picker, import, filters)."""
        frame = ttk.Frame(self.notebook)
        self.load_frame = frame

        source_box = ttk.LabelFrame(frame, text="Источник данных")
        source_box.pack(fill=tk.X, padx=10, pady=(10, 4))
        self.source_label = ttk.Label(source_box, text=self._source_text())
        self.source_label.pack(anchor=tk.W, padx=6, pady=2)
        conn_frame = ttk.Frame(source_box)
        conn_frame.pack(fill=tk.X, padx=6, pady=2)
        ttk.Button(conn_frame, text="Настройки ClickHouse…", command=self.open_connection_settings).pack(side=tk.LEFT)
        ttk.Button(conn_frame, text="Проверить подключение", command=self.check_connection_async).pack(side=tk.LEFT, padx=4)
        ttk.Button(conn_frame, text="Проверить схему", command=self.validate_schema_async).pack(side=tk.LEFT)

        pick = ttk.Frame(frame)
        pick.pack(fill=tk.X, padx=10, pady=4)
        self.select_button = ttk.Button(pick, text="Выбрать папку с логами", command=self.select_folder)
        self.select_button.pack(side=tk.LEFT)
        self.path_label = ttk.Label(pick, text="Путь: не выбран")
        self.path_label.pack(side=tk.LEFT, padx=8)

        info = ttk.Frame(frame)
        info.pack(fill=tk.X, padx=10)
        self.file_count_label = ttk.Label(info, text="Файлов: 0")
        self.file_count_label.pack(side=tk.LEFT)

        dataset_row = ttk.Frame(frame)
        dataset_row.pack(fill=tk.X, padx=10, pady=4)
        ttk.Label(dataset_row, text="Dataset:").pack(side=tk.LEFT)
        self.dataset_var = tk.StringVar(master=frame, value=self.dataset)
        self.dataset_entry = ttk.Entry(dataset_row, textvariable=self.dataset_var, width=24)
        self.dataset_entry.pack(side=tk.LEFT, padx=4)

        action = ttk.Frame(frame)
        action.pack(fill=tk.X, padx=10, pady=6)
        self.load_button = ttk.Button(action, text="Загрузить и распарсить", command=self.load_and_parse)
        self.load_button.pack(side=tk.LEFT)
        self.logt_button = ttk.Button(
            action, text="Загрузить в ClickHouse (LogT)", command=self.load_via_logt
        )
        self.logt_button.pack(side=tk.LEFT, padx=6)
        self.cancel_button = ttk.Button(
            action, text="Отменить", command=self.cancel_import, state=tk.DISABLED
        )
        self.cancel_button.pack(side=tk.LEFT)

        # P1-04: line counting makes the progress determinate at the cost of a
        # slower start.  The value is read on the Tk thread before the worker
        # starts, so toggling it mid-import cannot change the current run.
        self.count_lines_var = tk.BooleanVar(master=frame, value=True)
        self.count_lines_check = ttk.Checkbutton(
            frame,
            text="Точный прогресс (подсчёт строк, медленнее старт)",
            variable=self.count_lines_var,
        )
        self.count_lines_check.pack(anchor=tk.W, padx=10, pady=(2, 0))

        progress_row = ttk.Frame(frame)
        progress_row.pack(fill=tk.X, padx=10, pady=(6, 0))
        self.import_stage_label = ttk.Label(progress_row, text="Этап: —")
        self.import_stage_label.pack(side=tk.LEFT)
        self.import_count_label = ttk.Label(progress_row, text="Файлов: —")
        self.import_count_label.pack(side=tk.LEFT, padx=12)
        self.import_percent_label = ttk.Label(progress_row, text="")
        self.import_percent_label.pack(side=tk.RIGHT)

        self.progress = ttk.Progressbar(
            frame, mode="determinate", maximum=100, value=0
        )
        self.progress.pack(fill=tk.X, padx=10, pady=4)

        self.summary_label = ttk.Label(frame, text="", justify=tk.LEFT)
        self.summary_label.pack(anchor=tk.W, padx=10, pady=4)

        self._build_filter_row(frame)

    def _build_filter_row(self, parent: tk.Misc) -> None:
        """Build the shared filter rows, preset controls and hints.

        Args:
            parent: Container widget.
        """
        box = ttk.LabelFrame(parent, text="Фильтры (ClickHouse)")
        box.pack(fill=tk.X, padx=10, pady=(4, 8))

        row = ttk.Frame(box)
        row.pack(fill=tk.X, padx=4, pady=2)

        ttk.Label(row, text="Пользователь:").pack(side=tk.LEFT)
        self.filter_user_entry = ttk.Entry(row, width=16)
        self.filter_user_entry.pack(side=tk.LEFT, padx=(2, 8))

        ttk.Label(row, text="Событие:").pack(side=tk.LEFT)
        self.filter_event_entry = ttk.Entry(row, width=14)
        self.filter_event_entry.pack(side=tk.LEFT, padx=(2, 8))

        ttk.Label(row, text="Поиск:").pack(side=tk.LEFT)
        self.filter_search_entry = ttk.Entry(row, width=18)
        self.filter_search_entry.pack(side=tk.LEFT, padx=(2, 8))

        ttk.Label(row, text="С даты:").pack(side=tk.LEFT)
        self.filter_from_entry = ttk.Entry(row, width=12)
        self.filter_from_entry.pack(side=tk.LEFT, padx=(2, 8))
        self.filter_from_entry.insert(0, "ГГГГ-ММ-ДД")

        ttk.Label(row, text="По дату:").pack(side=tk.LEFT)
        self.filter_to_entry = ttk.Entry(row, width=12)
        self.filter_to_entry.pack(side=tk.LEFT, padx=(2, 8))

        ttk.Button(row, text="Применить", command=self.apply_filters).pack(side=tk.LEFT, padx=2)
        ttk.Button(row, text="Сбросить", command=self.clear_filters).pack(side=tk.LEFT, padx=2)

        # Second row: P2-05 extended filters.
        extended = ttk.Frame(box)
        extended.pack(fill=tk.X, padx=4, pady=2)

        ttk.Label(extended, text="Процесс:").pack(side=tk.LEFT)
        self.filter_process_entry = ttk.Entry(extended, width=20)
        self.filter_process_entry.pack(side=tk.LEFT, padx=(2, 8))

        ttk.Label(extended, text="AppID:").pack(side=tk.LEFT)
        self.filter_appid_entry = ttk.Entry(extended, width=24)
        self.filter_appid_entry.pack(side=tk.LEFT, padx=(2, 8))

        ttk.Label(extended, text="Мин. длительность, мс:").pack(side=tk.LEFT)
        self.filter_min_duration_entry = ttk.Entry(extended, width=10)
        self.filter_min_duration_entry.pack(side=tk.LEFT, padx=(2, 8))

        self.filter_hint_label = ttk.Label(
            box, text=FILTER_HINT, justify=tk.LEFT, wraplength=900
        )
        self.filter_hint_label.pack(anchor=tk.W, padx=4, pady=(2, 0))
        self.filter_scope_label = ttk.Label(
            box, text=FILTER_SCOPE_NOTE, justify=tk.LEFT, wraplength=900
        )
        self.filter_scope_label.pack(anchor=tk.W, padx=4, pady=(0, 2))

        # Preset row: save/load named filter selections (P2-04).
        preset_row = ttk.Frame(box)
        preset_row.pack(fill=tk.X, padx=4, pady=(2, 4))

        ttk.Label(preset_row, text="Пресет:").pack(side=tk.LEFT)
        self.preset_var = tk.StringVar(master=box, value="")
        self.preset_combo = ttk.Combobox(
            preset_row, textvariable=self.preset_var, state="readonly", width=28
        )
        self.preset_combo.pack(side=tk.LEFT, padx=(2, 8))
        ttk.Button(preset_row, text="Загрузить", command=self.load_preset).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(
            preset_row, text="Сохранить как…", command=self.save_preset
        ).pack(side=tk.LEFT, padx=2)
        ttk.Button(preset_row, text="Удалить", command=self.delete_preset).pack(
            side=tk.LEFT, padx=2
        )
        ttk.Button(
            preset_row, text="Сбросить все…", command=self.reset_presets
        ).pack(side=tk.LEFT, padx=2)

        self.preset_status_label = ttk.Label(box, text="", justify=tk.LEFT, wraplength=900)
        self.preset_status_label.pack(anchor=tk.W, padx=4, pady=(0, 2))

    def _source_text(self) -> str:
        """Describe the active data source for the UI."""
        config = self.clickhouse_config
        where = f"{config['host']}:{config['port']}/{config['database']}"
        if self.source == "clickhouse":
            return f"Источник: ClickHouse ({where}), dataset «{self.dataset}»"
        return f"Источник: память (ClickHouse: {where})"

    def _refresh_source_label(self) -> None:
        """Update the source label if it exists."""
        label = getattr(self, "source_label", None)
        if label is not None:
            try:
                label.config(text=self._source_text())
            except tk.TclError:  # pragma: no cover
                pass

    # -- folder selection --------------------------------------------------
    def select_folder(self) -> None:
        """Prompt for a log directory and count ``.log`` files."""
        folder = filedialog.askdirectory(title="Выберите папку с 1C логами")
        if not folder:
            return
        self.file_path = folder
        self.path_label.config(text=f"Путь: {folder}")
        try:
            log_files = list(Path(folder).glob("*.log"))
            if not log_files:
                nested = list(Path(folder).glob("**/*.log"))
                self.file_count_label.config(
                    text=f"Файлов: {len(nested)} (включая вложенные папки)"
                )
            else:
                self.file_count_label.config(text=f"Файлов: {len(log_files)}")
        except OSError as exc:
            self.file_count_label.config(text=f"Файлов: 0 (ошибка: {exc})")

    # -- import controls ---------------------------------------------------
    def _set_busy(self, busy: bool, action: str = "import") -> None:
        """Enable or disable load controls while work is running.

        Args:
            busy: ``True`` while a blocking operation is in flight.
            action: ``"import"`` when a cancellable import is running.
        """
        self.busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        for widget in (self.load_button, self.logt_button, self.select_button):
            try:
                widget.config(state=state)
            except tk.TclError:  # pragma: no cover
                pass
        try:
            self.dataset_entry.config(state=state)
        except tk.TclError:  # pragma: no cover
            pass
        try:
            self.cancel_button.config(
                state=tk.NORMAL if (busy and action == "import") else tk.DISABLED
            )
        except tk.TclError:  # pragma: no cover
            pass
        if busy:
            if action == "import":
                # P1-04: start from zero and stay determinate until the first
                # typed event decides whether the percentage is known.
                self._reset_import_progress()
            else:
                self._set_progress_indeterminate(True)
        else:
            self.progress.stop()

    def cancel_import(self) -> None:
        """Request cancellation of a running import."""
        if self.cancel_event is not None:
            self.cancel_event.set()
            try:
                # One request is enough; block the button until the worker
                # reports back, otherwise a double click only spams the flag.
                self.cancel_button.config(state=tk.DISABLED)
            except tk.TclError:  # pragma: no cover
                pass
            try:
                self.import_stage_label.config(text="Этап: Остановка LogT…")
            except tk.TclError:  # pragma: no cover
                pass
            self._set_import_status("Отмена запрошена…")

    # -- import progress ---------------------------------------------------
    def _reset_import_progress(self) -> None:
        """Return the progress widgets to the idle state (0 %, no stage)."""
        self.last_import_progress = None
        self._set_progress_indeterminate(False)
        try:
            self.progress.config(maximum=100, value=0)
            self.import_stage_label.config(text="Этап: —")
            self.import_count_label.config(text="Файлов: —")
            self.import_percent_label.config(text="")
        except tk.TclError:  # pragma: no cover
            pass

    def _set_progress_indeterminate(self, enabled: bool) -> None:
        """Switch the bar between the indeterminate and determinate modes.

        ``ttk.Progressbar`` refuses to change ``mode`` while the animation is
        running, so the animation is stopped first.  The operation is tolerant
        of a destroyed widget (shutdown).

        Args:
            enabled: ``True`` for a running indeterminate animation.
        """
        mode = "indeterminate" if enabled else "determinate"
        try:
            if str(self.progress.cget("mode")) != mode:
                self.progress.stop()
                self.progress.config(mode=mode)
            if enabled:
                self.progress.start(12)
            else:
                self.progress.stop()
        except tk.TclError:  # pragma: no cover
            pass

    def _apply_import_progress(self, progress: ImportProgress) -> None:
        """Render one typed progress snapshot on the load tab.

        Unknown percentages fall back to the indeterminate animation and the
        count label shows a dash.  Known values switch to determinate mode and
        scale the bar to ``total`` when the adapter supplied one, otherwise the
        percentage itself drives a 0..100 bar.

        Args:
            progress: Snapshot produced by the adapter.
        """
        title = clean_status_text(progress.message) or import_stage_title(progress.stage)
        try:
            if progress.percent is None:
                self._set_progress_indeterminate(True)
                self.import_count_label.config(text="Файлов: —")
                self.import_percent_label.config(text="")
            else:
                self._set_progress_indeterminate(False)
                total = progress.total
                if total is not None:
                    self.progress.config(maximum=total)
                    current = progress.current
                    if current is None:
                        current = int(round(total * progress.percent / 100.0))
                    self.progress.config(value=min(current, total))
                    if progress.current is not None:
                        count_text = f"Файлов: {progress.current}/{total}"
                    else:
                        count_text = "Файлов: —"
                else:
                    self.progress.config(maximum=100)
                    self.progress.config(value=progress.percent)
                    count_text = (
                        f"Файлов: {progress.current}"
                        if progress.current is not None
                        else "Файлов: —"
                    )
                self.import_count_label.config(text=count_text)
                self.import_percent_label.config(
                    text=format_import_percent(progress.percent)
                )
            self.import_stage_label.config(text=f"Этап: {title}")
        except tk.TclError:  # pragma: no cover - widget destroyed during shutdown
            return
        self._set_import_status(title)
        self.last_import_progress = progress

    def _update_import_progress(self, progress: ImportProgress) -> None:
        """Apply ``progress`` only while an import is running.

        Args:
            progress: Snapshot to render.
        """
        if not self.busy:
            return
        self._apply_import_progress(progress)

    def _mark_import_done(self) -> None:
        """Show the successful terminal state (100 %, «Готово»)."""
        self._set_progress_indeterminate(False)
        try:
            self.progress.config(maximum=100, value=100)
            self.import_stage_label.config(text="Этап: Готово")
            self.import_percent_label.config(text=format_import_percent(100.0))
        except tk.TclError:  # pragma: no cover
            pass
        self._set_import_status("Готово")

    def _mark_import_cancelled(self) -> None:
        """Stop the animation and show that the user cancelled the import."""
        self._set_progress_indeterminate(False)
        try:
            self.import_stage_label.config(text="Этап: Импорт отменён")
        except tk.TclError:  # pragma: no cover
            pass
        self._set_import_status("Импорт отменён.")

    def _mark_import_failed(self, stage: Any = None) -> None:
        """Stop the animation and mark the failing stage.

        Args:
            stage: Stage reached before the failure (``ImportStage``, its string
                value, or ``None`` when nothing was reported).
        """
        self._set_progress_indeterminate(False)
        title = import_stage_title(stage) if stage is not None else "Ошибка"
        try:
            self.import_stage_label.config(text=f"Этап: {title} — ошибка")
        except tk.TclError:  # pragma: no cover
            pass
        self._set_import_status("Ошибка импорта.")

    def _set_import_status(self, text: str) -> None:
        """Show a cleaned, single-line import status in the status bar.

        Args:
            text: Status text; log-level prefixes and line breaks are removed.
        """
        cleaned = clean_status_text(text)
        if cleaned:
            self.set_status(cleaned)

    def load_and_parse(self) -> None:
        """Parse logs into memory (legacy path; no ClickHouse writes)."""
        if not self.file_path:
            messagebox.showwarning("Внимание", "Сначала выберите папку с логами!")
            return
        if self.busy:
            return
        self._set_busy(True, action="parse")
        self.set_status("Загрузка (в память)…")

        def work() -> List[Dict[str, Any]]:
            return parse_log_directory(self.file_path)

        def runner() -> None:
            try:
                events = work()
            except Exception as exc:  # noqa: BLE001
                self.dispatcher.post("parse_error", str(exc))
                return
            self.dispatcher.post("parse_done", events)

        threading.Thread(target=runner, daemon=True, name="legacy-parse").start()

    def load_via_logt(self) -> None:
        """Import logs into ClickHouse through LogT, off the Tk main thread."""
        if not self.file_path:
            messagebox.showwarning("Внимание", "Сначала выберите папку с логами!")
            return
        if self.busy:
            return

        dataset = (self.dataset_var.get() or "").strip()
        if not dataset:
            messagebox.showwarning("Внимание", "Укажите dataset.")
            return
        try:
            logt_adapter.resolve_config(self.clickhouse_config)
        except logt_adapter.AdapterError as exc:
            messagebox.showerror("ClickHouse", str(exc))
            return

        # BUG-006 preflight: a frozen build has neither a Python interpreter nor
        # vendored LogT sources.  Fail before a thread, staging directory or
        # child process exists, so the user sees an actionable message instead
        # of a confusing import error.
        try:
            logt_adapter.check_logt_runtime()
        except logt_adapter.AdapterError as exc:
            messagebox.showerror("LogT", str(exc))
            return

        self.dataset = dataset
        self.cancel_event = threading.Event()
        # Read the checkbox on the Tk thread, before the worker starts: a
        # mid-import toggle must not change the running process.
        count_lines_for_progress = bool(self.count_lines_var.get())
        self._set_busy(True, action="import")
        self.set_status("Импорт в ClickHouse…")
        self._import_stage = "Подготовка"
        self._import_log_path = None

        client = ClickHouseClient(self.clickhouse_config)
        log_dir = self.file_path
        cancel_event = self.cancel_event
        started_at = time.monotonic()
        # The stage lives in a mutable dict because ``work`` runs on the worker
        # thread; ``runner`` reads it there and builds the failure, so no Tk
        # state is touched off the main thread.
        stage = {"name": "Подготовка"}
        password = self.clickhouse_config.get("password")

        def post_progress(progress: ImportProgress) -> None:
            self.dispatcher.post("import_progress", progress)

        def work() -> ImportResult:
            # SCHEMA is a GUI-side stage: the adapter knows nothing about the
            # idempotent schema check that precedes the child process.
            post_progress(
                ImportProgress(ImportStage.SCHEMA, "Проверка схемы ClickHouse…")
            )
            # BUG-004: make an empty/incomplete database usable without a
            # separate manual schema step.  ``ensure_schema`` is idempotent and
            # never drops anything; a mismatch is reported, not repaired.
            stage["name"] = "Подготовка схемы ClickHouse"
            status = client.ensure_schema()
            if not status.ready:
                raise logt_adapter.AdapterError(
                    f"Схема ClickHouse не готова: {status.message}"
                )
            stage["name"] = "Импорт LogT"
            return client.import_logs(
                log_dir,
                dataset,
                cancel_event,
                # Legacy raw LogT lines stay in the log: they may contain
                # carriage returns and must not leak into the status bar.
                progress=lambda message: logger.debug("LogT: %s", message),
                on_progress=post_progress,
                count_lines_for_progress=count_lines_for_progress,
            )

        def runner() -> None:
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001
                # The journal is searched here, on the worker thread: the GUI
                # thread must not walk directories (P1-05).
                failure = ImportFailure(
                    stage=stage["name"],
                    error=logt_adapter.redact(exc, password),
                    dataset=dataset,
                    log_dir=log_dir,
                    log_path=find_latest_logt_log(log_dir),
                    exit_code=-1,
                    duration=time.monotonic() - started_at,
                )
                self.dispatcher.post("import_failed", failure)
                return
            self._import_stage = stage["name"]
            self._import_log_path = find_latest_logt_log(log_dir)
            self.dispatcher.post("import_done", result)

        self.import_thread = threading.Thread(target=runner, daemon=True, name="logt-import")
        self.import_thread.start()

    def _on_import_progress(self, payload: Any) -> None:
        """Apply import progress on the Tk main thread.

        Args:
            payload: An :class:`ImportProgress` snapshot.  A plain string is
                accepted for backwards compatibility with test stubs and older
                adapters; it is cleaned and shown in the status bar only.
        """
        if isinstance(payload, ImportProgress):
            self.last_import_progress = payload
            if not self.busy:
                # A late event must not repaint a finished or failed import.
                logger.debug("Ignoring late import progress: %s", payload.message)
                return
            self._update_import_progress(payload)
            return

        text = clean_status_text(payload)
        if not text:
            return
        logger.debug("Legacy import progress: %s", text)
        if not self.busy:
            return
        self._set_import_status(text)

    def _on_import_done(self, result: ImportResult) -> None:
        """Handle import completion on the main thread."""
        self._set_busy(False)
        self.cancel_event = None
        if result.cancelled:
            self._mark_import_cancelled()
            self.summary_label.config(text="Импорт отменён пользователем.")
            return
        if not result.success:
            self._mark_import_failed(
                self.last_import_progress.stage if self.last_import_progress else None
            )
            failure = ImportFailure.from_result(
                result,
                stage=self._import_stage,
                dataset=self.dataset,
                log_dir=self.file_path,
                log_path=self._import_log_path,
            )
            self._show_import_error(failure)
            return

        self._mark_import_done()
        # ClickHouse is now the source of truth for every tab.
        self.source = "clickhouse"
        self.dataset = (self.dataset_var.get() or self.dataset).strip()
        self.error_page = 0
        # Scope every tab to the dataset that was just imported, otherwise the
        # views would mix in rows from unrelated datasets.
        self.filters = self.current_filters()
        self._refresh_source_label()
        counts = result.table_counts or {}
        breakdown = ", ".join(f"{name}={value}" for name, value in counts.items())
        summary = (
            f"Импорт завершён за {result.duration:.1f} с.\n"
            f"Dataset: {self.dataset}\n"
            f"Событий в ClickHouse: {result.rows_loaded}\n"
            f"Таблицы: {breakdown}"
        )
        self.summary_label.config(text=summary)
        self.set_status(f"Готово. Импортировано строк: {result.rows_loaded}")
        messagebox.showinfo(
            "Готово",
            f"Логи загружены в ClickHouse.\n\nDataset: {self.dataset}\n"
            f"Событий: {result.rows_loaded}\nВремя: {result.duration:.1f} с",
        )
        self.refresh_all_tabs()

    def _on_import_failed(self, failure: ImportFailure) -> None:
        """Handle an import that raised on the worker thread (main thread).

        Args:
            failure: Failure built by the worker in ``load_via_logt``.
        """
        self._show_import_error(failure)

    def _show_import_error(self, failure: ImportFailure) -> None:
        """Report a failed import without a modal message box.

        The report is a non-modal window so the user can read, copy and act on
        the diagnostics while the application stays usable.  Import failures
        never use ``messagebox.showerror``; every other error path keeps its
        existing dialog.

        Args:
            failure: Structured description of the failure.
        """
        self._import_stage = failure.stage or self._import_stage
        logger.error("LogT import failed at stage %r: %s", failure.stage, failure.error)
        password = self.clickhouse_config.get("password")
        logger.debug(
            "Import failure details:\n%s",
            logt_adapter.redact(format_import_failure(failure), password),
        )

        self._set_busy(False)
        self.cancel_event = None
        stage_text = failure.stage or "неизвестный этап"
        self.set_status(f"Ошибка импорта: {stage_text}.")
        self.summary_label.config(
            text=f"Ошибка импорта ({stage_text}): {failure.error[:300]}"
        )
        self._close_import_error_dialog()
        try:
            self._import_error_dialog = ImportErrorDialog(
                self.root,
                failure,
                redact=lambda text: logt_adapter.redact(text, password),
            )
        except tk.TclError:  # pragma: no cover - window already destroyed
            logger.exception("Could not show the import error dialog")

    def _close_import_error_dialog(self) -> None:
        """Close the previous failure report, if one is open."""
        dialog = self._import_error_dialog
        self._import_error_dialog = None
        if dialog is None:
            return
        try:
            dialog.close()
        except tk.TclError:  # pragma: no cover - already destroyed
            pass

    def on_parse_complete(self, events: List[Dict[str, Any]]) -> None:
        """Render legacy in-memory parse results.

        Args:
            events: Parsed events.
        """
        self.events = events
        self._memory_cache = None
        self.source = "memory"
        self.error_page = 0
        self._set_busy(False)
        self._refresh_source_label()
        self.set_status(f"Загружено {len(events)} событий")
        format_type = detect_format(self.file_path) if self.file_path else "unknown"
        self.summary_label.config(text=f"Формат: {format_type}\nСобытий: {len(events)}")
        self.refresh_overview()
        self.refresh_errors()
        self.refresh_performance()
        self.refresh_users()

    def on_parse_error(self, error_msg: str) -> None:
        """Report a failure to the user.

        Args:
            error_msg: Message to display.
        """
        logger.error("In-memory parse failed: %s", error_msg)
        self.progress.stop()
        self._set_busy(False)
        self.set_status(f"Ошибка: {error_msg}")
        self.summary_label.config(text=f"Ошибка: {error_msg}")
        messagebox.showerror("Ошибка", error_msg)

    # -- connection + schema ----------------------------------------------
    def open_connection_settings(self) -> None:
        """Edit ClickHouse connection settings in a modal dialog."""
        dialog = ConnectionDialog(self.root, self.clickhouse_config)
        self.root.wait_window(dialog.window)
        if dialog.result is not None:
            self.clickhouse_config = dialog.result
            self.ch_client = ClickHouseClient(self.clickhouse_config)
            self._refresh_source_label()
            self.set_status("Настройки подключения обновлены.")
            self.check_connection_async()

    def check_connection_async(self) -> None:
        """Test connectivity on a worker thread."""
        self.set_status("Проверка подключения…")
        client = ClickHouseClient(self.clickhouse_config)
        self.run_async(client.test, "connection")

    def validate_schema_async(self) -> None:
        """Validate the schema on a worker thread (read-only)."""
        self.set_status("Проверка схемы…")
        client = ClickHouseClient(self.clickhouse_config)
        self.run_async(client.validate_schema, "schema")

    def reset_schema_dialog(self) -> None:
        """Ask for explicit confirmation, then reset the schema.

        This is the only destructive path in the application and it is never
        reached automatically: the menu item and this confirmation are required.
        """
        config = self.clickhouse_config
        answer = messagebox.askyesno(
            "Сбросить схему ClickHouse",
            "Это УДАЛИТ все таблицы LogT в базе "
            f"«{config['database']}» на {config['host']}:{config['port']}:\n"
            "  operations, events, event_stats, calls\n\n"
            "Все загруженные ранее данные будут потеряны без возможности восстановления.\n\n"
            "Продолжить?",
            icon=messagebox.WARNING,
            default=messagebox.NO,
        )
        if not answer:
            self.set_status("Сброс схемы отменён.")
            return
        typed = simpledialog.askstring(
            "Подтверждение",
            "Введите слово RESET (заглавными), чтобы подтвердить удаление:",
            parent=self.root,
        )
        if typed != "RESET":
            self.set_status("Сброс схемы отменён (подтверждение не получено).")
            return

        client = ClickHouseClient(self.clickhouse_config)
        self.set_status("Сброс схемы…")

        def work() -> SchemaStatus:
            return logt_adapter.reset_schema(self.clickhouse_config, confirm=True)

        self.run_async(work, "schema")

    def _on_connection(self, reachable: bool) -> None:
        """Report the connectivity probe result."""
        if reachable:
            self.set_status("ClickHouse доступен.")
        else:
            config = self.clickhouse_config
            self.set_status("ClickHouse недоступен.")
            messagebox.showerror(
                "ClickHouse недоступен",
                f"Не удалось подключиться к {config['host']}:{config['port']}.\n\n"
                "Проверьте, что сервер запущен и настройки подключения верны.\n"
                "См. docs/CLICKHOUSE.md.",
            )

    def _on_schema(self, status: SchemaStatus) -> None:
        """Report the schema validation/creation result."""
        self.schema_status = status
        if status.ready:
            self.set_status(status.message[:200])
            if status.created:
                messagebox.showinfo("Схема ClickHouse", status.message)
            elif status.warnings:
                self.set_status("Схема готова (есть примечания: см. статус).")
        else:
            self.set_status("Схема не соответствует ожидаемой.")
            messagebox.showerror("Схема ClickHouse", status.message)

    def _on_status(self, payload: Any) -> None:
        """Handle miscellaneous status messages."""
        if payload == "memory_parsed":
            return

    def _on_error(self, message: str) -> None:
        """Show an error raised on a worker thread."""
        logger.error("Background task failed: %s", message)
        self._set_busy(False)
        self.set_status("Ошибка.")
        messagebox.showerror("Ошибка ClickHouse", str(message)[:1500])

    # ==================================================================
    # Filters
    # ==================================================================
    def current_filters(self) -> Filters:
        """Build a :class:`Filters` value from the filter widgets."""
        def text(entry) -> Optional[str]:
            try:
                value = entry.get().strip()
            except tk.TclError:  # pragma: no cover
                return None
            return value or None

        def parse_date(value: Optional[str]) -> Optional[datetime]:
            if not value:
                return None
            for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
                try:
                    return datetime.strptime(value, fmt)
                except ValueError:
                    continue
            return None

        def parse_duration(value: Optional[str]) -> Optional[int]:
            if not value:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        return Filters(
            dataset=self.dataset if self.source == "clickhouse" else None,
            date_from=parse_date(text(self.filter_from_entry)),
            date_to=parse_date(text(self.filter_to_entry)),
            user=text(self.filter_user_entry),
            event_name=text(self.filter_event_entry),
            search=text(self.filter_search_entry),
            process_name=text(self.filter_process_entry),
            app_id=text(self.filter_appid_entry),
            min_duration_ms=parse_duration(text(self.filter_min_duration_entry)),
        ).normalized()

    def apply_filters(self) -> None:
        """Apply the current filter widgets and refresh every tab."""
        self.filters = self.current_filters()
        self.error_page = 0
        self.set_status("Фильтры применены.")
        self.refresh_all_tabs()

    def clear_filters(self) -> None:
        """Reset the filter widgets and refresh."""
        for entry in (
            self.filter_user_entry,
            self.filter_event_entry,
            self.filter_search_entry,
            self.filter_from_entry,
            self.filter_to_entry,
            self.filter_process_entry,
            self.filter_appid_entry,
            self.filter_min_duration_entry,
        ):
            try:
                entry.delete(0, tk.END)
            except tk.TclError:  # pragma: no cover
                pass
        self.filters = self.current_filters()
        self.error_page = 0
        self.refresh_all_tabs()

    # -- presets -----------------------------------------------------------
    def _init_presets(self) -> None:
        """Create the preset store, load it and restore the last used preset.

        The widgets are restored without a query: startup must stay offline and
        the user decides when to apply the filters.
        """
        self.preset_store = presets.PresetStore()
        try:
            self.preset_store.load()
        except presets.PresetsError as exc:  # pragma: no cover - corrupt file details
            logger.warning("Could not load presets: %s", exc)

        self._refresh_presets()
        error = self.preset_store.last_error
        if error:
            self._set_preset_status(error)

        last_used = self.preset_store.get_last_used()
        if not last_used:
            return
        preset = self.preset_store.get(last_used)
        if preset is None:
            return
        try:
            kwargs = presets.dict_to_filter_kwargs(preset.get("filters", {}))
        except presets.PresetsError as exc:  # pragma: no cover - validated on load
            logger.warning("Could not restore preset %r: %s", last_used, exc)
            return
        self._fill_filter_widgets(kwargs)
        self.preset_var.set(last_used)
        self._set_preset_status(f"Восстановлен пресет: {last_used}")

    def _refresh_presets(self) -> None:
        """Reload the combobox values from the store."""
        if self.preset_store is None:  # pragma: no cover - called before init
            return
        names = self.preset_store.names()
        self.preset_combo["values"] = names
        selected = self.preset_var.get().strip()
        if selected and selected in names:
            return
        fallback = self.preset_store.get_last_used()
        if fallback in names:
            self.preset_var.set(fallback)
        elif names:
            self.preset_var.set(names[0])
        else:
            self.preset_var.set("")

    def _set_preset_status(self, message: str) -> None:
        """Show ``message`` next to the preset controls."""
        try:
            self.preset_status_label.config(text=str(message)[:500])
        except tk.TclError:  # pragma: no cover - during shutdown
            pass

    def _fill_filter_widgets(self, kwargs: Dict[str, Any]) -> None:
        """Populate every filter entry from ``Filters``-style keyword arguments.

        Args:
            kwargs: Mapping produced by :func:`presets.dict_to_filter_kwargs`.
        """
        text_values = {
            "user": kwargs.get("user"),
            "event_name": kwargs.get("event_name"),
            "search": kwargs.get("search"),
            "process_name": kwargs.get("process_name"),
            "app_id": kwargs.get("app_id"),
        }
        entries = {
            "user": self.filter_user_entry,
            "event_name": self.filter_event_entry,
            "search": self.filter_search_entry,
            "process_name": self.filter_process_entry,
            "app_id": self.filter_appid_entry,
        }
        for field, entry in entries.items():
            value = text_values.get(field)
            self._set_entry(entry, "" if value is None else str(value))

        duration = kwargs.get("min_duration_ms")
        self._set_entry(
            self.filter_min_duration_entry, "" if duration is None else str(duration)
        )

        for field, entry in (
            ("date_from", self.filter_from_entry),
            ("date_to", self.filter_to_entry),
        ):
            moment = kwargs.get(field)
            if isinstance(moment, datetime):
                self._set_entry(entry, moment.strftime("%Y-%m-%d"))
            elif moment is None:
                self._set_entry(entry, "")
            else:
                self._set_entry(entry, str(moment))

    @staticmethod
    def _set_entry(entry: ttk.Entry, value: str) -> None:
        """Replace the contents of ``entry`` with ``value``."""
        try:
            entry.delete(0, tk.END)
            if value:
                entry.insert(0, value)
        except tk.TclError:  # pragma: no cover - during shutdown
            pass

    def save_preset(self) -> None:
        """Prompt for a name and persist the current filter selection."""
        if self.preset_store is None:  # pragma: no cover - not initialised
            return
        name = simpledialog.askstring(
            "Сохранить пресет", "Имя пресета:", parent=self.root
        )
        if not name:
            return
        try:
            self.preset_store.save_preset(name, self.current_filters())
            self.preset_store.set_last_used(name)
        except presets.PresetsError as exc:
            messagebox.showerror("Пресеты", str(exc))
            self._set_preset_status("Не удалось сохранить пресет.")
            return
        clean = name.strip()
        self._refresh_presets()
        self.preset_var.set(clean)
        self._set_preset_status(f"Пресет сохранён: {clean}")

    def load_preset(self) -> None:
        """Restore the selected preset into the filter widgets and apply it."""
        if self.preset_store is None:  # pragma: no cover - not initialised
            return
        name = self.preset_var.get().strip()
        if not name:
            self._set_preset_status("Пресет не выбран.")
            return
        preset = self.preset_store.get(name)
        if preset is None:
            self._set_preset_status(f"Пресет не найден: {name}")
            return
        try:
            kwargs = presets.dict_to_filter_kwargs(preset.get("filters", {}))
        except presets.PresetsError as exc:
            messagebox.showerror("Пресеты", str(exc))
            return

        self._fill_filter_widgets(kwargs)
        self.apply_filters()
        try:
            self.preset_store.set_last_used(name)
        except presets.PresetsError as exc:  # pragma: no cover - read-only store
            logger.warning("Could not remember the last preset: %s", exc)
        self._set_preset_status(f"Пресет загружен: {name}")

    def delete_preset(self) -> None:
        """Delete the selected preset after confirmation."""
        if self.preset_store is None:  # pragma: no cover - not initialised
            return
        name = self.preset_var.get().strip()
        if not name:
            self._set_preset_status("Пресет не выбран.")
            return
        if not messagebox.askyesno(
            "Удалить пресет", f"Удалить пресет «{name}»?"
        ):
            return
        try:
            removed = self.preset_store.delete_preset(name)
        except presets.PresetsError as exc:
            messagebox.showerror("Пресеты", str(exc))
            return
        self.preset_var.set("")
        self._refresh_presets()
        if removed:
            self._set_preset_status(f"Пресет удалён: {name}")
        else:
            self._set_preset_status(f"Пресет не найден: {name}")

    def reset_presets(self) -> None:
        """Delete every preset after two confirmations."""
        if self.preset_store is None:  # pragma: no cover - not initialised
            return
        if not messagebox.askyesno(
            "Сбросить пресеты", "Удалить все сохранённые пресеты?"
        ):
            return
        if not messagebox.askyesno(
            "Подтверждение",
            "Все пресеты будут удалены без возможности восстановления. Продолжить?",
        ):
            return
        try:
            self.preset_store.reset()
        except presets.PresetsError as exc:
            messagebox.showerror("Пресеты", str(exc))
            return
        self.preset_var.set("")
        self._refresh_presets()
        self._set_preset_status("Пресеты сброшены.")

    # ==================================================================
    # Refresh orchestration
    # ==================================================================
    def refresh_all_tabs(self) -> None:
        """Refresh all four data tabs from the active source."""
        self.refresh_overview()
        self.refresh_errors()
        self.refresh_performance()
        self.refresh_users()

    def _memory_data(self) -> tuple:
        """Return cached in-memory aggregates for the current event list.

        The caller may be a worker thread (PERF-003); the lock guarantees that
        concurrent tab refreshes compute the aggregates once and share them
        instead of racing (PERF-004).

        Returns:
            ``(events, event_counts, user_counts, errors)``.  The cache is
            rebuilt when ``self.events`` is replaced.
        """
        events = self.events
        cache = self._memory_cache
        if cache is not None and cache[0] is events:
            return cache

        with self._memory_lock:
            cache = self._memory_cache
            if cache is None or cache[0] is not events:
                events = self.events
                cache = (
                    events,
                    aggregate_by_event(events),
                    aggregate_by_user(events),
                    find_errors(events),
                )
                self._memory_cache = cache
        return cache

    def _on_tab_changed(self, event: Any = None) -> None:
        """Lazily refresh a tab the first time it is shown."""
        try:
            index = self.notebook.index(self.notebook.select())
            title = self.notebook.tab(index, "text")
        except tk.TclError:  # pragma: no cover
            return
        if self.source != "clickhouse":
            return
        mapping = {
            "Обзор": self.refresh_overview,
            "Ошибки": self.refresh_errors,
            "Производительность": self.refresh_performance,
            "По пользователям": self.refresh_users,
        }
        callback = mapping.get(title)
        if callback is not None:
            callback()

    # ==================================================================
    # TAB 2: Обзор
    # ==================================================================
    def create_overview_tab(self) -> None:
        """Build the overview tab."""
        frame = ttk.Frame(self.notebook)
        self.overview_frame = frame

        summary_frame = ttk.LabelFrame(frame, text="Статистика")
        summary_frame.pack(fill=tk.X, padx=10, pady=5)
        self.overview_stats_label = ttk.Label(summary_frame, text="Нет данных", justify=tk.LEFT)
        self.overview_stats_label.pack(anchor=tk.W, padx=6, pady=2)
        self.overview_range_label = ttk.Label(summary_frame, text="", justify=tk.LEFT)
        self.overview_range_label.pack(anchor=tk.W, padx=6, pady=2)

        tree_frame = ttk.Frame(frame)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        columns = ("Event", "Count", "TotalMs", "MaxMs")
        self.event_tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=8)
        for column, title, width in (
            ("Event", "Тип события", 160),
            ("Count", "Количество", 90),
            ("TotalMs", "Сумма, мс", 100),
            ("MaxMs", "Макс, мс", 90),
        ):
            self.event_tree.heading(column, text=title)
            self.event_tree.column(column, width=width)
        self._attach_scrollbar(tree_frame, self.event_tree)

        user_frame = ttk.Frame(frame)
        user_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        user_columns = ("User", "Count", "Errors")
        self.user_tree = ttk.Treeview(user_frame, columns=user_columns, show="headings", height=6)
        for column, title, width in (
            ("User", "Пользователь", 200),
            ("Count", "Событий", 90),
            ("Errors", "Ошибок", 90),
        ):
            self.user_tree.heading(column, text=title)
            self.user_tree.column(column, width=width)
        self._attach_scrollbar(user_frame, self.user_tree)

    def _attach_scrollbar(self, parent: tk.Misc, tree: ttk.Treeview) -> None:
        """Pack a treeview with a vertical scrollbar.

        Args:
            parent: Container widget.
            tree: Treeview to attach.
        """
        scrollbar = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def refresh_overview(self) -> None:
        """Refresh the overview tab from ClickHouse or memory.

        In memory mode the aggregation runs on a worker thread (PERF-003); the
        result is rendered on the Tk main thread by :meth:`_on_memory_overview`.
        """
        if self.source != "clickhouse":
            if not self.events:
                return
            self.run_async(self._memory_overview_payload, "memory_overview")
            return

        client = ClickHouseClient(self.clickhouse_config)
        filters = self.filters
        self.run_async(lambda: client.overview(filters), "overview")

    def _memory_overview_payload(self) -> Dict[str, Any]:
        """Build the overview payload from ``self.events`` on a worker thread."""
        events, event_counts, user_counts, _ = self._memory_data()
        timestamps = [event.get("timestamp", "") for event in events if event.get("timestamp")]
        return {
            "count": len(events),
            "event_counts": event_counts,
            "user_counts": user_counts,
            "first": min(timestamps) if timestamps else "",
            "last": max(timestamps) if timestamps else "",
        }

    def _on_memory_overview(self, payload: Dict[str, Any]) -> None:
        """Render the in-memory overview payload on the main thread."""
        self.overview_stats_label.config(
            text=f"Событий: {payload.get('count', 0)} (в памяти)"
        )
        self.overview_range_label.config(
            text=f"Первое: {payload.get('first', '')}    "
                 f"Последнее: {payload.get('last', '')}"
        )

        self.event_tree.delete(*self.event_tree.get_children())
        for name, count in sorted(
            payload.get("event_counts", {}).items(),
            key=lambda item: item[1],
            reverse=True,
        )[:10]:
            self.event_tree.insert("", tk.END, values=(name, count, "", ""))

        self.user_tree.delete(*self.user_tree.get_children())
        for user, count in sorted(
            payload.get("user_counts", {}).items(),
            key=lambda item: item[1],
            reverse=True,
        )[:10]:
            self.user_tree.insert("", tk.END, values=(user, count, ""))

    def _on_overview(self, payload: Dict[str, Any]) -> None:
        """Render the ClickHouse overview payload."""
        totals = payload.get("totals", {})
        time_range = payload.get("time_range", {})
        self.overview_stats_label.config(
            text=(
                f"Событий: {totals.get('events', 0)}    "
                f"Пользователей: {totals.get('users', 0)}    "
                f"Типов событий: {totals.get('event_types', 0)}    "
                f"Ошибок: {totals.get('errors', 0)}    "
                f"Dataset'ов: {totals.get('datasets', 0)}"
            )
        )
        self.overview_range_label.config(
            text=(
                f"Первое: {time_range.get('first', '')}    "
                f"Последнее: {time_range.get('last', '')}"
            )
        )

        self.event_tree.delete(*self.event_tree.get_children())
        for row in payload.get("top_events", []):
            self.event_tree.insert(
                "", tk.END,
                values=(row["name"], row["count"], row["total_ms"], row["max_ms"]),
            )

        self.user_tree.delete(*self.user_tree.get_children())
        for row in payload.get("top_users", []):
            self.user_tree.insert("", tk.END, values=(row["user"], row["count"], row["errors"]))

    # ==================================================================
    # TAB 3: Ошибки
    # ==================================================================
    def create_errors_tab(self) -> None:
        """Build the errors tab with pagination controls."""
        frame = ttk.Frame(self.notebook)
        self.errors_frame = frame

        search_frame = ttk.Frame(frame)
        search_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Label(search_frame, text="Поиск:").pack(side=tk.LEFT)
        self.search_entry = ttk.Entry(search_frame, width=40)
        self.search_entry.pack(side=tk.LEFT, padx=5)
        self.search_entry.bind("<Return>", self.on_search_errors)

        self.error_count_label = ttk.Label(frame, text="Ошибок: 0")
        self.error_count_label.pack(pady=2)

        pager = ttk.Frame(frame)
        pager.pack(fill=tk.X, padx=10)
        self.prev_button = ttk.Button(pager, text="◀ Назад", command=self.error_prev_page)
        self.prev_button.pack(side=tk.LEFT)
        self.next_button = ttk.Button(pager, text="Вперёд ▶", command=self.error_next_page)
        self.next_button.pack(side=tk.LEFT, padx=4)
        self.page_label = ttk.Label(pager, text="Стр. 1")
        self.page_label.pack(side=tk.LEFT, padx=8)

        tree_frame = ttk.Frame(frame)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        columns = ("Time", "Event", "Db", "User", "Description")
        self.error_tree = ttk.Treeview(tree_frame, columns=columns, show="headings")
        for column, title, width in (
            ("Time", "Время", 140),
            ("Event", "Тип", 80),
            ("Db", "База", 110),
            ("User", "Пользователь", 120),
            (columns[4], DESCRIPTION_COLUMN_LABEL, 320),
        ):
            self.error_tree.heading(column, text=title)
            self.error_tree.column(column, width=width)
        self._attach_scrollbar(tree_frame, self.error_tree)
        self.error_tree.bind("<Double-1>", self.on_error_double_click)

    def refresh_errors(self) -> None:
        """Refresh the errors tab (paginated; ClickHouse or memory).

        Memory mode aggregates and slices the error list on a worker thread
        (PERF-003) and only inserts the current page into the tree.
        """
        if self.source != "clickhouse":
            if not self.events:
                return
            self.run_async(self._memory_errors_payload, "memory_errors")
            return
        client = ClickHouseClient(self.clickhouse_config)
        filters = self.filters
        page, size = self.error_page, self.error_page_size
        self.run_async(lambda: client.errors(filters, page, size), "errors")

    def _memory_errors_payload(self) -> Dict[str, Any]:
        """Return one page of in-memory errors in the ClickHouse row shape."""
        errors = self._memory_data()[3]
        total = len(errors)
        size = self.error_page_size
        pages = max(1, (total + size - 1) // size)
        page = min(max(0, self.error_page), pages - 1)
        start = page * size
        rows = [
            {
                "time": error.get("timestamp", "") or "",
                "event": error.get("name", "") or "",
                "db_name": error.get("process", "") or "",
                "user": error.get("user", "") or "",
                "description": error.get("descr", "") or "",
                "description_is_excerpt": False,
            }
            for error in errors[start:start + size]
        ]
        return {"rows": rows, "total": total, "page": page, "page_size": size}

    def _on_memory_errors(self, payload: Dict[str, Any]) -> None:
        """Render one page of in-memory errors."""
        self._render_errors_page(payload, description_note="")

    def _on_errors(self, payload: Dict[str, Any]) -> None:
        """Render one page of ClickHouse errors."""
        self._render_errors_page(
            payload,
            description_note=(
                "описание берётся из текста события; «…» — сокращено до 200 "
                "символов, в схеме LogT отдельной колонки нет"
            ),
        )

    def _render_errors_page(
        self,
        payload: Dict[str, Any],
        description_note: str = "",
    ) -> None:
        """Render one page of error rows shared by both data sources."""
        rows = payload.get("rows", [])
        total = int(payload.get("total", 0))
        page = int(payload.get("page", 0))
        size = int(payload.get("page_size", self.error_page_size)) or 1
        self.error_total = total
        self.error_page = page
        self.error_page_size = size

        self.error_tree.delete(*self.error_tree.get_children())
        for row in rows:
            self.error_tree.insert(
                "", tk.END,
                values=(
                    row.get("time", ""),
                    row.get("event", ""),
                    row.get("db_name", ""),
                    row.get("user", ""),
                    format_description(row),
                ),
            )

        pages = max(1, (total + size - 1) // size)
        note = f"; {description_note}" if description_note else ""
        self.error_count_label.config(
            text=f"Ошибок: {total}  (показано {len(rows)}{note})"
        )
        self.page_label.config(text=f"Стр. {page + 1} из {pages}")
        self.prev_button.config(state=tk.NORMAL if page > 0 else tk.DISABLED)
        self.next_button.config(state=tk.NORMAL if page + 1 < pages else tk.DISABLED)

    def error_next_page(self) -> None:
        """Load the next page of errors."""
        if (self.error_page + 1) * self.error_page_size < self.error_total:
            self.error_page += 1
            self.refresh_errors()

    def error_prev_page(self) -> None:
        """Load the previous page of errors."""
        if self.error_page > 0:
            self.error_page -= 1
            self.refresh_errors()

    def on_search_errors(self, event: Any = None) -> None:
        """Apply the search entry as a filter (parameterized query)."""
        text = ""
        try:
            text = self.search_entry.get().strip()
        except tk.TclError:  # pragma: no cover
            pass
        try:
            self.filter_search_entry.delete(0, tk.END)
            if text:
                self.filter_search_entry.insert(0, text)
        except tk.TclError:  # pragma: no cover
            pass
        self.error_page = 0
        self.filters = self.current_filters()
        self.refresh_errors()

    def on_error_double_click(self, event: Any = None) -> None:
        """Show the reference description for the selected event code."""
        selection = self.error_tree.selection()
        if not selection:
            return
        values = self.error_tree.item(selection[0], "values")
        if len(values) < 2:
            return
        code = values[1]
        entry = get_event(code)
        if not entry:
            messagebox.showinfo("Событие", f"Код: {code}\nНет описания в справочнике.")
            return
        messagebox.showinfo(
            "Событие",
            f"{entry['code']} — {entry.get('title', '')}\n\n{entry.get('description', '')}",
        )

    # ==================================================================
    # TAB 4: Производительность
    # ==================================================================
    def create_performance_tab(self) -> None:
        """Build the performance tab."""
        frame = ttk.Frame(self.notebook)
        self.performance_frame = frame

        top_n_frame = ttk.LabelFrame(frame, text="Топ N самых долгих событий")
        top_n_frame.pack(fill=tk.X, padx=10, pady=5)
        ttk.Label(top_n_frame, text="N:").pack(side=tk.LEFT)
        self.top_n_spinbox = ttk.Spinbox(top_n_frame, from_=1, to=500, width=8)
        self.top_n_spinbox.set(20)
        self.top_n_spinbox.pack(side=tk.LEFT, padx=5)
        ttk.Button(top_n_frame, text="Обновить", command=self.refresh_performance).pack(
            side=tk.LEFT, padx=5
        )

        tree_frame = ttk.Frame(frame)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        columns = ("Time", "Event", "DurationMs", "Db", "User", "Context")
        self.performance_tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=10)
        for column, title, width in (
            ("Time", "Время", 140),
            ("Event", "Тип события", 110),
            ("DurationMs", "Длительность, мс", 120),
            ("Db", "База", 110),
            ("User", "Пользователь", 110),
            ("Context", "Контекст", 240),
        ):
            self.performance_tree.heading(column, text=title)
            self.performance_tree.column(column, width=width)
        self._attach_scrollbar(tree_frame, self.performance_tree)

        agg_frame = ttk.LabelFrame(frame, text="Агрегаты по типам событий (count/sum/avg/max/p95)")
        agg_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        agg_columns = ("Event", "Count", "SumMs", "AvgMs", "MaxMs", "P95Ms")
        self.performance_agg_tree = ttk.Treeview(
            agg_frame, columns=agg_columns, show="headings", height=6
        )
        for column, title, width in (
            ("Event", "Тип события", 130),
            ("Count", "Кол-во", 70),
            ("SumMs", "Сумма, мс", 100),
            ("AvgMs", "Среднее, мс", 110),
            ("MaxMs", "Макс, мс", 95),
            ("P95Ms", "p95, мс", 95),
        ):
            self.performance_agg_tree.heading(column, text=title)
            self.performance_agg_tree.column(column, width=width)
        self._attach_scrollbar(agg_frame, self.performance_agg_tree)

    def refresh_performance(self) -> None:
        """Refresh the performance tab from ClickHouse or memory.

        The memory-mode scan runs on a worker thread (PERF-003); the spinbox is
        read on the Tk thread before dispatching.
        """
        try:
            limit = int(self.top_n_spinbox.get())
        except (ValueError, tk.TclError):
            limit = 20
        limit = max(1, min(limit, logt_adapter.MAX_PAGE_SIZE))

        if self.source != "clickhouse":
            if not self.events:
                return
            self.run_async(
                lambda: self._memory_performance_payload(limit), "memory_performance"
            )
            return

        client = ClickHouseClient(self.clickhouse_config)
        filters = self.filters
        self.run_async(lambda: client.performance(filters, limit), "performance")

    def _memory_performance_payload(self, limit: int) -> Dict[str, Any]:
        """Return the slowest in-memory events in the ClickHouse row shape."""
        slowest = [
            {
                "time": item.get("timestamp", "") or "",
                "event": item.get("name", "") or "",
                "duration_ms": f"{float(item.get('duration', 0) or 0):.2f}",
                "db_name": item.get("process", "") or "",
                "user": item.get("user", "") or "",
                "context": "",
            }
            for item in top_slowest(self.events, limit)
        ]
        return {"slowest": slowest, "by_event": []}

    def _on_memory_performance(self, payload: Dict[str, Any]) -> None:
        """Render the in-memory performance payload."""
        self._on_performance(payload)

    def _on_performance(self, payload: Dict[str, Any]) -> None:
        """Render slowest events and per-event aggregates."""
        self.performance_tree.delete(*self.performance_tree.get_children())
        for row in payload.get("slowest", []):
            self.performance_tree.insert(
                "", tk.END,
                values=(
                    row.get("time", ""),
                    row.get("event", ""),
                    row.get("duration_ms", 0),
                    row.get("db_name", ""),
                    row.get("user", ""),
                    row.get("context", ""),
                ),
            )
        self.performance_agg_tree.delete(*self.performance_agg_tree.get_children())
        for row in payload.get("by_event", []):
            self.performance_agg_tree.insert(
                "", tk.END,
                values=(
                    row.get("event", ""),
                    row.get("count", 0),
                    row.get("sum_ms", 0),
                    row.get("avg_ms", 0),
                    row.get("max_ms", 0),
                    row.get("p95_ms", 0),
                ),
            )

    # ==================================================================
    # TAB 5: По пользователям
    # ==================================================================
    def create_users_tab(self) -> None:
        """Build the users tab."""
        frame = ttk.Frame(self.notebook)
        self.users_frame = frame

        tree_frame = ttk.Frame(frame)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        columns = ("User", "Events", "Errors", "FirstSeen", "LastSeen")
        self.user_stats_tree = ttk.Treeview(tree_frame, columns=columns, show="headings", height=8)
        for column, title, width in (
            ("User", "Пользователь", 180),
            ("Events", "Событий", 90),
            ("Errors", "Ошибок", 90),
            ("FirstSeen", "Первое событие", 150),
            ("LastSeen", "Последнее событие", 150),
        ):
            self.user_stats_tree.heading(column, text=title)
            self.user_stats_tree.column(column, width=width)
        self._attach_scrollbar(tree_frame, self.user_stats_tree)
        self.user_stats_tree.bind("<<TreeviewSelect>>", self.on_user_click)

        lower_frame = ttk.LabelFrame(frame, text="События пользователя")
        lower_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        lower_columns = ("Time", "Event", "Db", "DurationMs", "Description")
        self.user_events_tree = ttk.Treeview(
            lower_frame, columns=lower_columns, show="headings", height=8
        )
        for column, title, width in (
            ("Time", "Время", 140),
            ("Event", "Тип события", 110),
            ("Db", "База", 110),
            ("DurationMs", "Длительность, мс", 120),
            (lower_columns[4], DESCRIPTION_COLUMN_LABEL, 300),
        ):
            self.user_events_tree.heading(column, text=title)
            self.user_events_tree.column(column, width=width)
        self._attach_scrollbar(lower_frame, self.user_events_tree)

    def refresh_users(self) -> None:
        """Refresh the users tab from ClickHouse or memory.

        Memory-mode aggregation runs on a worker thread (PERF-003).
        """
        if self.source != "clickhouse":
            if not self.events:
                return
            self.run_async(self._memory_users_payload, "memory_users")
            return
        client = ClickHouseClient(self.clickhouse_config)
        filters = self.filters
        self.run_async(lambda: client.users(filters), "users")

    def _memory_users_payload(self) -> List[Dict[str, Any]]:
        """Return per-user statistics from ``self.events`` on a worker thread."""
        _, _, user_counts, errors = self._memory_data()
        error_counts = Counter(
            event.get("user", "") for event in errors if event.get("user")
        )
        return [
            {
                "user": user,
                "events": count,
                "errors": error_counts.get(user, 0),
                "first_seen": "",
                "last_seen": "",
            }
            for user, count in sorted(
                user_counts.items(), key=lambda item: item[1], reverse=True
            )
        ]

    def _on_memory_users(self, rows: List[Dict[str, Any]]) -> None:
        """Render in-memory per-user statistics."""
        self._on_users(rows)

    def _on_users(self, rows: List[Dict[str, Any]]) -> None:
        """Render per-user ClickHouse statistics."""
        self.user_stats_tree.delete(*self.user_stats_tree.get_children())
        for row in rows:
            self.user_stats_tree.insert(
                "", tk.END,
                values=(
                    row.get("user", ""),
                    row.get("events", 0),
                    row.get("errors", 0),
                    row.get("first_seen", ""),
                    row.get("last_seen", ""),
                ),
            )

    def on_user_click(self, event: Any = None) -> None:
        """Load the selected user's events."""
        selection = self.user_stats_tree.selection()
        if not selection:
            return
        item = self.user_stats_tree.item(selection[0])
        values = item.get("values") or []
        if not values:
            return
        user = str(values[0])
        self.selected_user = user
        if self.source != "clickhouse":
            self.run_async(
                lambda: self._memory_user_events(user), "user_events"
            )
            return
        client = ClickHouseClient(self.clickhouse_config)
        filters = self.filters
        self.run_async(lambda: client.user_events(user, filters), "user_events")

    def _memory_user_events(self, user: str, limit: int = 200) -> List[Dict[str, Any]]:
        """Return one user's recent in-memory events in the ClickHouse shape."""
        rows: List[Dict[str, Any]] = []
        for row in self.events:
            if row.get("user") != user:
                continue
            rows.append(
                {
                    "time": row.get("timestamp", "") or "",
                    "event": row.get("name", "") or "",
                    "db_name": row.get("process", "") or "",
                    "duration_ms": "",
                    "description": row.get("descr", "") or "",
                    "description_is_excerpt": False,
                }
            )
            if len(rows) >= limit:
                break
        return rows

    def _on_user_events(self, rows: List[Dict[str, Any]]) -> None:
        """Render one user's events."""
        self.user_events_tree.delete(*self.user_events_tree.get_children())
        for row in rows:
            self.user_events_tree.insert(
                "", tk.END,
                values=(
                    row.get("time", ""),
                    row.get("event", ""),
                    row.get("db_name", ""),
                    row.get("duration_ms", 0),
                    format_description(row),
                ),
            )

    # ==================================================================
    # Export
    # ==================================================================
    def create_export_tab(self) -> None:
        """Build the export tab: legacy CSV buttons plus format/scope export."""
        frame = ttk.Frame(self.notebook)
        self.export_frame = frame

        legacy = ttk.LabelFrame(frame, text="Быстрый экспорт текущих таблиц (CSV)")
        legacy.pack(fill=tk.X, padx=10, pady=(10, 4))
        ttk.Button(
            legacy, text="Сохранить обзор в CSV", command=self.save_overview_csv
        ).pack(pady=4)
        ttk.Button(
            legacy, text="Сохранить ошибки в CSV", command=self.save_errors_csv
        ).pack()
        ttk.Button(
            legacy,
            text="Сохранить производительность в CSV",
            command=self.save_performance_csv,
        ).pack(pady=4)
        info = ttk.Label(
            frame,
            text="Экспорт сохраняет ровно те строки, которые показаны в таблице "
                 "(для ошибок — текущая страница).",
            justify=tk.LEFT,
        )
        info.pack(anchor=tk.W, padx=12, pady=8)

        advanced = ttk.LabelFrame(frame, text="Экспорт в файл")
        advanced.pack(fill=tk.X, padx=10, pady=(4, 8))

        format_row = ttk.Frame(advanced)
        format_row.pack(fill=tk.X, padx=6, pady=2)
        ttk.Label(format_row, text="Формат:").pack(side=tk.LEFT)
        self.export_format_var = tk.StringVar(master=frame, value="CSV")
        self.export_format_combo = ttk.Combobox(
            format_row,
            textvariable=self.export_format_var,
            values=list(EXPORT_FORMATS),
            state="readonly",
            width=10,
        )
        self.export_format_combo.pack(side=tk.LEFT, padx=(2, 12))

        ttk.Label(format_row, text="Что:").pack(side=tk.LEFT)
        self.export_target_var = tk.StringVar(master=frame, value=EXPORT_TARGET_ERRORS)
        self.export_target_combo = ttk.Combobox(
            format_row,
            textvariable=self.export_target_var,
            values=list(EXPORT_TARGETS),
            state="readonly",
            width=22,
        )
        self.export_target_combo.pack(side=tk.LEFT, padx=(2, 12))

        scope_row = ttk.Frame(advanced)
        scope_row.pack(fill=tk.X, padx=6, pady=2)
        ttk.Label(scope_row, text="Область:").pack(side=tk.LEFT)
        self.export_scope_var = tk.StringVar(master=frame, value=EXPORT_SCOPE_CURRENT)
        ttk.Radiobutton(
            scope_row,
            text="Текущая таблица",
            variable=self.export_scope_var,
            value=EXPORT_SCOPE_CURRENT,
        ).pack(side=tk.LEFT, padx=(2, 8))
        ttk.Radiobutton(
            scope_row,
            text="Все отфильтрованные",
            variable=self.export_scope_var,
            value=EXPORT_SCOPE_ALL,
        ).pack(side=tk.LEFT, padx=(2, 8))

        action_row = ttk.Frame(advanced)
        action_row.pack(fill=tk.X, padx=6, pady=4)
        self.export_button = ttk.Button(
            action_row, text="Экспортировать…", command=self.start_export
        )
        self.export_button.pack(side=tk.LEFT)
        self.export_cancel_button = ttk.Button(
            action_row,
            text="Отменить экспорт",
            command=self.cancel_export,
            state=tk.DISABLED,
        )
        self.export_cancel_button.pack(side=tk.LEFT, padx=6)

        self.export_progress = ttk.Progressbar(
            advanced, mode="determinate", maximum=1, value=0
        )
        self.export_progress.pack(fill=tk.X, padx=6, pady=(4, 2))
        self.export_status_label = ttk.Label(advanced, text="", justify=tk.LEFT)
        self.export_status_label.pack(anchor=tk.W, padx=6, pady=(0, 4))

        note = ttk.Label(
            advanced,
            text="Агрегаты (Обзор, Производительность) ограничены top-N — «все» "
                 "для них недоступно. Экспорт «все» доступен для ошибок и "
                 "пользователей.",
            justify=tk.LEFT,
            wraplength=900,
        )
        note.pack(anchor=tk.W, padx=6, pady=(0, 6))

    def _tree_rows(self, tree: ttk.Treeview) -> List[List[str]]:
        """Return the values of every row in ``tree``.

        Args:
            tree: Treeview to read.

        Returns:
            List of row value lists.
        """
        rows = []
        for item in tree.get_children():
            values = tree.item(item, "values")
            rows.append([str(value) for value in values])
        return rows

    def _tree_headers(self, tree: ttk.Treeview) -> List[str]:
        """Return the display headers of ``tree``.

        Args:
            tree: Treeview to read.

        Returns:
            List of header labels.
        """
        return [tree.heading(column, "text") for column in tree["columns"]]

    # -- export helpers ----------------------------------------------------
    def _selected_export_format(self) -> export.ExportFormat:
        """Return the selected format, falling back to CSV for unknown values."""
        raw = self.export_format_var.get().strip() or "CSV"
        try:
            return export.ExportFormat(raw)
        except export.ExportError:
            return export.ExportFormat.CSV

    def _export_tree_for(self, target: str) -> "tuple[ttk.Treeview, str]":
        """Return the treeview and default file-name prefix for ``target``.

        Args:
            target: One of :data:`EXPORT_TARGETS`.

        Returns:
            ``(tree, prefix)``; the overview tree is the safe fallback.
        """
        mapping = {
            EXPORT_TARGET_ERRORS: (self.error_tree, "errors"),
            EXPORT_TARGET_USERS: (self.user_stats_tree, "users"),
            EXPORT_TARGET_OVERVIEW: (self.event_tree, "overview"),
            EXPORT_TARGET_PERFORMANCE: (self.performance_tree, "performance"),
        }
        return mapping.get(target, (self.event_tree, "overview"))

    def _export_metadata(self) -> Dict[str, Any]:
        """Return metadata stored in JSON/XLSX exports.

        Filters are ``None`` in memory mode: the in-memory parser never applies
        them, so advertising them would be misleading.
        """
        filters: Optional[Dict[str, Any]] = None
        if self.source == "clickhouse":
            try:
                filters = presets.filters_to_dict(self.filters) or None
            except presets.PresetsError:  # pragma: no cover - normalized filters
                filters = None
        return {"source": self.source, "dataset": self.dataset, "filters": filters}

    def _ask_export_path(
        self, prefix: str, fmt: export.ExportFormat
    ) -> Optional[str]:
        """Ask for a destination path for ``fmt`` on the Tk main thread."""
        path = filedialog.asksaveasfilename(
            defaultextension=f".{fmt.value}",
            filetypes=export.filetypes(fmt),
            initialfile=export.suggest_filename(prefix, fmt),
        )
        return path or None

    def _set_export_status(self, message: str) -> None:
        """Show ``message`` below the export progress bar."""
        try:
            self.export_status_label.config(text=str(message)[:500])
        except tk.TclError:  # pragma: no cover - during shutdown
            pass

    def _set_export_busy(self, busy: bool) -> None:
        """Enable or disable the export controls while a worker runs."""
        self.export_busy = busy
        try:
            self.export_button.config(state=tk.DISABLED if busy else tk.NORMAL)
            self.export_cancel_button.config(
                state=tk.NORMAL if busy else tk.DISABLED
            )
        except tk.TclError:  # pragma: no cover - during shutdown
            pass

    def _finish_export(self) -> None:
        """Return the export controls to the idle state and forget the worker."""
        self._set_export_busy(False)
        self.export_cancel_event = None
        self.export_thread = None

    def _export_finished(self, result: export.ExportResult) -> None:
        """Report a completed export (current table or worker) to the user."""
        try:
            self.export_progress.config(
                maximum=max(int(result.row_count), 1), value=int(result.row_count)
            )
        except tk.TclError:  # pragma: no cover - during shutdown
            pass
        text = f"Сохранено: {result.path} ({result.row_count} строк)"
        if result.truncated:
            text += " — экспорт усечён"
        self._set_export_status(text)
        self.set_status(f"Сохранено: {result.path}")
        message = f"Файл: {result.path}\nСтрок: {result.row_count}"
        if result.truncated:
            message += f"\nЭкспорт усечён: записано {result.row_count} строк."
        messagebox.showinfo("Экспорт", message)

    # -- export entry points ------------------------------------------------
    def start_export(self) -> None:
        """Export the current table or every filtered row, depending on scope."""
        if self.export_busy:
            return
        fmt = self._selected_export_format()
        target = self.export_target_var.get().strip() or EXPORT_TARGET_ERRORS
        scope = self.export_scope_var.get() or EXPORT_SCOPE_CURRENT
        if scope == EXPORT_SCOPE_ALL:
            self._export_all_rows(target, fmt)
        else:
            self._export_current_table(target, fmt)

    def _export_current_table(
        self, target: str, fmt: export.ExportFormat
    ) -> None:
        """Synchronously export the rows currently shown in ``target``'s tree."""
        tree, prefix = self._export_tree_for(target)
        rows = self._tree_rows(tree)
        if not rows:
            messagebox.showwarning("Внимание", "Нет данных для экспорта!")
            return
        headers = self._tree_headers(tree)
        path = self._ask_export_path(prefix, fmt)
        if not path:
            return
        try:
            result = export.export_rows(
                headers,
                rows,
                path,
                fmt=fmt,
                max_rows=None,
                metadata=self._export_metadata(),
            )
        except (OSError, export.ExportError) as exc:
            self._set_export_status("Ошибка экспорта.")
            messagebox.showerror(
                "Ошибка", f"Не удалось сохранить экспорт:\n{exc}"
            )
            return
        self._export_finished(result)

    def _export_all_rows(self, target: str, fmt: export.ExportFormat) -> None:
        """Start a worker that exports every filtered row of ``target``.

        Aggregates (Overview, Performance) have no "all" source and memory mode
        does not filter, so both cases explain the limitation instead of
        silently exporting the displayed top-N slice.
        """
        if self.source != "clickhouse":
            messagebox.showwarning(
                "Экспорт",
                "Экспорт «все» доступен только для ClickHouse; в режиме памяти — "
                "текущая таблица.",
            )
            return
        if target in (EXPORT_TARGET_OVERVIEW, EXPORT_TARGET_PERFORMANCE):
            messagebox.showwarning(
                "Экспорт",
                f"«{target}» — агрегат top-N, полностью выгрузить его нельзя. "
                "Выберите область «Текущая таблица».",
            )
            return

        tree, prefix = self._export_tree_for(target)
        headers = self._tree_headers(tree)
        total: Optional[int] = None
        allow_truncate = False
        if target == EXPORT_TARGET_ERRORS:
            total = int(self.error_total)
            if total <= 0:
                messagebox.showwarning("Внимание", "Нет данных для экспорта!")
                return
            proceed, allow_truncate = self._confirm_export_size(total)
            if not proceed:
                return

        path = self._ask_export_path(prefix, fmt)
        if not path:
            return
        self._start_export_worker(
            target, fmt, path, headers, total, allow_truncate
        )

    def _confirm_export_size(self, total: int) -> "tuple[bool, bool]":
        """Ask about large exports.

        Args:
            total: Known number of matching rows.

        Returns:
            ``(proceed, allow_truncate)``.  Truncation is only allowed after an
            explicit confirmation; cancelling either dialog aborts the export.
        """
        if total >= WARN_ROWS:
            proceed = messagebox.askyesno(
                "Большой экспорт",
                f"Будет выгружено {total} строк. Это может занять время. "
                "Продолжить?",
            )
            if not proceed:
                return False, False
        allow_truncate = False
        if total > DEFAULT_MAX_ROWS:
            allow_truncate = bool(
                messagebox.askyesno(
                    "Ограничение экспорта",
                    f"За один экспорт можно записать не более {DEFAULT_MAX_ROWS} "
                    f"строк из {total}. Продолжить с ограничением?",
                )
            )
            if not allow_truncate:
                return False, False
        return True, allow_truncate

    def _start_export_worker(
        self,
        target: str,
        fmt: export.ExportFormat,
        path: str,
        headers: List[str],
        total: Optional[int],
        allow_truncate: bool,
    ) -> None:
        """Launch the background exporter and switch the controls to busy."""
        config = dict(self.clickhouse_config)
        filters = self.filters
        metadata = self._export_metadata()
        cancel = threading.Event()
        self.export_cancel_event = cancel
        self._set_export_busy(True)
        try:
            self.export_progress.config(maximum=max(int(total or 1), 1), value=0)
        except tk.TclError:  # pragma: no cover - during shutdown
            pass
        self._set_export_status("Экспорт…")
        self.set_status("Экспорт…")

        worker = threading.Thread(
            target=self._run_export_worker,
            args=(
                target,
                fmt,
                path,
                headers,
                total,
                allow_truncate,
                config,
                filters,
                metadata,
                cancel,
            ),
            daemon=True,
            name="export",
        )
        self.export_thread = worker
        worker.start()

    def _run_export_worker(
        self,
        target: str,
        fmt: export.ExportFormat,
        path: str,
        headers: List[str],
        total: Optional[int],
        allow_truncate: bool,
        config: Dict[str, Any],
        filters: Filters,
        metadata: Dict[str, Any],
        cancel: threading.Event,
    ) -> None:
        """Worker body: page/query ClickHouse and write the file.

        Tk is never touched here; progress and the terminal state are posted to
        the dispatcher for the main thread.
        """
        last = {"count": 0, "time": 0.0}

        def post_progress(count: int) -> None:
            now = time.monotonic()
            if (
                count - last["count"] < EXPORT_PAGE_SIZE
                and now - last["time"] < 0.1
            ):
                return
            last["count"] = count
            last["time"] = now
            self.dispatcher.post(
                "export_progress", {"count": count, "total": total}
            )

        try:
            if target == EXPORT_TARGET_ERRORS:
                rows = export.iter_paged_rows(
                    lambda offset, limit: self._fetch_error_export_page(
                        config, filters, offset, limit
                    ),
                    int(total or 0),
                    page_size=EXPORT_PAGE_SIZE,
                    # The cap is enforced by ``export_rows`` so that
                    # ``ExportResult.truncated`` reports a limit that was hit.
                    max_rows=None,
                    cancel=cancel,
                    progress=post_progress,
                )
            else:
                users = logt_adapter.query_users(config, filters)
                rows = (self._user_export_row(row) for row in users)

            result = export.export_rows(
                headers,
                rows,
                path,
                fmt=fmt,
                max_rows=DEFAULT_MAX_ROWS,
                allow_truncate=allow_truncate,
                progress=post_progress,
                cancel=cancel,
                metadata=metadata,
            )
        except export.ExportCancelled:
            self.dispatcher.post("export_cancelled", None)
        except Exception as exc:  # noqa: BLE001 - reported to the user
            self.dispatcher.post(
                "export_failed",
                logt_adapter.redact(exc, config.get("password")),
            )
        else:
            self.dispatcher.post("export_done", result)

    def _fetch_error_export_page(
        self,
        config: Dict[str, Any],
        filters: Filters,
        offset: int,
        limit: int,
    ) -> List[List[str]]:
        """Fetch one error page (display shape) for :func:`export.iter_paged_rows`.

        ``query_errors`` only accepts a page index and a page size, so the
        offset is translated into a ``page_size``-aligned request and the
        remainder is sliced off in memory.
        """
        page_size = EXPORT_PAGE_SIZE
        page = offset // page_size
        rows = logt_adapter.query_errors(
            config, filters, page=page, page_size=page_size
        )
        start = offset - page * page_size
        return [
            self._error_export_row(row) for row in rows[start:start + limit]
        ]

    @staticmethod
    def _error_export_row(row: Dict[str, Any]) -> List[str]:
        """Map one error row to the values shown in ``error_tree``."""
        return [
            str(row.get("time", "") or ""),
            str(row.get("event", "") or ""),
            str(row.get("db_name", "") or ""),
            str(row.get("user", "") or ""),
            format_description(row),
        ]

    @staticmethod
    def _user_export_row(row: Dict[str, Any]) -> List[Any]:
        """Map one user-statistics row to the values shown in ``user_stats_tree``."""
        return [
            str(row.get("user", "") or ""),
            row.get("events", 0),
            row.get("errors", 0),
            str(row.get("first_seen", "") or ""),
            str(row.get("last_seen", "") or ""),
        ]

    # -- export worker handlers --------------------------------------------
    def _on_export_progress(self, payload: Any) -> None:
        """Apply an informational progress message (main thread only)."""
        if not self.export_busy:
            return
        if isinstance(payload, dict):
            count = int(payload.get("count", 0) or 0)
            total = payload.get("total")
        else:  # pragma: no cover - defensive
            count = int(payload or 0)
            total = None
        try:
            if total:
                self.export_progress.config(maximum=max(int(total), 1))
            self.export_progress.config(value=count)
            self._set_export_status(f"Экспорт… {count} строк")
        except (TypeError, ValueError, tk.TclError):  # pragma: no cover
            pass

    def _on_export_done(self, result: export.ExportResult) -> None:
        """Handle a finished background export."""
        self._finish_export()
        self._export_finished(result)

    def _on_export_cancelled(self, payload: Any = None) -> None:
        """Handle a cancelled background export."""
        self._finish_export()
        try:
            self.export_progress.config(value=0)
        except tk.TclError:  # pragma: no cover - during shutdown
            pass
        self._set_export_status("Экспорт отменён.")
        self.set_status("Экспорт отменён.")

    def _on_export_failed(self, message: Any) -> None:
        """Handle a failed background export."""
        self._finish_export()
        self._set_export_status("Ошибка экспорта.")
        self.set_status("Ошибка экспорта.")
        messagebox.showerror(
            "Ошибка", f"Не удалось сохранить экспорт:\n{str(message)[:1500]}"
        )

    def cancel_export(self) -> None:
        """Request cancellation of the running export worker."""
        event = self.export_cancel_event
        if event is None:
            return
        event.set()
        try:
            self.export_cancel_button.config(state=tk.DISABLED)
        except tk.TclError:  # pragma: no cover - during shutdown
            pass
        self._set_export_status("Отмена экспорта запрошена…")

    def _write_csv(self, headers: List[str], rows: List[List[str]], initial: str) -> Optional[str]:
        """Ask for a path and write a CSV file through :mod:`export`.

        Args:
            headers: Column headers.
            rows: Row values.
            initial: Suggested file name.

        Returns:
            The written path, or ``None`` when cancelled or failed.
        """
        file_path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=export.filetypes(export.ExportFormat.CSV),
            initialfile=initial,
        )
        if not file_path:
            return None
        try:
            export.export_rows(
                headers,
                rows,
                file_path,
                fmt=export.ExportFormat.CSV,
                max_rows=None,
            )
        except (OSError, export.ExportError) as exc:
            messagebox.showerror("Ошибка", f"Не удалось сохранить CSV:\n{exc}")
            return None
        self.set_status(f"Сохранено: {file_path}")
        return file_path

    def save_overview_csv(self) -> None:
        """Export the overview event tree to CSV."""
        rows = self._tree_rows(self.event_tree)
        if not rows:
            messagebox.showwarning("Внимание", "Нет данных для экспорта!")
            return
        self._write_csv(self._tree_headers(self.event_tree), rows, "overview.csv")

    def save_errors_csv(self) -> None:
        """Export the displayed error page to CSV."""
        rows = self._tree_rows(self.error_tree)
        if not rows:
            messagebox.showwarning("Внимание", "Нет ошибок для экспорта!")
            return
        self._write_csv(self._tree_headers(self.error_tree), rows, "errors.csv")

    def save_performance_csv(self) -> None:
        """Export the slowest-events table to CSV."""
        rows = self._tree_rows(self.performance_tree)
        if not rows:
            messagebox.showwarning("Внимание", "Нет данных для экспорта!")
            return
        self._write_csv(self._tree_headers(self.performance_tree), rows, "performance.csv")

    # ==================================================================
    # TAB 7: Справочник
    # ==================================================================
    def create_reference_tab(self) -> None:
        """Build the event reference tab."""
        frame = ttk.Frame(self.notebook)
        self.reference_frame = frame

        # Visible warning when ``docs/events.json`` is missing or broken.
        self.registry_warning = ttk.Label(
            frame, text="", foreground="#a00000", justify=tk.LEFT, wraplength=900
        )
        self.registry_warning.pack(anchor=tk.W, padx=10, pady=(5, 0))

        top = ttk.Frame(frame)
        top.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        columns = ("Code", "Title")
        self.ref_tree = ttk.Treeview(top, columns=columns, show="headings", height=10)
        self.ref_tree.heading("Code", text="Код")
        self.ref_tree.heading("Title", text="Название")
        self.ref_tree.column("Code", width=140)
        self.ref_tree.column("Title", width=400)
        self.ref_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(top, orient=tk.VERTICAL, command=self.ref_tree.yview)
        self.ref_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        bottom = ttk.LabelFrame(frame, text="Описание события")
        bottom.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        self.ref_text = tk.Text(bottom, wrap=tk.WORD, height=12, state=tk.DISABLED)
        self.ref_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.ref_tree.bind("<<TreeviewSelect>>", self.on_reference_select)
        self._populate_reference_tree()

    def _populate_reference_tree(self) -> None:
        """Fill the reference tree from ``docs/events.json`` and warn on failure."""
        try:
            data = load_registry()
        except Exception:  # pragma: no cover - load_registry records failures
            data = {"events": []}
        error = registry_error()
        if error:
            self.registry_warning.config(
                text=(
                    "Справочник событий не загружен — работа продолжается с пустым "
                    f"реестром.\n{error}"
                )
            )
            self.set_status("Справочник событий не загружен — см. вкладку «Справочник».")
        else:
            self.registry_warning.config(text="")
        for entry in data.get("events", []):
            self.ref_tree.insert("", tk.END, values=(entry["code"], entry.get("title", "")))

    def on_reference_select(self, event: Any = None) -> None:
        """Show details for the selected event code."""
        selection = self.ref_tree.selection()
        if not selection:
            return
        code = self.ref_tree.item(selection[0], "values")[0]
        data = load_registry()
        entry = next((item for item in data.get("events", []) if item["code"] == code), None)
        if not entry:
            return
        lines = [f"{entry['code']} — {entry.get('title', '')}", "", "Описание:"]
        lines.append(entry.get("description", "") or "(нет описания)")
        lines.append("")
        if entry.get("fields"):
            lines.append("Поля:")
            for field in entry["fields"]:
                lines.append(f"  {field['name']} — {field.get('meaning', '')}")
            lines.append("")
        if entry.get("examples"):
            lines.append("Пример:")
            lines.append(entry["examples"][0])
        self.ref_text.config(state=tk.NORMAL)
        self.ref_text.delete("1.0", tk.END)
        self.ref_text.insert("1.0", "\n".join(lines))
        self.ref_text.config(state=tk.DISABLED)

    # ==================================================================
    # Lifecycle
    # ==================================================================
    def on_close(self) -> None:
        """Cancel any running import, wait briefly, then destroy the window."""
        self.shutdown()
        thread = self.import_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)
        export_thread = self.export_thread
        if export_thread is not None and export_thread.is_alive():
            export_thread.join(timeout=5)
        try:
            self.root.destroy()
        except tk.TclError:  # pragma: no cover
            pass


class ConnectionDialog:
    """Modal dialog for editing ClickHouse connection settings."""

    def __init__(self, parent: tk.Misc, config: Dict[str, Any]):
        """Build the dialog.

        Args:
            parent: Parent window.
            config: Current connection settings.
        """
        self.window = tk.Toplevel(parent)
        self.window.title("Настройки ClickHouse")
        self.window.transient(parent)
        self.window.grab_set()
        self.result: Optional[Dict[str, Any]] = None

        self.vars = {
            "host": tk.StringVar(master=self.window, value=str(config.get("host", "127.0.0.1"))),
            "port": tk.StringVar(master=self.window, value=str(config.get("port", 9000))),
            "database": tk.StringVar(master=self.window, value=str(config.get("database", "tracelog"))),
            "user": tk.StringVar(master=self.window, value=str(config.get("user", "default"))),
            "password": tk.StringVar(master=self.window, value=str(config.get("password", ""))),
        }

        body = ttk.Frame(self.window, padding=10)
        body.pack(fill=tk.BOTH, expand=True)
        for index, (key, label) in enumerate(
            (
                ("host", "Хост"),
                ("port", "Порт (native, 9000)"),
                ("database", "База данных"),
                ("user", "Пользователь"),
                ("password", "Пароль"),
            )
        ):
            ttk.Label(body, text=label).grid(row=index, column=0, sticky=tk.W, pady=3)
            entry = ttk.Entry(body, textvariable=self.vars[key], width=32)
            if key == "password":
                entry.config(show="•")
            entry.grid(row=index, column=1, sticky=tk.EW, pady=3)

        note = ttk.Label(
            body,
            text="Пароль можно не вводить: используется переменная окружения "
                 "CLICKHOUSE_PASSWORD или файл config/clickhouse.local.json.",
            wraplength=380,
            justify=tk.LEFT,
        )
        note.grid(row=len(self.vars), column=0, columnspan=2, sticky=tk.W, pady=(8, 4))

        buttons = ttk.Frame(body)
        buttons.grid(row=len(self.vars) + 1, column=0, columnspan=2, sticky=tk.E)
        ttk.Button(buttons, text="Отмена", command=self.window.destroy).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Сохранить", command=self._save).pack(side=tk.RIGHT, padx=6)

    def _save(self) -> None:
        """Validate input and close the dialog with the new settings."""
        try:
            port = int(self.vars["port"].get())
        except ValueError:
            messagebox.showerror("Ошибка", "Порт должен быть числом.", parent=self.window)
            return
        database = self.vars["database"].get().strip()
        try:
            logt_adapter.schema.qualified_table(database, logt_adapter.TABLE_EVENTS)
        except ValueError as exc:
            messagebox.showerror("Ошибка", str(exc), parent=self.window)
            return
        self.result = {
            "host": self.vars["host"].get().strip() or "127.0.0.1",
            "port": port,
            "database": database,
            "user": self.vars["user"].get().strip() or "default",
            "password": self.vars["password"].get(),
        }
        self.window.destroy()


def main() -> None:
    """Start the GUI."""
    configure_logging()
    root = tk.Tk()
    LogAnalyzerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
