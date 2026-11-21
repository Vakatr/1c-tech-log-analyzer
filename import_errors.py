"""Diagnosable import failures for the 1C Log Analyzer GUI.

The LogT import can fail before it starts (schema, configuration), inside the
subprocess (non-zero exit code) or while the result is being evaluated.  A
generic ``messagebox`` hides everything the user needs to report the problem:
the stage, the dataset, LogT's exit code and its own diagnostic log.

This module builds one immutable failure description (:class:`ImportFailure`),
renders it as text (:func:`format_import_failure`) and shows it in a
non-modal window (:class:`ImportErrorDialog`) whose content can be copied to
the clipboard.  It deliberately has no ClickHouse, ``logt_adapter`` or
``ics_log_analyzer`` imports at runtime: the adapter result is handled
duck-typed, so this module can be imported and tested on its own.  Tkinter is
the only non-stdlib dependency and is used solely by the dialog.
"""

from __future__ import annotations

import logging
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import ttk
from typing import TYPE_CHECKING, Callable, List, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from logt_adapter import ImportResult

logger = logging.getLogger(__name__)

__all__ = [
    "ImportFailure",
    "ImportErrorDialog",
    "find_latest_logt_log",
    "format_import_failure",
    "LOGT_LOG_DIR_NAME",
    "LOGT_LOG_GLOB",
    "STDOUT_TAIL_LINES",
]

#: Sub-directory of the selected log folder where LogT writes its own journal.
LOGT_LOG_DIR_NAME = ".logt_logs"

#: File name pattern used by LogT (``vendor/logt/src/main.py``).
LOGT_LOG_GLOB = "trace-vrs-*.log"

#: How many trailing stdout lines are kept for diagnostics.
STDOUT_TAIL_LINES = 20

#: Placeholder for a field the caller could not determine.
UNKNOWN = "—"

#: Maximum length of the short error line above the details box.
SHORT_ERROR_LIMIT = 400

#: Dialog title, shared by the window manager and the tests.
DIALOG_TITLE = "Ошибка импорта"


@dataclass
class ImportFailure:
    """Everything needed to explain a failed import to the user.

    Attributes:
        stage: Human-readable stage the import was in when it failed
            (for example ``"Подготовка схемы ClickHouse"``).
        error: Already redacted error message.
        dataset: Dataset the import was writing to.
        log_dir: Folder of 1C logs selected by the user.
        log_path: Path to LogT's own journal (``trace-vrs-*.log``), if found.
        exit_code: LogT process exit code; ``-1`` when no process ran.
        duration: Wall-clock duration of the attempt, in seconds.
        stderr_lines: Trailing stderr lines (already filtered by the adapter).
        stdout_lines: Trailing stdout lines, at most :data:`STDOUT_TAIL_LINES`.
    """

    stage: str = ""
    error: str = ""
    dataset: str = ""
    log_dir: str = ""
    log_path: Optional[str] = None
    exit_code: int = -1
    duration: float = 0.0
    stderr_lines: List[str] = field(default_factory=list)
    stdout_lines: List[str] = field(default_factory=list)

    @classmethod
    def from_result(
        cls,
        result: "ImportResult",
        *,
        stage: str,
        dataset: str,
        log_dir: str,
        log_path: Optional[str] = None,
    ) -> "ImportFailure":
        """Build a failure from an adapter :class:`ImportResult`.

        The adapter's ``ImportResult`` is accessed duck-typed so this module
        never imports :mod:`logt_adapter`.

        Args:
            result: Object with ``error``, ``exit_code``, ``duration``,
                ``stderr_tail`` and ``stdout`` attributes.
            stage: Stage the import was in; passed by the GUI, which owns the
                stage machine.
            dataset: Dataset being imported into.
            log_dir: Folder of 1C logs.
            log_path: Optional path to LogT's journal; the caller finds it in
                the worker thread so the GUI thread does no I/O.

        Returns:
            A populated :class:`ImportFailure`.
        """
        try:
            exit_code = int(getattr(result, "exit_code", -1))
        except (TypeError, ValueError):
            exit_code = -1
        try:
            duration = float(getattr(result, "duration", 0.0) or 0.0)
        except (TypeError, ValueError):
            duration = 0.0

        stdout = str(getattr(result, "stdout", "") or "")
        stdout_lines = [line for line in stdout.splitlines() if line.strip()]
        stderr_tail = getattr(result, "stderr_tail", None) or []

        return cls(
            stage=str(stage or ""),
            error=str(getattr(result, "error", "") or ""),
            dataset=str(dataset or ""),
            log_dir=str(log_dir or ""),
            log_path=log_path,
            exit_code=exit_code,
            duration=duration,
            stderr_lines=[str(line) for line in stderr_tail],
            stdout_lines=stdout_lines[-STDOUT_TAIL_LINES:],
        )


def find_latest_logt_log(log_dir: str, newer_than: Optional[float] = None) -> Optional[str]:
    """Return the newest LogT journal in ``<log_dir>/.logt_logs``.

    LogT writes one ``trace-vrs-*.log`` per run.  Showing the newest one (or
    telling the user it is missing) turns "the import failed" into something
    that can actually be investigated.

    Args:
        log_dir: Folder of 1C logs selected by the user.
        newer_than: Optional ``time.time()`` lower bound.  Journals modified at
            or before this timestamp are ignored, so a failure right after the
            import started does not point at a log from an earlier run.

    Returns:
        Absolute path to the newest matching journal, or ``None`` when the
        directory is missing, unreadable or contains no matching file.
    """
    if not log_dir:
        return None
    try:
        directory = Path(log_dir) / LOGT_LOG_DIR_NAME
        candidates = []
        for path in directory.glob(LOGT_LOG_GLOB):
            try:
                mtime = path.stat().st_mtime
            except OSError:  # raced with LogT's cleanup
                continue
            if newer_than is not None and mtime <= newer_than:
                continue
            candidates.append((mtime, path.name, path))
    except (OSError, TypeError, ValueError):
        return None
    if not candidates:
        return None
    # Sort by mtime first and by name second so the result is deterministic
    # when the filesystem reports coarse timestamps.
    candidates.sort(key=lambda item: (item[0], item[1]))
    return str(candidates[-1][2])


def _render_lines(title: str, lines: List[str]) -> List[str]:
    """Render a labelled diagnostic block."""
    if not lines:
        return ["", f"{title}: пусто"]
    return ["", f"{title} (последние {len(lines)} строк):", *lines]


def format_import_failure(failure: ImportFailure) -> str:
    """Render a failure as copyable diagnostic text.

    Args:
        failure: Failure description.

    Returns:
        Multi-line text with the stage, dataset, log folder, exit code,
        duration, journal path and the trailing stderr/stdout lines.  Missing
        values are rendered as a dash instead of raising.
    """
    stage = failure.stage or UNKNOWN
    dataset = failure.dataset or UNKNOWN
    log_dir = failure.log_dir or UNKNOWN
    try:
        duration = float(failure.duration or 0.0)
    except (TypeError, ValueError):
        duration = 0.0

    lines = [
        f"Этап: {stage}",
        f"Dataset: {dataset}",
        f"Папка с логами: {log_dir}",
        f"Код возврата LogT: {failure.exit_code}",
        f"Длительность: {duration:.1f} с",
    ]
    if failure.log_path:
        lines.append(f"Журнал LogT: {failure.log_path}")
    else:
        expected = (
            Path(failure.log_dir) / LOGT_LOG_DIR_NAME if failure.log_dir else LOGT_LOG_DIR_NAME
        )
        lines.append(f"Журнал LogT: не найден в {expected}")

    lines += ["", "Ошибка:", failure.error or "Неизвестная ошибка"]
    lines += _render_lines("stderr", list(failure.stderr_lines or []))
    lines += _render_lines("stdout", list(failure.stdout_lines or []))
    return "\n".join(lines)


class ImportErrorDialog:
    """Non-modal window with the full details of a failed import.

    The dialog never calls :meth:`tkinter.Misc.grab_set` or
    ``wait_window``: the main window stays usable, so the user can fix the
    problem and start a new import while the old report is still open.  Every
    text widget is read-only but selectable, and the whole report can be
    copied with one button — that is what makes a screenshot-free bug report
    possible.

    Attributes:
        window: The ``Toplevel`` window.
        failure: The :class:`ImportFailure` being displayed.
        details: Rendered (and optionally redacted) diagnostic text.
        text: Read-only ``tk.Text`` holding :attr:`details`.
        copy_status: Label showing the clipboard confirmation.
    """

    def __init__(
        self,
        parent: tk.Misc,
        failure: ImportFailure,
        *,
        redact: Optional[Callable[[str], str]] = None,
    ) -> None:
        """Build and show the dialog.

        Args:
            parent: Parent window (the main application window).
            failure: Failure description to display.
            redact: Optional sanitizer applied to the rendered report, so a
                password that survived in stderr or stdout never reaches the
                screen or the clipboard.  Exceptions inside the sanitizer are
                logged and ignored.
        """
        self.failure = failure
        details = format_import_failure(failure)
        if redact is not None:
            try:
                details = redact(details)
            except Exception:  # noqa: BLE001 - a failing filter must not hide the error
                logger.exception("Could not redact import failure details")
        self.details = details

        self.window = tk.Toplevel(parent)
        self.window.title(DIALOG_TITLE)
        try:
            self.window.transient(parent)
        except tk.TclError:  # pragma: no cover - parent already gone
            pass
        self.window.geometry("760x520")
        self.window.minsize(520, 320)

        body = ttk.Frame(self.window, padding=10)
        body.pack(fill=tk.BOTH, expand=True)

        ttk.Label(body, text="Импорт не выполнен").pack(anchor=tk.W)
        ttk.Label(body, text=f"Этап: {failure.stage or UNKNOWN}").pack(
            anchor=tk.W, pady=(6, 0)
        )
        ttk.Label(
            body,
            text=(failure.error or "Неизвестная ошибка")[:SHORT_ERROR_LIMIT],
            wraplength=700,
            justify=tk.LEFT,
        ).pack(anchor=tk.W, pady=(2, 8))

        text_frame = ttk.Frame(body)
        text_frame.pack(fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(text_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.text = tk.Text(
            text_frame,
            wrap=tk.WORD,
            height=18,
            yscrollcommand=scrollbar.set,
        )
        self.text.insert("1.0", self.details)
        self.text.config(state=tk.DISABLED)
        self.text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.text.yview)

        self.copy_status = ttk.Label(body, text="")
        self.copy_status.pack(anchor=tk.W, pady=(6, 0))

        buttons = ttk.Frame(body)
        buttons.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(buttons, text="Закрыть", command=self.close).pack(side=tk.RIGHT)
        ttk.Button(buttons, text="Скопировать детали", command=self.copy_details).pack(
            side=tk.RIGHT, padx=6
        )

        self.window.bind("<Escape>", self._on_escape)
        self.window.protocol("WM_DELETE_WINDOW", self.close)

    @property
    def is_open(self) -> bool:
        """Return whether the window still exists."""
        try:
            return bool(self.window.winfo_exists())
        except tk.TclError:  # pragma: no cover - interpreter shutting down
            return False

    def copy_details(self) -> bool:
        """Copy the rendered report to the clipboard.

        Returns:
            ``True`` when the clipboard was updated.  Tk failures are logged
            and reported in :attr:`copy_status` instead of raising: a broken
            clipboard must not crash the error dialog itself.
        """
        try:
            self.window.clipboard_clear()
            self.window.clipboard_append(self.details)
            self.window.update_idletasks()
        except tk.TclError:
            logger.exception("Could not copy import failure details")
            self._set_copy_status("Не удалось скопировать в буфер обмена.")
            return False
        self._set_copy_status("Детали скопированы в буфер обмена.")
        return True

    def close(self) -> None:
        """Close the dialog.  Idempotent."""
        try:
            self.window.destroy()
        except tk.TclError:  # pragma: no cover - already destroyed
            pass

    def _set_copy_status(self, message: str) -> None:
        """Update the confirmation label without raising during shutdown."""
        try:
            self.copy_status.config(text=message)
        except tk.TclError:  # pragma: no cover
            pass

    def _on_escape(self, _event: "tk.Event[tk.Misc] | None" = None) -> str:
        """Close the dialog on Escape and stop Tk from handling the key."""
        self.close()
        return "break"
