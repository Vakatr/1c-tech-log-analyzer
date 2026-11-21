"""Persistent filter presets for the 1C Log Analyzer.

The GUI lets the user save the current filter selection under a name and restore
it later.  This module owns the JSON document, validation and the atomic write
machinery; it has no Tkinter dependency and does not import
:mod:`logt_adapter` (filter objects are duck-typed).

Storage
-------
Presets live in ``<project>/config/presets.json`` by default.  Tests and
deployments may point :class:`PresetStore` at another file with the
``ICS_PRESETS_PATH`` environment variable or the ``path`` argument.

The document looks like::

    {
      "schema_version": 1,
      "last_used": null,
      "presets": [
        {
          "name": "Errors for alice",
          "created_at": "2026-01-02T03:04:05+00:00",
          "updated_at": "2026-01-02T03:04:05+00:00",
          "filters": {"user": "alice", "event_name": "Error"}
        }
      ]
    }

Only the fields in :data:`ALLOWED_FILTER_FIELDS` are ever serialized, so
credentials and connection settings (password, host, log directory) cannot
leak into the presets file.

Failure policy
--------------
* A missing file is an empty store, not an error.
* A corrupt file never crashes the caller: the store keeps working, returns an
  empty list and explains the problem through :attr:`PresetStore.last_error`.
* A file created by a newer schema is read-only; every write raises
  :class:`PresetsValidationError` until :meth:`PresetStore.reset` is called.
* A failed atomic write leaves the previous file untouched and raises
  :class:`PresetsStoreError`.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "ALLOWED_FILTER_FIELDS",
    "DEFAULT_PRESETS_PATH",
    "MAX_DURATION_MS",
    "MAX_FILE_BYTES",
    "MAX_NAME_LENGTH",
    "MAX_PRESETS",
    "MAX_STRING_LENGTH",
    "PresetStore",
    "PresetsError",
    "PresetsStoreError",
    "PresetsValidationError",
    "SCHEMA_VERSION",
    "dict_to_filter_kwargs",
    "filters_to_dict",
]

#: Default location of the presets document (inside the project directory).
DEFAULT_PRESETS_PATH = Path(__file__).resolve().parent / "config" / "presets.json"

#: Version of the JSON document written by this module.
SCHEMA_VERSION = 1

#: Filter attributes that may be persisted.  Everything else is ignored.
ALLOWED_FILTER_FIELDS = (
    "dataset",
    "date_from",
    "date_to",
    "user",
    "event_name",
    "search",
    "process_name",
    "app_id",
    "min_duration_ms",
)

#: Maximum number of named presets in one document.
MAX_PRESETS = 200

#: Refuse to parse a presets file larger than one mebibyte.
MAX_FILE_BYTES = 1024 * 1024

#: Maximum length of a preset name in characters.
MAX_NAME_LENGTH = 80

#: Maximum length of a persisted filter string; longer values are truncated.
MAX_STRING_LENGTH = 1000

#: Largest accepted value of ``min_duration_ms`` (24 hours in milliseconds).
MAX_DURATION_MS = 86_400_000

#: Environment variable that overrides :data:`DEFAULT_PRESETS_PATH`.
PRESETS_ENV_VAR = "ICS_PRESETS_PATH"

#: Number of ``presets.corrupt-*.json`` archives kept next to the store.
MAX_ARCHIVES = 3


class PresetsError(Exception):
    """Base class for every error raised by this module."""


class PresetsValidationError(PresetsError):
    """A name, filter value or JSON document violates the presets contract."""


class PresetsStoreError(PresetsError):
    """The presets file or its directory cannot be read or written."""


def _has_control_characters(text: str) -> bool:
    """Return ``True`` if ``text`` contains C0 or DEL control characters."""
    return any(ord(char) < 32 or ord(char) == 127 for char in text)


def _strip_control_characters(text: str) -> str:
    """Return ``text`` without C0 or DEL control characters."""
    return "".join(
        char for char in text if not (ord(char) < 32 or ord(char) == 127)
    )


def _clean_text(value: object, field: str) -> str | None:
    """Normalize a string filter value.

    Args:
        value: Raw filter value.
        field: Field name used in error messages.

    Returns:
        The stripped value with control characters removed and the length
        capped at :data:`MAX_STRING_LENGTH`, or ``None`` for blank input.

    Raises:
        PresetsValidationError: If ``value`` is not a string.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise PresetsValidationError(f"{field} must be a string")
    text = _strip_control_characters(value).strip()
    if not text:
        return None
    return text[:MAX_STRING_LENGTH]


def _coerce_datetime(value: object, field: str) -> datetime | None:
    """Convert a date/datetime/ISO string into a :class:`datetime`.

    Date-only strings are accepted and interpreted as midnight.

    Raises:
        PresetsValidationError: If the value has an unsupported type or is not
            a valid ISO 8601 date.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text)
        except ValueError as exc:
            raise PresetsValidationError(
                f"{field} is not a valid ISO 8601 date: {text!r}"
            ) from exc
    raise PresetsValidationError(
        f"{field} must be a datetime or an ISO 8601 string"
    )


def _coerce_min_duration(value: object) -> int | None:
    """Normalize ``min_duration_ms``.

    Non-positive values mean "no lower bound" and become ``None``.  Booleans,
    non-integers and values above :data:`MAX_DURATION_MS` are rejected.

    Raises:
        PresetsValidationError: If the value is not a valid duration.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise PresetsValidationError("min_duration_ms must be an integer, not bool")
    if not isinstance(value, int):
        raise PresetsValidationError("min_duration_ms must be an integer")
    if value <= 0:
        return None
    if value > MAX_DURATION_MS:
        raise PresetsValidationError(
            f"min_duration_ms must not exceed {MAX_DURATION_MS}"
        )
    return value


def _clean_filter_record(
    record: Mapping[str, object], *, include_dataset: bool
) -> dict[str, Any]:
    """Validate and normalize a filters mapping.

    Args:
        record: Raw mapping of filter values.
        include_dataset: Keep the ``dataset`` field when it is present.

    Returns:
        A new dictionary containing only allow-listed, normalized values.

    Raises:
        PresetsValidationError: If any present value is invalid or the date
            range is reversed.
    """
    if not isinstance(record, Mapping):
        raise PresetsValidationError("filters must be a JSON object")

    cleaned: dict[str, Any] = {}
    parsed_dates: dict[str, datetime] = {}

    for field in ALLOWED_FILTER_FIELDS:
        if field == "dataset" and not include_dataset:
            continue
        if field not in record:
            continue
        value = record[field]
        if field in ("date_from", "date_to"):
            parsed = _coerce_datetime(value, field)
            if parsed is None:
                continue
            parsed_dates[field] = parsed
            cleaned[field] = parsed.isoformat()
        elif field == "min_duration_ms":
            duration = _coerce_min_duration(value)
            if duration is not None:
                cleaned[field] = duration
        else:
            text = _clean_text(value, field)
            if text is not None:
                cleaned[field] = text

    if len(parsed_dates) == 2:
        try:
            reversed_range = parsed_dates["date_from"] > parsed_dates["date_to"]
        except TypeError as exc:
            raise PresetsValidationError(
                "date_from and date_to must use compatible time zones"
            ) from exc
        if reversed_range:
            raise PresetsValidationError("date_from must not be after date_to")

    return cleaned


def filters_to_dict(filters: object, *, include_dataset: bool = False) -> dict:
    """Serialize a filter object into a JSON-friendly dictionary.

    Filter objects are duck-typed: every :data:`ALLOWED_FILTER_FIELDS`
    attribute that exists is considered.  Unknown attributes (for example a
    ClickHouse password) are never serialized.

    Args:
        filters: Any object with the allow-listed attributes, or a mapping.
        include_dataset: Store the ``dataset`` field.  It is omitted by default
            because it describes the import scope rather than a filter.

    Returns:
        A new dictionary with ISO 8601 dates and normalized strings/integers.

    Raises:
        PresetsValidationError: If a present value violates the contract.
    """
    if isinstance(filters, Mapping):
        raw: dict[str, object] = {
            field: filters[field]
            for field in ALLOWED_FILTER_FIELDS
            if field in filters
        }
    elif filters is None:
        raw = {}
    else:
        raw = {}
        for field in ALLOWED_FILTER_FIELDS:
            try:
                raw[field] = getattr(filters, field)
            except AttributeError:
                continue
    return _clean_filter_record(raw, include_dataset=include_dataset)


def dict_to_filter_kwargs(record: object) -> dict:
    """Convert a stored filters dictionary into ``Filters(**kwargs)`` input.

    Unknown keys are ignored; date strings (including date-only values) are
    parsed with :meth:`datetime.fromisoformat`.

    Args:
        record: Stored ``filters`` mapping.

    Returns:
        Keyword arguments suitable for the adapter's ``Filters`` dataclass.

    Raises:
        PresetsValidationError: If ``record`` is not a mapping or a value is
            invalid.
    """
    if not isinstance(record, Mapping):
        raise PresetsValidationError("filters must be a JSON object")
    cleaned = _clean_filter_record(record, include_dataset=True)
    kwargs: dict[str, Any] = {}
    for field, value in cleaned.items():
        if field in ("date_from", "date_to"):
            kwargs[field] = datetime.fromisoformat(value)
        elif field == "min_duration_ms":
            kwargs[field] = int(value)
        else:
            kwargs[field] = value
    return kwargs


def _normalize_name(name: object) -> str:
    """Validate and strip a preset name.

    Raises:
        PresetsValidationError: If the name is not a string, is blank, contains
            control characters or is longer than :data:`MAX_NAME_LENGTH`.
    """
    if not isinstance(name, str):
        raise PresetsValidationError("Preset name must be a string")
    if _has_control_characters(name):
        raise PresetsValidationError("Preset name must not contain control characters")
    text = name.strip()
    if not text:
        raise PresetsValidationError("Preset name must not be blank")
    if len(text) > MAX_NAME_LENGTH:
        raise PresetsValidationError(
            f"Preset name must not exceed {MAX_NAME_LENGTH} characters"
        )
    return text


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _normalize_timestamp(value: object, fallback: str | None = None) -> str:
    """Return a canonical ISO 8601 timestamp for a stored value."""
    if isinstance(value, str):
        text = value.strip()
        if text:
            try:
                return datetime.fromisoformat(text).isoformat()
            except ValueError:
                pass
    return fallback if fallback is not None else _now_iso()


def _normalize_preset_entry(entry: object) -> dict[str, Any]:
    """Validate one ``presets[]`` entry and return a normalized copy.

    Raises:
        PresetsValidationError: If the entry is not an object or any field is
            invalid.
    """
    if not isinstance(entry, dict):
        raise PresetsValidationError("preset entry must be a JSON object")

    name = _normalize_name(entry.get("name"))

    raw_filters = entry.get("filters", {})
    if raw_filters is None:
        raw_filters = {}
    if not isinstance(raw_filters, Mapping):
        raise PresetsValidationError("preset 'filters' must be a JSON object")
    filters = _clean_filter_record(raw_filters, include_dataset=True)

    created_at = _normalize_timestamp(entry.get("created_at"))
    updated_at = _normalize_timestamp(entry.get("updated_at"), fallback=created_at)
    return {
        "name": name,
        "created_at": created_at,
        "updated_at": updated_at,
        "filters": filters,
    }


class PresetStore:
    """Read and write the named filter presets stored in a JSON file.

    The store caches the last successfully loaded document.  :meth:`load`
    always re-reads the file; every other method loads lazily on first use.
    Returned preset dictionaries are deep copies, so callers cannot mutate the
    cache by accident.

    Args:
        path: Optional file path.  ``None`` uses ``ICS_PRESETS_PATH`` when set
            and :data:`DEFAULT_PRESETS_PATH` otherwise.
    """

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        if path is None:
            env_value = os.environ.get(PRESETS_ENV_VAR, "").strip()
            path = Path(env_value) if env_value else DEFAULT_PRESETS_PATH
        elif isinstance(path, str) and not path.strip():
            path = DEFAULT_PRESETS_PATH

        self._path = Path(path).expanduser()
        self._presets: list[dict[str, Any]] = []
        self._last_used: str | None = None
        self._last_error: str | None = None
        self._loaded = False
        self._invalid_file = False
        self._read_only = False

    @property
    def path(self) -> Path:
        """Location of the presets JSON document."""
        return self._path

    @property
    def last_error(self) -> str | None:
        """Human-readable reason why the last load skipped data, if any."""
        try:
            self._ensure_loaded()
        except PresetsValidationError:
            pass
        return self._last_error

    def load(self) -> list[dict]:
        """Re-read the document from disk and return copies of valid presets.

        Missing files yield an empty list.  Corrupt files, oversized files and
        documents with an unsupported schema are reported through
        :attr:`last_error` instead of raising.

        Raises:
            PresetsValidationError: If the file contains more than
                :data:`MAX_PRESETS` entries.
        """
        self._loaded = True
        self._last_error = None
        self._presets = []
        self._last_used = None
        self._invalid_file = False
        self._read_only = False

        try:
            if not self._path.exists():
                return []
        except OSError as exc:
            self._last_error = f"Cannot access presets file {self._path}: {exc}"
            return []

        if not self._path.is_file():
            self._last_error = f"Presets path is not a regular file: {self._path}"
            return []

        try:
            size = self._path.stat().st_size
        except OSError as exc:
            self._last_error = f"Cannot read presets file {self._path}: {exc}"
            return []

        if size > MAX_FILE_BYTES:
            self._invalid_file = True
            self._last_error = (
                f"Presets file is too large ({size} bytes > {MAX_FILE_BYTES} bytes) "
                "and was ignored."
            )
            return []

        try:
            raw_text = self._path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            self._last_error = f"Cannot read presets file {self._path}: {exc}"
            return []

        try:
            document = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            self._invalid_file = True
            self._last_error = f"Presets file is not valid JSON: {exc}."
            return []

        if not isinstance(document, dict):
            self._invalid_file = True
            self._last_error = "Presets file must contain a JSON object."
            return []

        version = document.get("schema_version")
        if not isinstance(version, int) or isinstance(version, bool):
            self._invalid_file = True
            self._last_error = "Presets file has no integer 'schema_version'."
            return []

        if version > SCHEMA_VERSION:
            self._read_only = True
            self._last_error = (
                "Presets file was created by a newer version "
                f"(schema_version {version}); write access is disabled until the "
                "presets are reset."
            )
            return []

        if version != SCHEMA_VERSION:
            self._invalid_file = True
            self._last_error = f"Unsupported presets schema_version: {version}."
            return []

        raw_presets = document.get("presets", [])
        if not isinstance(raw_presets, list):
            self._invalid_file = True
            self._last_error = "Presets file field 'presets' must be a list."
            return []

        if len(raw_presets) > MAX_PRESETS:
            self._invalid_file = True
            self._last_error = (
                f"Presets file contains {len(raw_presets)} presets, more than the "
                f"limit of {MAX_PRESETS}."
            )
            raise PresetsValidationError(self._last_error)

        presets: list[dict[str, Any]] = []
        seen: set[str] = set()
        problems: list[str] = []
        for index, entry in enumerate(raw_presets, start=1):
            try:
                preset = _normalize_preset_entry(entry)
            except PresetsValidationError as exc:
                problems.append(f"preset #{index}: {exc}")
                continue
            if preset["name"] in seen:
                problems.append(
                    f"preset #{index}: duplicate name {preset['name']!r}"
                )
                continue
            seen.add(preset["name"])
            presets.append(preset)

        last_used = document.get("last_used")
        self._presets = presets
        self._last_used = (
            last_used if isinstance(last_used, str) and last_used in seen else None
        )

        if problems:
            self._invalid_file = True
            summary = "; ".join(problems[:3])
            if len(problems) > 3:
                summary += f"; and {len(problems) - 3} more"
            self._last_error = (
                f"Skipped {len(problems)} invalid preset(s): {summary}."
            )

        return copy.deepcopy(self._presets)

    def names(self) -> list[str]:
        """Return preset names in document order."""
        self._ensure_loaded()
        return [preset["name"] for preset in self._presets]

    def get(self, name: object) -> dict | None:
        """Return a copy of the named preset, or ``None`` when absent."""
        self._ensure_loaded()
        try:
            normalized = _normalize_name(name)
        except PresetsValidationError:
            return None
        for preset in self._presets:
            if preset["name"] == normalized:
                return copy.deepcopy(preset)
        return None

    def save_preset(
        self,
        name: object,
        filters: object,
        *,
        include_dataset: bool = False,
    ) -> dict:
        """Create or update a named preset and write the document atomically.

        Saving an existing name updates the filters in place and preserves the
        original ``created_at`` value while refreshing ``updated_at``.

        Args:
            name: Preset name (stripped, 1..80 characters, no control chars).
            filters: Duck-typed filter object.
            include_dataset: Store the ``dataset`` field as part of the preset.

        Returns:
            A copy of the saved preset.

        Raises:
            PresetsValidationError: If the name, filters or the preset count are
                invalid, or the file is read-only because it was created by a
                newer version.
            PresetsStoreError: If the document cannot be written.
        """
        clean_name = _normalize_name(name)
        filter_dict = filters_to_dict(filters, include_dataset=include_dataset)

        self._ensure_loaded()
        self._ensure_writable()

        now = _now_iso()
        presets = copy.deepcopy(self._presets)
        saved: dict[str, Any] | None = None
        for preset in presets:
            if preset["name"] == clean_name:
                preset["filters"] = filter_dict
                preset["updated_at"] = now
                saved = preset
                break

        if saved is None:
            if len(presets) >= MAX_PRESETS:
                raise PresetsValidationError(
                    f"Cannot save more than {MAX_PRESETS} presets."
                )
            saved = {
                "name": clean_name,
                "created_at": now,
                "updated_at": now,
                "filters": filter_dict,
            }
            presets.append(saved)

        self._persist(presets, self._last_used)
        return copy.deepcopy(saved)

    def delete_preset(self, name: object) -> bool:
        """Delete a preset by name.

        Returns:
            ``True`` if the preset existed and was removed, ``False`` otherwise.

        Raises:
            PresetsValidationError: If the file is read-only.
            PresetsStoreError: If the document cannot be written.
        """
        try:
            clean_name = _normalize_name(name)
        except PresetsValidationError:
            return False

        self._ensure_loaded()
        self._ensure_writable()

        presets = copy.deepcopy(self._presets)
        remaining = [preset for preset in presets if preset["name"] != clean_name]
        if len(remaining) == len(presets):
            return False

        last_used = None if self._last_used == clean_name else self._last_used
        self._persist(remaining, last_used)
        return True

    def get_last_used(self) -> str | None:
        """Return the name of the last applied preset, if it still exists."""
        self._ensure_loaded()
        return self._last_used

    def set_last_used(self, name: str | None) -> None:
        """Remember the last applied preset.

        An unknown or invalid name clears the value instead of raising; this
        matches "presets are best effort" semantics for the GUI.

        Raises:
            PresetsValidationError: If the file is read-only.
            PresetsStoreError: If the document cannot be written.
        """
        self._ensure_loaded()
        self._ensure_writable()

        normalized: str | None = None
        if name is not None:
            try:
                candidate = _normalize_name(name)
            except PresetsValidationError:
                candidate = ""
            if candidate and any(
                preset["name"] == candidate for preset in self._presets
            ):
                normalized = candidate

        if normalized == self._last_used:
            return
        self._persist(copy.deepcopy(self._presets), normalized)

    def reset(self) -> None:
        """Archive the current file and write a valid empty document.

        This is the recovery path for corrupt and future-schema files, so it
        deliberately ignores the read-only flag.

        Raises:
            PresetsStoreError: If the archive or the new document cannot be
                written.
        """
        try:
            self.load()
        except PresetsValidationError:
            pass

        if self._path.is_file():
            self._archive_current()

        self._write_json(
            {
                "schema_version": SCHEMA_VERSION,
                "last_used": None,
                "presets": [],
            }
        )
        self._presets = []
        self._last_used = None
        self._last_error = None
        self._invalid_file = False
        self._read_only = False
        self._loaded = True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _ensure_loaded(self) -> None:
        """Load the document once, unless a previous call already did."""
        if not self._loaded:
            self.load()

    def _ensure_writable(self) -> None:
        """Reject writes when the file belongs to a newer schema version."""
        if self._read_only:
            raise PresetsValidationError(
                "Presets file was created by a newer version; writes are disabled "
                "until the presets are reset."
            )

    def _persist(
        self, presets: list[dict[str, Any]], last_used: str | None
    ) -> None:
        """Write a document and commit it to the in-memory cache."""
        if self._invalid_file and self._path.is_file():
            self._archive_current()

        self._write_json(
            {
                "schema_version": SCHEMA_VERSION,
                "last_used": last_used,
                "presets": presets,
            }
        )

        self._presets = copy.deepcopy(presets)
        self._last_used = last_used
        self._last_error = None
        self._invalid_file = False
        self._read_only = False
        self._loaded = True

    def _write_json(self, document: Mapping[str, object]) -> None:
        """Atomically replace the presets file with ``document``.

        A temporary file is created next to the target, flushed and ``fsync``-ed
        before :func:`os.replace`.  Any failure removes the temporary file and
        leaves the previous document untouched.

        Raises:
            PresetsStoreError: If the directory or the file cannot be written.
        """
        directory = self._path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PresetsStoreError(
                f"Cannot create presets directory {directory}: {exc}"
            ) from exc

        fd = -1
        temp_name = ""
        try:
            fd, temp_name = tempfile.mkstemp(
                prefix=".presets-", suffix=".tmp", dir=str(directory)
            )
            stream = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
            fd = -1
            with stream:
                json.dump(document, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            if os.name == "posix":
                os.chmod(temp_name, 0o600)
            os.replace(temp_name, self._path)
            temp_name = ""
        except Exception as exc:
            raise PresetsStoreError(
                f"Failed to write presets file {self._path}: {exc}"
            ) from exc
        finally:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            if temp_name:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass

    def _archive_current(self) -> Path | None:
        """Copy the current file to ``presets.corrupt-<timestamp>.json``.

        Returns:
            The archive path, or ``None`` when there is no regular file.

        Raises:
            PresetsStoreError: If the copy fails.
        """
        if not self._path.is_file():
            return None

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        base = f"{self._path.stem}.corrupt-{stamp}"
        archive = self._path.with_name(f"{base}.json")
        counter = 1
        while archive.exists():
            archive = self._path.with_name(f"{base}-{counter}.json")
            counter += 1

        try:
            shutil.copy2(self._path, archive)
        except OSError as exc:
            raise PresetsStoreError(
                f"Cannot archive presets file {self._path}: {exc}"
            ) from exc

        self._prune_archives()
        return archive

    def _prune_archives(self) -> None:
        """Keep at most :data:`MAX_ARCHIVES` corrupt-file archives."""
        pattern = f"{self._path.stem}.corrupt-*.json"
        archives = sorted(
            self._path.parent.glob(pattern), key=lambda item: item.name
        )
        for archive in archives[:-MAX_ARCHIVES]:
            try:
                archive.unlink()
            except OSError:
                pass
