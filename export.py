"""CSV, JSON and XLSX export for the 1C Log Analyzer.

The GUI used to write CSV files inline, which made the export logic untestable
without Tk and impossible to extend.  This module owns serialisation only: it
has no GUI, ClickHouse or adapter imports, so it can be exercised headless and
reused by the worker thread that pages through ClickHouse results.

Design notes
------------
* **Atomic by contract.**  Every format is written to a sibling ``*.part`` file
  created with :func:`tempfile.mkstemp`, flushed and ``fsync``-ed, and only then
  moved over the destination with :func:`os.replace`.  A failure, a cancelled
  export or a row that breaks validation therefore leaves an existing target
  byte-for-byte untouched and never leaves a partial file behind.
* **CSV compatibility.**  The CSV writer reproduces the historical GUI writer
  exactly: ``utf-8-sig``, ``newline=""`` and :data:`csv.QUOTE_MINIMAL`.
* **Streaming.**  Rows are consumed once, from any iterable, and are never
  collected into a list.  JSON rows are serialised one by one; XLSX uses
  ``Workbook(write_only=True)``.
* **Lazy optional dependency.**  ``openpyxl`` is imported inside
  :func:`_import_openpyxl`, so CSV and JSON keep working when it is missing and
  the frozen build must collect it explicitly (see ``1C_Log_Analyzer.spec``).
* **Spreadsheet injection.**  User-controlled strings beginning with ``=``,
  ``+``, ``-``, ``@``, tab or carriage return get a leading apostrophe unless
  the caller opts out with ``defuse_formulas=False``.  Numbers are never
  altered.

The module is deliberately free of any UI import so the same code can run in
the main thread or in an export worker.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import logging
import os
import re
import tempfile
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, TextIO

logger = logging.getLogger(__name__)

__all__ = [
    "ExportCancelled",
    "ExportDependencyError",
    "ExportError",
    "ExportFormat",
    "ExportFormatError",
    "ExportLimitExceeded",
    "ExportResult",
    "defuse_formula",
    "export_rows",
    "filetypes",
    "iter_paged_rows",
    "iter_records",
    "openpyxl_available",
    "suggest_filename",
]

#: How often (in rows) a progress callback is invoked, besides the final call.
PROGRESS_EVERY = 1_000

_JSON_SCHEMA_VERSION = 1
_JSON_RESERVED_KEYS = frozenset(
    {"schema_version", "exported_at", "row_count", "truncated", "columns", "rows"}
)

_EXCEL_MAX_DATA_ROWS = 1_048_575
_EXCEL_MAX_CELL_LENGTH = 32_767
_EXCEL_DATA_SHEET = "Данные"
_EXCEL_METADATA_SHEET = "Метаданные"

#: Characters Excel refuses to store.  Tab, newline and carriage return are
#: legal and therefore deliberately not matched.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class ExportFormat(str, Enum):
    """Supported output formats.

    The ``str`` mixin keeps the enum friendly to ``filedialog`` and to code
    that stores the chosen format in a config file.  Lookup is
    case-insensitive: ``ExportFormat("CSV")`` and ``ExportFormat("xlsx")`` both
    work, anything else raises :class:`ExportFormatError`.
    """

    CSV = "csv"
    JSON = "json"
    XLSX = "xlsx"

    @classmethod
    def _missing_(cls, value: object) -> "ExportFormat | None":
        """Resolve a case-insensitive string value or raise a clear error."""
        if isinstance(value, str):
            lowered = value.strip().lower()
            for member in cls:
                if member.value == lowered:
                    return member
        raise ExportFormatError(f"unsupported export format: {value!r}")


class ExportError(Exception):
    """Base class for every export failure."""


class ExportFormatError(ExportError, ValueError):
    """The requested output format is not supported."""


class ExportDependencyError(ExportError, ImportError):
    """An optional third-party package needed for the format is missing."""


class ExportLimitExceeded(ExportError):
    """The data has more rows than the caller or the format allows."""


class ExportCancelled(ExportError):
    """The caller requested cancellation through a :class:`threading.Event`."""


@dataclass(frozen=True, slots=True)
class ExportResult:
    """Description of a completed export.

    Attributes:
        path: Absolute path of the written file.
        fmt: Format that was actually written.
        row_count: Number of data rows written (excluding the header).
        truncated: ``True`` when rows were dropped because of ``max_rows`` or
            the XLSX row limit.
        columns: Column headers in export order.
        file_size: Size of the finished file in bytes.
    """

    path: str
    fmt: ExportFormat
    row_count: int
    truncated: bool
    columns: list[str]
    file_size: int = 0


_FILETYPES: dict[ExportFormat, tuple[str, str]] = {
    ExportFormat.CSV: ("CSV files", "*.csv"),
    ExportFormat.JSON: ("JSON files", "*.json"),
    ExportFormat.XLSX: ("Excel files", "*.xlsx"),
}


def defuse_formula(value: Any) -> Any:
    """Neutralise a spreadsheet formula in a user-controlled string.

    Values beginning with ``=``, ``+``, ``-``, ``@``, tab or carriage return can
    be interpreted as formulas by Excel and LibreOffice.  A leading apostrophe
    forces the cell to stay text.  Non-string values (including numbers, which
    legitimately start with a minus sign) are returned unchanged.

    Args:
        value: Any cell value.

    Returns:
        The value with a leading apostrophe when it looks like a formula,
        otherwise the original value.
    """
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def iter_records(
    rows: Iterable[Mapping[str, Any]] | Iterable[Sequence[Any]],
    headers: Sequence[str],
    keys: Sequence[str] | None = None,
) -> Iterator[list[Any]]:
    """Yield row values in header order, validating their width.

    Args:
        rows: Mapping rows (for example ClickHouse dictionaries) or sequence
            rows (for example values read from a tree view).
        headers: Column headers defining the expected width.
        keys: Mapping keys to read for each header position.  Defaults to
            ``headers`` when the rows are mappings; ignored for sequences.

    Yields:
        One list of values per row, exactly ``len(headers)`` long.

    Raises:
        ExportError: If ``headers``/``keys`` are malformed or a row has a
            different width than the header row.
    """
    if isinstance(headers, (str, bytes)):
        raise ExportError("headers must be a sequence of column names")
    header_list = [header if isinstance(header, str) else str(header) for header in headers]

    if keys is not None:
        if isinstance(keys, (str, bytes)):
            raise ExportError("keys must be a sequence of mapping keys")
        key_list: list[Any] | None = list(keys)
        if len(key_list) != len(header_list):
            raise ExportError(
                f"keys has {len(key_list)} entries, expected {len(header_list)}"
            )
    else:
        key_list = None

    width = len(header_list)
    try:
        row_iterator = iter(rows)
    except TypeError as exc:
        raise ExportError(
            "rows must be an iterable of mappings or sequences"
        ) from exc
    for index, row in enumerate(row_iterator, start=1):
        if isinstance(row, Mapping):
            mapping_keys = key_list if key_list is not None else header_list
            values = [row.get(key, "") for key in mapping_keys]
        elif isinstance(row, (str, bytes, bytearray)):
            raise ExportError(f"row {index} is a string, expected a mapping or sequence")
        else:
            try:
                values = list(row)
            except TypeError as exc:
                raise ExportError(
                    f"row {index} is neither a mapping nor a sequence: {row!r}"
                ) from exc
        if len(values) != width:
            raise ExportError(
                f"row {index} has {len(values)} values, expected {width}"
            )
        yield values


def iter_paged_rows(
    fetch_page: Callable[[int, int], Iterable[Any]],
    total: int,
    page_size: int = 500,
    max_rows: int | None = None,
    cancel: threading.Event | None = None,
    progress: Callable[[int], None] | None = None,
) -> Iterator[Any]:
    """Yield rows from a paged source, stopping on ``total``, short pages or limits.

    ``fetch_page(offset, limit)`` must return at most ``limit`` rows starting at
    ``offset``.  The iterator never asks for more than ``total`` rows and also
    stops as soon as a page is shorter than requested, so a source that ignores
    the offset cannot loop forever.

    Args:
        fetch_page: Callable returning a page of rows.
        total: Expected number of rows (``0`` performs no requests at all).
        page_size: Maximum page size requested.
        max_rows: Hard cap on yielded rows; ``None`` means unlimited.
        cancel: Optional event checked before every fetch and every row.
        progress: Optional callback receiving the number of rows yielded so
            far, throttled to every :data:`PROGRESS_EVERY` rows plus a final
            call.

    Yields:
        Rows exactly as returned by ``fetch_page``.

    Raises:
        ValueError: If ``total``, ``page_size`` or ``max_rows`` is invalid.
        ExportCancelled: When ``cancel`` is set.
    """
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise ValueError("total must be a non-negative integer")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or page_size < 1:
        raise ValueError("page_size must be a positive integer")
    if max_rows is not None and (
        isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 0
    ):
        raise ValueError("max_rows must be a non-negative integer or None")

    offset = 0
    yielded = 0
    last_notified: int | None = None

    def notify(*, force: bool = False) -> None:
        nonlocal last_notified
        if progress is None:
            return
        if not force and yielded % PROGRESS_EVERY != 0:
            return
        if yielded == last_notified:
            return
        last_notified = yielded
        try:
            progress(yielded)
        except Exception:  # pragma: no cover - defensive, a UI callback
            logger.warning("export progress callback raised; ignoring", exc_info=True)

    while offset < total and (max_rows is None or yielded < max_rows):
        _check_cancel(cancel)
        limit = page_size
        if max_rows is not None:
            limit = min(limit, max_rows - yielded)
        page = list(fetch_page(offset, limit))
        if not page:
            break
        offset += len(page)
        for row in page:
            _check_cancel(cancel)
            if yielded >= total:
                notify(force=True)
                return
            yield row
            yielded += 1
            if max_rows is not None and yielded >= max_rows:
                notify(force=True)
                return
            notify()
        if len(page) < limit:
            break
    notify(force=True)


def openpyxl_available() -> bool:
    """Return ``True`` when the optional XLSX dependency can be imported."""
    try:
        return importlib.util.find_spec("openpyxl") is not None
    except (ImportError, ValueError):  # pragma: no cover - broken installation
        return False


def suggest_filename(
    prefix: str,
    fmt: ExportFormat | str = ExportFormat.CSV,
    timestamp: datetime | None = None,
) -> str:
    """Build a safe default file name for a save dialog.

    Args:
        prefix: Human-readable prefix (for example ``"ошибки"``); characters
            that are illegal on Windows are replaced with underscores.
        fmt: Output format.
        timestamp: Moment used in the name; defaults to ``datetime.now()``.

    Returns:
        A file name such as ``errors_20261112_101500.csv``.
    """
    format_value = _coerce_format(fmt)
    moment = timestamp or datetime.now()
    if not isinstance(moment, datetime):
        raise ValueError("timestamp must be a datetime")
    safe = _INVALID_FILENAME_CHARS.sub("_", str(prefix)).strip(" .")
    if not safe:
        safe = "export"
    return f"{safe}_{moment.strftime('%Y%m%d_%H%M%S')}.{format_value.value}"


def filetypes(fmt: ExportFormat | str | None = None) -> list[tuple[str, str]]:
    """Return file-dialog file types as plain ``(label, pattern)`` tuples.

    Args:
        fmt: Restrict the list to one format; ``None`` returns all formats.

    Returns:
        A list of tuples suitable for ``filedialog.asksaveasfilename``.  The
        module returns plain tuples so it does not need a UI toolkit.
    """
    if fmt is None:
        entries = [_FILETYPES[member] for member in ExportFormat]
    else:
        entries = [_FILETYPES[_coerce_format(fmt)]]
    return [*entries, ("All files", "*.*")]


def export_rows(
    headers: Sequence[str],
    rows: Iterable[Mapping[str, Any]] | Iterable[Sequence[Any]],
    path: str | os.PathLike[str],
    fmt: ExportFormat | str = ExportFormat.CSV,
    *,
    keys: Sequence[str] | None = None,
    max_rows: int | None = 100_000,
    allow_truncate: bool = False,
    defuse_formulas: bool = True,
    progress: Callable[[int], None] | None = None,
    cancel: threading.Event | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ExportResult:
    """Write ``rows`` to ``path`` in the requested format, atomically.

    Args:
        headers: Column headers, in export order.
        rows: Mapping or sequence rows.  Consumed lazily, exactly once.
        path: Destination file.  The parent directory must already exist.
        fmt: Output format (enum or case-insensitive string).
        keys: Mapping keys per column position; defaults to ``headers``.
        max_rows: Row cap.  ``None`` means unlimited, otherwise XLSX is still
            capped at Excel's 1 048 575 data rows.
        allow_truncate: When ``True`` an over-limit export stops and reports
            ``truncated=True`` instead of raising :class:`ExportLimitExceeded`.
        defuse_formulas: Prefix formula-like strings with an apostrophe.
        progress: Callback receiving the number of rows written so far.  Called
            every :data:`PROGRESS_EVERY` rows and once at the end, always from
            the calling thread.
        cancel: Event checked before every row.  When set, the temporary file
            is removed and :class:`ExportCancelled` is raised.
        metadata: Extra JSON/XLSX metadata (``source``, ``dataset``,
            ``filters``).  ``schema_version``, ``exported_at``, ``row_count``,
            ``truncated``, ``columns`` and ``rows`` are authoritative and
            cannot be overridden.

    Returns:
        An :class:`ExportResult` describing the written file.

    Raises:
        ExportFormatError: Unknown format.
        ExportDependencyError: XLSX requested without ``openpyxl``.
        ExportLimitExceeded: More rows than allowed and truncation is off.
        ExportCancelled: ``cancel`` was set.
        ExportError: Row/header validation or filesystem failure.
        ValueError: Duplicate headers (JSON only) or invalid arguments.
    """
    format_value = _coerce_format(fmt)
    header_list = _normalise_headers(headers)
    _validate_max_rows(max_rows)
    if metadata is not None and not isinstance(metadata, Mapping):
        raise ExportError("metadata must be a mapping")
    if format_value is ExportFormat.JSON:
        _ensure_unique_headers(header_list)

    if format_value is ExportFormat.XLSX:
        limit = (
            _EXCEL_MAX_DATA_ROWS
            if max_rows is None
            else min(max_rows, _EXCEL_MAX_DATA_ROWS)
        )
    else:
        limit = max_rows

    _check_cancel(cancel)
    target = _absolute_target(path)
    pump = _RowPump(
        iter_records(rows, header_list, keys),
        max_rows=limit,
        allow_truncate=allow_truncate,
        cancel=cancel,
        progress=progress,
    )

    try:
        if format_value is ExportFormat.CSV:
            _write_atomic_text(
                target,
                lambda handle: _write_csv(handle, header_list, pump, defuse_formulas),
                encoding="utf-8-sig",
            )
        elif format_value is ExportFormat.JSON:
            _write_atomic_text(
                target,
                lambda handle: _write_json(
                    handle,
                    header_list,
                    pump,
                    defuse_formulas,
                    metadata,
                ),
                encoding="utf-8",
            )
        else:
            _write_atomic_binary(
                target,
                lambda temp_path: _write_xlsx(
                    temp_path,
                    header_list,
                    pump,
                    defuse_formulas,
                    metadata,
                ),
            )
    except ExportError:
        raise
    except OSError as exc:
        raise ExportError(f"failed to write {target}: {exc}") from exc

    pump.finish()
    try:
        file_size = os.path.getsize(target)
    except OSError:  # pragma: no cover - the file was just written
        file_size = 0
    return ExportResult(
        path=target,
        fmt=format_value,
        row_count=pump.count,
        truncated=pump.truncated,
        columns=header_list,
        file_size=file_size,
    )


# ----------------------------------------------------------------------
# Internals: validation, limits and progress
# ----------------------------------------------------------------------


class _RowPump:
    """Iterate export records while enforcing limits, cancellation and progress."""

    def __init__(
        self,
        records: Iterable[list[Any]],
        *,
        max_rows: int | None,
        allow_truncate: bool,
        cancel: threading.Event | None,
        progress: Callable[[int], None] | None,
    ) -> None:
        self._records = records
        self._max_rows = max_rows
        self._allow_truncate = allow_truncate
        self._cancel = cancel
        self._progress = progress
        self._last_notified: int | None = None
        self.count = 0
        self.truncated = False

    def __iter__(self) -> Iterator[list[Any]]:
        for record in self._records:
            _check_cancel(self._cancel)
            if self._max_rows is not None and self.count >= self._max_rows:
                if not self._allow_truncate:
                    raise ExportLimitExceeded(
                        f"export would exceed the limit of {self._max_rows} rows"
                    )
                self.truncated = True
                return
            self.count += 1
            if self.count % PROGRESS_EVERY == 0:
                self._emit()
            yield record

    def finish(self) -> None:
        """Emit the final progress value (including zero rows)."""
        self._emit()

    def _emit(self) -> None:
        if self._progress is None or self.count == self._last_notified:
            return
        self._last_notified = self.count
        try:
            self._progress(self.count)
        except Exception:  # pragma: no cover - defensive, a UI callback
            logger.warning("export progress callback raised; ignoring", exc_info=True)


def _coerce_format(fmt: ExportFormat | str) -> ExportFormat:
    """Return ``fmt`` as an :class:`ExportFormat` or raise."""
    if isinstance(fmt, ExportFormat):
        return fmt
    if isinstance(fmt, str):
        return ExportFormat(fmt)
    raise ExportFormatError(f"unsupported export format: {fmt!r}")


def _normalise_headers(headers: Sequence[str]) -> list[str]:
    """Convert headers to a list of strings, validating the container."""
    if headers is None or isinstance(headers, (str, bytes)):
        raise ExportError("headers must be a sequence of column names")
    try:
        return [header if isinstance(header, str) else str(header) for header in headers]
    except TypeError as exc:
        raise ExportError("headers must be a sequence of column names") from exc


def _ensure_unique_headers(headers: Sequence[str]) -> None:
    """Raise :class:`ValueError` when a JSON header repeats.

    JSON objects cannot represent two columns with the same name, so a repeated
    header would silently drop a column from every row.
    """
    duplicates = sorted({header for header in headers if headers.count(header) > 1})
    if duplicates:
        raise ValueError(f"duplicate column headers: {', '.join(duplicates)}")


def _validate_max_rows(max_rows: int | None) -> None:
    """Validate the ``max_rows`` argument."""
    if max_rows is None:
        return
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 0:
        raise ValueError("max_rows must be a non-negative integer or None")


def _check_cancel(cancel: threading.Event | None) -> None:
    """Raise :class:`ExportCancelled` when the event is set."""
    if cancel is not None and cancel.is_set():
        raise ExportCancelled("export cancelled")


# ----------------------------------------------------------------------
# Internals: atomic files
# ----------------------------------------------------------------------


def _absolute_target(path: str | os.PathLike[str]) -> str:
    """Return an absolute target path or raise :class:`ExportError`."""
    try:
        target = os.fsdecode(os.fspath(path))
    except TypeError as exc:
        raise ExportError(f"invalid export path: {path!r}") from exc
    target = os.path.abspath(target)
    if os.path.isdir(target):
        raise ExportError(f"export target is a directory: {target}")
    directory = os.path.dirname(target) or "."
    if not os.path.isdir(directory):
        raise ExportError(f"destination directory does not exist: {directory}")
    return target


def _temp_sibling(target: str) -> tuple[int, str]:
    """Create a hidden ``*.part`` file next to ``target``.

    Returns:
        ``(file_descriptor, path)`` of the temporary file.
    """
    directory = os.path.dirname(target) or "."
    base = os.path.basename(target) or "export"
    prefix = f".{base}." if len(base) <= 80 else ".export."
    try:
        return tempfile.mkstemp(prefix=prefix, suffix=".part", dir=directory)
    except OSError as exc:
        raise ExportError(f"cannot create a temporary file in {directory}: {exc}") from exc


def _discard(fd: int, temp_path: str) -> None:
    """Close an owned descriptor and remove a temporary file, ignoring errors."""
    if fd >= 0:
        try:
            os.close(fd)
        except OSError:  # pragma: no cover - already closed
            pass
    try:
        os.unlink(temp_path)
    except OSError:
        pass


def _write_atomic_text(
    target: str,
    writer: Callable[[TextIO], None],
    *,
    encoding: str,
) -> None:
    """Write text through ``writer`` and atomically replace ``target``."""
    fd, temp_path = _temp_sibling(target)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as handle:
            fd = -1
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    finally:
        _discard(fd, temp_path)


def _write_atomic_binary(target: str, writer: Callable[[str], None]) -> None:
    """Let ``writer`` fill a temporary path, then atomically replace ``target``."""
    fd, temp_path = _temp_sibling(target)
    try:
        os.close(fd)
        fd = -1
        writer(temp_path)
        # ``fsync`` needs a writable handle on Windows, hence ``ab`` and no
        # truncation; the file was just written by ``writer``.
        with open(temp_path, "ab") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, target)
    finally:
        _discard(fd, temp_path)


# ----------------------------------------------------------------------
# Internals: CSV
# ----------------------------------------------------------------------


def _write_csv(
    handle: TextIO,
    headers: Sequence[str],
    records: Iterable[list[Any]],
    defuse: bool,
) -> None:
    """Write a CSV document identical to the historical GUI writer."""
    writer = csv.writer(handle, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(headers)
    for record in records:
        if defuse:
            record = [defuse_formula(value) for value in record]
        writer.writerow(record)


# ----------------------------------------------------------------------
# Internals: JSON
# ----------------------------------------------------------------------


def _json_default(value: Any) -> Any:
    """Serialise values the standard encoder does not know about."""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return str(value)


def _write_json(
    handle: TextIO,
    headers: Sequence[str],
    records: Iterable[list[Any]],
    defuse: bool,
    metadata: Mapping[str, Any] | None,
) -> None:
    """Stream a JSON document with a metadata header and one object per row.

    ``row_count`` and ``truncated`` are only known once the rows have been
    consumed, so they are emitted after the ``rows`` array.  JSON objects are
    unordered, so consumers see the documented keys either way.
    """
    head: dict[str, Any] = {
        "schema_version": _JSON_SCHEMA_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }
    if metadata:
        for key, value in metadata.items():
            name = key if isinstance(key, str) else str(key)
            if name in _JSON_RESERVED_KEYS or name in head:
                continue
            head[name] = value
    head["columns"] = list(headers)

    encoded_head = json.dumps(head, ensure_ascii=False, default=_json_default)
    handle.write(encoded_head[:-1])
    handle.write(', "rows": [')
    first = True
    for record in records:
        if defuse:
            record = [defuse_formula(value) for value in record]
        row = dict(zip(headers, record))
        text = json.dumps(row, ensure_ascii=False, default=_json_default)
        handle.write("\n    " if first else ",\n    ")
        handle.write(text)
        first = False
    handle.write("]" if first else "\n  ]")
    truncated = "true" if getattr(records, "truncated", False) else "false"
    handle.write(f', "truncated": {truncated}')
    handle.write(f', "row_count": {getattr(records, "count", 0)}')
    handle.write("}\n")


# ----------------------------------------------------------------------
# Internals: XLSX
# ----------------------------------------------------------------------


def _import_openpyxl() -> Any:
    """Import ``openpyxl`` lazily.

    Returns:
        The imported ``openpyxl`` module.

    Raises:
        ExportDependencyError: If the package is not installed.
    """
    try:
        import openpyxl
    except ImportError as exc:
        raise ExportDependencyError(
            "XLSX export requires openpyxl; install it with: pip install openpyxl"
        ) from exc
    return openpyxl


def _xlsx_value(value: Any, defuse: bool) -> Any:
    """Convert a cell value for XLSX: strip control chars, defuse, truncate."""
    if value is None:
        return None
    if isinstance(value, str):
        text = _CONTROL_CHARS.sub("", value)
        if defuse:
            text = defuse_formula(text)
        if len(text) > _EXCEL_MAX_CELL_LENGTH:
            text = text[:_EXCEL_MAX_CELL_LENGTH]
        return text
    if isinstance(value, (bool, int, float, Decimal, datetime, date, time)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return str(value)


def _metadata_value(value: Any) -> Any:
    """Render a metadata value for the metadata worksheet."""
    if isinstance(value, (str, bool, int, float, datetime, date, time, Decimal)):
        return _xlsx_value(value, False)
    if value is None:
        return ""
    try:
        return _xlsx_value(
            json.dumps(value, ensure_ascii=False, default=_json_default), False
        )
    except (TypeError, ValueError):  # pragma: no cover - exotic metadata
        return _xlsx_value(str(value), False)


def _xlsx_row(sheet: Any, values: Sequence[Any], cell_factory: Any) -> list[Any]:
    """Build write-only cells, forcing every string to stay text.

    ``WriteOnlyCell`` guesses ``data_type`` from the value, so a string that
    starts with ``=`` would become a live formula.  Export data is never
    executable, therefore the type is always forced to ``s``.
    """
    cells = []
    for value in values:
        cell = cell_factory(sheet, value=value)
        if isinstance(value, str):
            cell.data_type = "s"
        cells.append(cell)
    return cells


def _set_column_widths(sheet: Any, widths: Sequence[float]) -> None:
    """Apply column widths, ignoring worksheets that cannot store them."""
    try:
        from openpyxl.utils import get_column_letter
    except ImportError:  # pragma: no cover - openpyxl is present by now
        return
    for index, width in enumerate(widths, start=1):
        try:
            sheet.column_dimensions[get_column_letter(index)].width = width
        except (AttributeError, KeyError, ValueError):  # pragma: no cover - old openpyxl
            return


def _write_xlsx(
    temp_path: str,
    headers: Sequence[str],
    records: Iterable[list[Any]],
    defuse: bool,
    metadata: Mapping[str, Any] | None,
) -> None:
    """Write an XLSX workbook to ``temp_path`` using the write-only mode."""
    _import_openpyxl()
    from openpyxl import Workbook

    workbook = Workbook(write_only=True)
    try:
        _populate_xlsx_workbook(workbook, headers, records, defuse, metadata)
        workbook.save(temp_path)
    finally:
        # A cancelled or failed export must not leave openpyxl's row generators
        # holding temporary files open until garbage collection.
        _abort_xlsx_workbook(workbook)


def _populate_xlsx_workbook(
    workbook: Any,
    headers: Sequence[str],
    records: Iterable[list[Any]],
    defuse: bool,
    metadata: Mapping[str, Any] | None,
) -> None:
    """Fill a write-only workbook with the data and metadata worksheets."""
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    sheet = workbook.create_sheet(_EXCEL_DATA_SHEET)
    try:
        sheet.freeze_panes = "A2"
    except (AttributeError, TypeError):  # pragma: no cover - old openpyxl
        logger.debug("freeze panes are not supported by this worksheet")

    bold = Font(bold=True)
    header_cells = _xlsx_row(
        sheet, [_xlsx_value(header, False) for header in headers], WriteOnlyCell
    )
    for cell in header_cells:
        cell.font = bold

    # Widths must be registered before the first row is appended: write-only
    # worksheets flush their column dimensions together with the sheet header.
    _set_column_widths(
        sheet,
        [min(60.0, max(10.0, float(len(str(header))) + 4.0)) for header in headers],
    )
    if header_cells:
        sheet.append(header_cells)

    for record in records:
        sheet.append(
            _xlsx_row(sheet, [_xlsx_value(value, defuse) for value in record], WriteOnlyCell)
        )

    if headers:
        try:
            last_column = get_column_letter(len(headers))
            sheet.auto_filter.ref = f"A1:{last_column}{getattr(records, 'count', 0) + 1}"
        except (AttributeError, TypeError, ValueError):  # pragma: no cover - old openpyxl
            logger.debug("auto-filter is not supported by this worksheet; skipping")

    if metadata:
        _write_xlsx_metadata(workbook, metadata, records, WriteOnlyCell, bold)


def _abort_xlsx_workbook(workbook: Any) -> None:
    """Close row generators of a workbook that was not saved successfully."""
    for sheet in getattr(workbook, "worksheets", ()):
        if getattr(sheet, "closed", False):
            continue
        pending = getattr(sheet, "_rows", None)
        if pending is not None:
            try:
                pending.close()
            except Exception:  # pragma: no cover - best-effort cleanup
                logger.debug("could not close an unfinished worksheet", exc_info=True)
    try:
        workbook.close()
    except Exception:  # pragma: no cover - best-effort cleanup
        logger.debug("could not close an unfinished workbook", exc_info=True)



def _write_xlsx_metadata(
    workbook: Any,
    metadata: Mapping[str, Any],
    records: Any,
    cell_factory: Any,
    bold: Any,
) -> None:
    """Append the optional ``Метаданные`` worksheet."""
    sheet = workbook.create_sheet(_EXCEL_METADATA_SHEET)
    _set_column_widths(sheet, [24.0, 60.0])
    header_cells = _xlsx_row(sheet, ["Поле", "Значение"], cell_factory)
    for cell in header_cells:
        cell.font = bold
    sheet.append(header_cells)
    for key, value in metadata.items():
        name = key if isinstance(key, str) else str(key)
        sheet.append(
            _xlsx_row(sheet, [_xlsx_value(name, False), _metadata_value(value)], cell_factory)
        )
    sheet.append(
        _xlsx_row(
            sheet,
            ["row_count", getattr(records, "count", 0)],
            cell_factory,
        )
    )
    sheet.append(
        _xlsx_row(
            sheet,
            ["truncated", bool(getattr(records, "truncated", False))],
            cell_factory,
        )
    )
