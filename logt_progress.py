"""Typed progress parser for the stdout/stderr of the LogT importer.

The LogT script (``vendor/logt/src/main.py``) is a separate process.  It
reports what it is doing by printing Russian ``[INFO]`` lines to ``stdout``
and by driving a ``tqdm`` progress bar on ``stderr``.  This module turns
those unstructured lines into the typed, UI-free snapshots defined in
:mod:`import_progress`.

Design notes
------------
* **No input, no output.**  The parser never spawns anything, never calls the
  GUI and never throttles.  :meth:`LogTProgressParser.feed_stdout` and
  :meth:`LogTProgressParser.feed_stderr` accept one line and return either an
  :class:`import_progress.ImportProgress` snapshot or ``None``.  Throttling and
  delivery belong to :class:`import_progress.ProgressReporter` (P1-03).
* **Unknown lines are not errors.**  A line the parser does not recognise
  returns ``None`` and leaves the state untouched.  LogT is allowed to change
  its wording without breaking an import.
* **Line-oriented but carriage-return tolerant.**  ``tqdm`` rewrites the same
  line with ``\\r``.  When a chunk contains several ``\\r``-separated ticks the
  parser only looks at the last non-empty segment, which is the freshest one.
* **Overall percentage is hour based.**  LogT processes the logs hour by hour::

      100 * ((hour_index - 1) + hour_fraction) / hours_total

  where ``hour_fraction`` is the fraction of the current hour (``current /
  total`` of the bar, or the bar percentage when the total is missing).  The
  value is clamped to ``0..100`` and never decreases.  When ``hours_total`` is
  unknown the overall percentage stays ``None`` (indeterminate).

  While the input is still being scanned, the overall import percentage is
  ``0``: the file counts are reported in ``current``/``total`` (the GUI shows
  "файлов 7/14"), but the hour-based bar has not advanced yet.  This keeps
  the bar monotonic when the first hour tick arrives.

This module imports only the standard library and :mod:`import_progress`; it
has no Tkinter, ClickHouse or ``logt_adapter`` dependency.
"""

from __future__ import annotations

import math
import re

from import_progress import ImportProgress, ImportStage

__all__ = ["LogTProgressParser", "is_tqdm_line"]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Stage used while LogT walks the input directory.
_SCANNING = ImportStage.SCANNING
#: Stage used while LogT parses and merges the hourly log files.
_PROCESSING = ImportStage.PROCESSING
#: Stage used once parsing is done and rows are written to ClickHouse.
_INSERTING = ImportStage.INSERTING

#: Fixed percentage reported for the insert phase.  ``COUNTING`` (95) and
#: ``DONE`` (100) are emitted by the adapter/GUI (P1-03/P1-04), not here.
INSERTING_PERCENT = 92.0

#: Multipliers of the ``tqdm`` ``unit_scale`` suffixes.
_SCALES = {
    "k": 1e3,
    "m": 1e6,
    "g": 1e9,
    "t": 1e12,
    "p": 1e15,
    "e": 1e18,
    "z": 1e21,
    "y": 1e24,
}

#: CSI and OSC ANSI escapes; ``tqdm`` colours its bar when the terminal allows.
_ANSI_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))"
)

#: Control characters that carry no information after ``\r`` handling.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Any LogT log prefix.  Such a line is never a ``tqdm`` tick.
_LOG_PREFIX_RE = re.compile(
    r"^\s*\[(?:INFO|WARN|WARNING|ERROR|DEBUG|CRITICAL)\]", re.IGNORECASE
)

#: ``10.0k``, ``1.23M``, ``500`` or ``1,5`` (comma decimal separator).
_NUMBER_RE = re.compile(
    r"^(?P<num>\d+(?:[.,]\d+)*)(?P<suffix>[kKmMgGtTpPeEzZyY]?)$"
)

#: ``[INFO] Найдено файлов rphost: 12, rmngr: 3``
_FILES_FOUND_RE = re.compile(
    r"^\[INFO\]\s*Найдено файлов\s+rphost:\s*(?P<rphost>\d[\d\s.,]*)"
    r"\s*,\s*rmngr:\s*(?P<rmngr>\d[\d\s.,]*)",
    re.IGNORECASE,
)

#: ``[INFO] Часовых групп: 3``
_HOURS_TOTAL_RE = re.compile(
    r"^\[INFO\]\s*Часовых групп:\s*(?P<hours>\d[\d\s,]*)", re.IGNORECASE
)

#: ``[INFO] Час 2024-01-15_10: файлов 7 (rphost+rmngr)``
_HOUR_START_RE = re.compile(
    r"^\[INFO\]\s*Час\s+(?P<key>.+?):\s*файлов\s+(?P<files>\d[\d\s,]*)",
    re.IGNORECASE,
)

#: ``[INFO] Процесс 1/2 rphost_1234: каталогов 2, файлов 14``
_MULTI_PROCESS_RE = re.compile(
    r"^\[INFO\]\s*Процесс\s+(?P<index>\d+)\s*/\s*(?P<total>\d+)\s+"
    r"(?P<name>\S+?):.*?каталогов\s+(?P<dirs>\d+)\s*,\s*"
    r"файлов\s+(?P<files>\d[\d\s,]*)",
    re.IGNORECASE,
)

#: ``... (обработано файлов 7/14)``
_MULTI_PROGRESS_RE = re.compile(
    r"\(обработано файлов\s+(?P<current>\d[\d\s,]*)\s*/\s*"
    r"(?P<total>\d[\d\s,]*)\)",
    re.IGNORECASE,
)

#: ``[INFO] Готово: обработано 12345 операций``
_DONE_RE = re.compile(
    r"^\[INFO\]\s*Готово:\s*обработано\s+(?P<operations>[\d\s.,]+?)"
    r"\s+операц",
    re.IGNORECASE,
)

#: ``[INFO] Скрипт успешно завершен`` -- success is decided by row counts.
_FINISHED_RE = re.compile(
    r"^\[INFO\]\s*Скрипт успешно заверш[её]н", re.IGNORECASE
)

#: ``Час 1/3:`` prefix of a tqdm tick.  ``hours_total`` is authoritative.
_TQDM_PREFIX_RE = re.compile(
    r"^\s*Час\s+(?P<index>\d+)\s*/\s*(?P<total>\d+)\s*:"
)

#: ``45%`` in a tqdm tick.
_TQDM_PCT_RE = re.compile(r"(?P<pct>\d+(?:[.,]\d+)?)\s*%")

#: ``45.0k/100.0k`` in a tqdm tick.
_TQDM_COUNTS_RE = re.compile(
    r"(?P<current>\d[\d.,]*\s*[kKmMgGtTpPeEzZyY]?)\s*/\s*"
    r"(?P<total>\d[\d.,]*\s*[kKmMgGtTpPeEzZyY]?)"
)

#: ``12.3kстрок`` when tqdm has no total.  The lookahead skips the rate,
#: which is printed as ``2.40kстрок/s``.
_TQDM_CURRENT_RE = re.compile(
    r"(?P<current>\d[\d.,]*\s*[kKmMgGtTpPeEzZyY]?)\s*строк(?!/)"
)

#: ``Операции=120`` postfix set by LogT with ``pbar.set_postfix``.
_OPERATIONS_RE = re.compile(r"Операции\s*=\s*(?P<operations>\d[\d\s,]*)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _last_carriage_return_segment(text: str) -> str:
    """Return the last non-empty ``\\r``-separated segment of ``text``.

    ``tqdm`` redraws its bar with a leading ``\\r``.  A reader may hand over
    several ticks in one chunk; only the last one is current.
    """
    if "\r" not in text:
        return text
    segments = [segment for segment in text.split("\r") if segment.strip()]
    return segments[-1] if segments else ""


def _clean_line(line: str) -> str:
    """Strip ANSI escapes, control characters and stale ``\\r`` segments."""
    if not isinstance(line, str):
        return ""
    text = _ANSI_RE.sub("", line)
    text = _last_carriage_return_segment(text)
    text = _CONTROL_RE.sub("", text)
    return text.strip()


def _parse_number(text: str | None) -> float | None:
    """Parse a LogT/tqdm number, including ``k``/``M`` suffixes.

    ``10.0k``, ``1.23M`` and ``500`` are supported, as is a comma used as the
    decimal separator (``1,5k``).  A comma used as a thousands separator
    (``1,234``) is detected when every following group has exactly three
    digits.  Non-numeric input returns ``None``.

    Args:
        text: Raw number text, possibly with spaces or a scale suffix.

    Returns:
        The number as a ``float``, scaled by its suffix, or ``None``.
    """
    if text is None:
        return None
    cleaned = text.replace("\u00a0", " ").replace(" ", "").strip()
    if not cleaned:
        return None
    match = _NUMBER_RE.match(cleaned)
    if match is None:
        return None

    number_text = match.group("num")
    if "," in number_text and "." in number_text:
        number_text = number_text.replace(",", "")
    elif "," in number_text:
        groups = number_text.split(",")
        if (
            len(groups) > 1
            and groups[0].isdigit()
            and all(len(group) == 3 for group in groups[1:])
        ):
            number_text = "".join(groups)
        else:
            number_text = number_text.replace(",", ".")

    try:
        value = float(number_text)
    except ValueError:  # pragma: no cover - the regex already guarantees digits
        return None
    value *= _SCALES.get(match.group("suffix").lower(), 1.0)
    # A pathological log line must never poison the progress bar or crash the
    # pump thread with ``OverflowError``.
    return value if math.isfinite(value) else None


def _to_int(value: float | None) -> int | None:
    """Round a parsed number to a non-negative ``int`` (or ``None``)."""
    if value is None:
        return None
    try:
        number = int(round(value))
    except (OverflowError, ValueError):  # pragma: no cover - guarded upstream
        return None
    return max(0, number)


def _clamp_percent(value: float) -> float:
    """Clamp an overall percentage to the ``0..100`` range."""
    return min(100.0, max(0.0, value))


def _looks_like_tqdm_tick(text: str) -> bool:
    """Heuristic used for ticks that do not carry the ``Час i/H:`` prefix."""
    if _LOG_PREFIX_RE.match(text):
        return False
    if "строк/s" in text or "строк/с" in text:
        return True
    if text.count("|") >= 2:
        return True
    return "%" in text and "|" in text


def is_tqdm_line(line: str) -> bool:
    """Return ``True`` when ``line`` is a ``tqdm`` progress tick.

    The predicate is used by the adapter (P1-03) to keep the diagnostic
    ``stderr_tail`` free of bar redraws.  A regular LogT ``[INFO]`` line,
    including ``[INFO] Час ...: файлов N``, is never a ``tqdm`` line.

    Args:
        line: One raw line/chunk from stdout or stderr.

    Returns:
        ``True`` for a bar tick (Unicode or ASCII bar, optional total,
        optional timing), ``False`` otherwise.
    """
    text = _clean_line(line)
    if not text:
        return False
    if _LOG_PREFIX_RE.match(text):
        return False
    if _TQDM_PREFIX_RE.match(text):
        return True
    return _looks_like_tqdm_tick(text)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


class LogTProgressParser:
    """Turn LogT output lines into :class:`ImportProgress` snapshots.

    One parser instance follows one import.  Both ``stdout`` and ``stderr``
    may be fed into the same instance; the parser tolerates the two streams
    arriving out of order.  It does not throttle and never calls the GUI.

    Attributes are private on purpose: the snapshot is the only public
    output.  Call :meth:`reset` before reusing the instance for a new import.

    Example (the numbers are illustrative, not a doctest)::

        parser = LogTProgressParser()
        parser.feed_stdout("[INFO] Часовых групп: 3")
        snapshot = parser.feed_stderr(
            "Час 1/3:  45%|##   | 45.0k/100.0k [00:12<00:14, 3.80kстрок/s]"
        )
        assert snapshot is not None and snapshot.percent == 15.0
    """

    def __init__(self) -> None:
        """Create a parser with empty progress state."""
        self.reset()

    # -- lifecycle ---------------------------------------------------------

    def reset(self) -> None:
        """Forget everything about the current import.

        The next snapshot starts from scratch: unknown hours, zero hour
        fraction and no remembered percentage.  Idempotent.
        """
        self._hours_total: int | None = None
        self._hour_index: int | None = None
        self._hour_fraction: float = 0.0
        self._hour_key: str | None = None
        self._files_current: int | None = None
        self._files_total: int | None = None
        self._last_percent: float | None = None

    # -- public API --------------------------------------------------------

    def feed_stdout(self, line: str) -> ImportProgress | None:
        """Feed one line (or ``\\r``-joined chunk) read from LogT ``stdout``.

        Args:
            line: Raw line as produced by the subprocess pump.

        Returns:
            A snapshot when the line describes progress, otherwise ``None``.
        """
        return self._feed(line)

    def feed_stderr(self, line: str) -> ImportProgress | None:
        """Feed one line (or ``\\r``-joined chunk) read from LogT ``stderr``.

        ``tqdm`` writes its bar to ``stderr``; ``[INFO]`` lines are accepted
        here too so the parser does not depend on stream redirection.

        Args:
            line: Raw line as produced by the subprocess pump.

        Returns:
            A snapshot when the line describes progress, otherwise ``None``.
        """
        return self._feed(line)

    # -- dispatch ----------------------------------------------------------

    def _feed(self, line: str) -> ImportProgress | None:
        """Clean ``line`` and route it to the first matching handler."""
        text = _clean_line(line)
        if not text:
            return None
        # Success is decided by ``count_tables`` (P1-03), not by this text.
        # Recognising the line explicitly keeps that rule visible here.
        if _FINISHED_RE.match(text):
            return None

        for handler in (
            self._handle_tqdm,
            self._handle_files_found,
            self._handle_hours_total,
            self._handle_hour_start,
            self._handle_multi_process,
            self._handle_multi_progress,
            self._handle_done,
        ):
            snapshot = handler(text)
            if snapshot is not None:
                return snapshot

        # Unknown lines intentionally yield no event: an unrecognised format
        # is not an error, it only means "no progress to show".
        return None

    # -- percentage bookkeeping -------------------------------------------

    def _overall_percent(self) -> float | None:
        """Compute the hour-weighted overall percentage.

        ``None`` is returned while the number of hours is unknown, which lets
        the GUI render an indeterminate bar instead of a fake number.
        """
        if (
            self._hours_total is None
            or self._hours_total <= 0
            or self._hour_index is None
        ):
            return None
        completed = (self._hour_index - 1) + min(1.0, max(0.0, self._hour_fraction))
        # Processing spans 5..90 %: the first five percent are reserved for
        # preparation/schema and the last ten for insertion/counting.  Without
        # the 5 % offset a completed last hour would report 100 % while LogT is
        # still inserting, and the monotonic reporter would freeze the bar there.
        return _clamp_percent(5.0 + 85.0 * completed / self._hours_total)

    def _monotonic(self, percent: float) -> float:
        """Clamp ``percent`` and never return less than the last known value."""
        percent = _clamp_percent(percent)
        if self._last_percent is not None:
            percent = max(percent, self._last_percent)
        self._last_percent = percent
        return percent

    # -- snapshot factory --------------------------------------------------

    @staticmethod
    def _snapshot(
        stage: ImportStage,
        message: str,
        *,
        current: int | None = None,
        total: int | None = None,
        percent: float | None = None,
        detail: str | None = None,
    ) -> ImportProgress:
        """Build an immutable snapshot from already normalised values."""
        return ImportProgress(
            stage=stage,
            message=message,
            current=current,
            total=total,
            percent=percent,
            detail=detail,
        )

    # -- handlers ----------------------------------------------------------

    def _handle_files_found(self, text: str) -> ImportProgress | None:
        """``[INFO] Найдено файлов rphost: N, rmngr: M`` -> scanning, N+M."""
        match = _FILES_FOUND_RE.match(text)
        if match is None:
            return None
        rphost = _to_int(_parse_number(match.group("rphost")))
        rmngr = _to_int(_parse_number(match.group("rmngr")))
        if rphost is None and rmngr is None:
            return None
        rphost = rphost or 0
        rmngr = rmngr or 0
        total = rphost + rmngr
        self._files_current = 0
        self._files_total = total or None
        percent = self._monotonic(0.0)
        return self._snapshot(
            _SCANNING,
            "Сканирование логов",
            current=0,
            total=total or None,
            percent=percent,
            detail=f"rphost: {rphost}, rmngr: {rmngr}",
        )

    def _handle_hours_total(self, text: str) -> ImportProgress | None:
        """``[INFO] Часовых групп: H`` -> remember the hour count."""
        match = _HOURS_TOTAL_RE.match(text)
        if match is None:
            return None
        hours = _to_int(_parse_number(match.group("hours")))
        if hours is None or hours <= 0:
            return None
        self._hours_total = hours
        percent = self._monotonic(0.0)
        return self._snapshot(
            _SCANNING,
            "Сканирование логов",
            current=self._files_current,
            total=self._files_total,
            percent=percent,
            detail=f"часовых групп: {hours}",
        )

    def _handle_hour_start(self, text: str) -> ImportProgress | None:
        """``[INFO] Час KEY: файлов F`` -> a new hour begins processing."""
        match = _HOUR_START_RE.match(text)
        if match is None:
            return None
        key = " ".join(match.group("key").split())
        files = _to_int(_parse_number(match.group("files")))
        self._hour_key = key
        self._hour_fraction = 0.0
        detail = f"час {key}"
        if files is not None:
            detail = f"{detail}, файлов {files}"
        return self._snapshot(
            _PROCESSING,
            "Обработка логов",
            percent=self._last_percent,
            detail=detail,
        )

    def _handle_multi_process(self, text: str) -> ImportProgress | None:
        """``[INFO] Процесс i/N NAME: каталогов D, файлов F`` (multi mode)."""
        match = _MULTI_PROCESS_RE.match(text)
        if match is None:
            return None
        files = _to_int(_parse_number(match.group("files")))
        self._files_current = 0
        self._files_total = files or None
        percent = self._monotonic(0.0)
        return self._snapshot(
            _SCANNING,
            "Сканирование логов",
            current=0,
            total=files or None,
            percent=percent,
            detail=(
                f"процесс {match.group('index')}/{match.group('total')} "
                f"{match.group('name')}"
            ),
        )

    def _handle_multi_progress(self, text: str) -> ImportProgress | None:
        """``... (обработано файлов X/Y)`` -> scanning file counter (multi)."""
        match = _MULTI_PROGRESS_RE.search(text)
        if match is None:
            return None
        current = _to_int(_parse_number(match.group("current")))
        total = _to_int(_parse_number(match.group("total")))
        if current is None or total is None:
            return None
        self._files_current = current
        self._files_total = total or None
        percent = self._monotonic(0.0)
        return self._snapshot(
            _SCANNING,
            "Объединение логов",
            current=current,
            total=total or None,
            percent=percent,
            detail=f"обработано файлов {current}/{total}",
        )

    def _handle_done(self, text: str) -> ImportProgress | None:
        """``[INFO] Готово: обработано O операций`` -> inserting, 92 %."""
        match = _DONE_RE.match(text)
        if match is None:
            return None
        operations = _to_int(_parse_number(match.group("operations")))
        percent = self._monotonic(INSERTING_PERCENT)
        detail = "запись операций"
        if operations is not None:
            detail = f"обработано операций: {operations}"
        return self._snapshot(
            _INSERTING,
            "Запись операций в ClickHouse",
            current=operations,
            total=None,
            percent=percent,
            detail=detail,
        )

    def _handle_tqdm(self, text: str) -> ImportProgress | None:
        """Parse a ``tqdm`` tick, with or without the ``Час i/H:`` prefix."""
        prefix = _TQDM_PREFIX_RE.match(text)
        if prefix is not None:
            rest = text[prefix.end():]
            # A ``Час i/H:`` prefix alone (e.g. ``Час 1/3: файлов 12`` without
            # the ``[INFO]`` marker) carries no progress data; do not emit an
            # event and do not touch the state.
            if not any(token in rest for token in ("%", "|", "строк")):
                return None
            index = _to_int(_parse_number(prefix.group("index")))
            hours_total = _to_int(_parse_number(prefix.group("total")))
            if index is None or index <= 0 or not hours_total:
                return None
            self._hour_index = index
            self._hours_total = hours_total
        else:
            if not _looks_like_tqdm_tick(text):
                return None
            rest = text

        percent_match = _TQDM_PCT_RE.search(rest)
        pct = _parse_number(percent_match.group("pct")) if percent_match else None

        counts = _TQDM_COUNTS_RE.search(rest)
        current: float | None = None
        total: float | None = None
        if counts is not None:
            current = _parse_number(counts.group("current"))
            total = _parse_number(counts.group("total"))
        if current is None:
            single = _TQDM_CURRENT_RE.search(rest)
            if single is not None:
                current = _parse_number(single.group("current"))

        fraction: float | None
        if current is not None and total is not None and total > 0:
            fraction = min(1.0, max(0.0, current / total))
        elif pct is not None:
            # A bar with a percentage but no counters still tells us where
            # inside the current hour we are.
            fraction = min(1.0, max(0.0, pct / 100.0))
        elif current is not None:
            # Count is known, total is not: the hour has only just started.
            fraction = 0.0
        else:
            fraction = None
        if fraction is not None:
            self._hour_fraction = fraction

        overall = self._overall_percent()
        if overall is not None:
            overall = self._monotonic(overall)

        detail_parts: list[str] = []
        if self._hour_index is not None and self._hours_total:
            detail_parts.append(f"час {self._hour_index}/{self._hours_total}")
        elif self._hour_key:
            detail_parts.append(f"час {self._hour_key}")
        operations_match = _OPERATIONS_RE.search(text)
        if operations_match is not None:
            operations = _to_int(_parse_number(operations_match.group("operations")))
            if operations is not None:
                detail_parts.append(f"Операции={operations}")

        return self._snapshot(
            _PROCESSING,
            "Обработка логов",
            current=_to_int(current),
            total=_to_int(total),
            percent=overall,
            detail=", ".join(detail_parts) if detail_parts else None,
        )
