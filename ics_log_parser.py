"""1C technological log parser.

Supports the text format produced by 1C and the JSON-lines variant.  Parsing
is deliberately tolerant: timestamps may omit milliseconds, fields may use a
comma without a following space, and quoted values may contain commas.
"""

from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ics_events_registry import known_field_names

logger = logging.getLogger(__name__)

#: Timestamp at the start of a text event.  Milliseconds are optional and may
#: use either ``.`` or ``,`` as the decimal separator.
TS_RE = re.compile(
    r"^\s*(\d{1,2}\.\d{1,2}\.\d{4}\s+\d{1,2}:\d{2}:\d{2}(?:[.,]\d{1,9})?)\s*$"
)

#: One ``key=value`` line.  Keys may contain ``:`` and ``.`` (``p:processName``).
FIELD_LINE_RE = re.compile(r"^(\s*)([A-Za-z_][\w:.]*)\s*=(.*)$")

#: Fields with a fixed normalized name; registry names are added below.
FIELD_MAP: Dict[str, str] = {
    "N": "name",
    "p:processName": "process",
    "t:process": "thread",
    "Usr": "user",
    "AppID": "app_id",
    "Event": "event",
    "Descr": "descr",
    "Duration": "duration",
}

# Extend FIELD_MAP with extra fields from events.json
_EXTRA_FIELDS = known_field_names() - set(FIELD_MAP.keys())
for _name in _EXTRA_FIELDS:
    FIELD_MAP[_name] = _name.lower().replace(":", "_").replace(" ", "_")

#: Default upper bound for concurrent file parsing.
DEFAULT_MAX_WORKERS = min(32, (os.cpu_count() or 1) + 4)


def detect_format(path: str) -> str:
    """Detect whether a log file is text or JSON-lines.

    Args:
        path: Path to the log file.

    Returns:
        ``"text"`` or ``"json"``.  Unreadable files are treated as text.
    """
    try:
        with open(path, "r", encoding="utf-8-sig") as stream:
            first_line = stream.readline().strip()
            if first_line.startswith("{"):
                return "json"
            return "text"
    except OSError:
        return "text"


def _strip_quotes(value: str) -> str:
    """Remove surrounding JSON-style quotes and unescape the content."""
    text = value.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return text[1:-1].replace('\\"', '"')
        return decoded if isinstance(decoded, str) else text[1:-1]
    return text


def _split_field_parts(line: str) -> List[str]:
    """Split a field line on commas that are outside quoted values."""
    parts: List[str] = []
    buffer: List[str] = []
    in_quotes = False
    escaped = False

    for char in line:
        if escaped:
            buffer.append(char)
            escaped = False
            continue
        if char == "\\" and in_quotes:
            buffer.append(char)
            escaped = True
            continue
        if char == '"':
            in_quotes = not in_quotes
            buffer.append(char)
            continue
        if char == "," and not in_quotes:
            parts.append("".join(buffer))
            buffer = []
            continue
        buffer.append(char)

    parts.append("".join(buffer))
    return parts


def _normalize_timestamp(value: str) -> str:
    """Use a dot as the millisecond separator for consistent downstream use."""
    return value.strip().replace(",", ".")


def _parse_duration(value: str) -> int:
    """Parse a duration in microseconds, tolerating integral floats."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _parse_fields(line: str, event: Dict[str, Any]) -> None:
    """Parse comma-separated ``key=value`` pairs into ``event``."""
    for part in _split_field_parts(line.strip()):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        target = FIELD_MAP.get(key)
        if target is None:
            continue
        if target == "duration":
            event["duration"] = _parse_duration(value)
        else:
            event[target] = _strip_quotes(value)


def _normalize_json(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize one JSON-lines record to the parser's field names."""
    event: Dict[str, Any] = {
        "timestamp": _normalize_timestamp(str(raw.get("timestamp", "") or "")),
        "name": raw.get("N", "") or "",
        "process": raw.get("p:processName", "") or "",
        "thread": raw.get("t:process", "") or "",
        "user": raw.get("Usr", "") or "",
        "app_id": raw.get("AppID", "") or "",
        "event": raw.get("Event", "") or "",
        "descr": raw.get("Descr", "") or "",
        "duration": _parse_duration(str(raw.get("Duration", 0))),
    }
    # Preserve registry-only fields instead of dropping them.
    for key, value in raw.items():
        target = FIELD_MAP.get(key)
        if target and target not in event and target != "duration":
            event[target] = value if value is not None else ""
    return event


def _empty_event(timestamp: str) -> Dict[str, Any]:
    """Return the normalized skeleton for one text event."""
    return {
        "timestamp": timestamp,
        "name": "",
        "process": "",
        "thread": "",
        "user": "",
        "app_id": "",
        "event": "",
        "descr": "",
        "duration": 0,
    }


def parse_event_block(lines: Iterable[str]) -> Dict[str, Any]:
    """Parse a single event block from lines.

    Args:
        lines: Lines belonging to one event.

    Returns:
        Dictionary with the timestamp and raw field names.
    """
    timestamp = ""
    fields: Dict[str, Any] = {}

    for line in lines:
        text = line.strip()
        match = TS_RE.match(text)
        if match:
            timestamp = _normalize_timestamp(match.group(1))
            continue

        field_match = FIELD_LINE_RE.match(line)
        if field_match:
            key = field_match.group(2)
            value = field_match.group(3).strip()
            fields[key] = _strip_quotes(value)

    return {"timestamp": timestamp, **fields}


def parse_log_file(path: str) -> List[Dict[str, Any]]:
    """Parse a single log file.

    Args:
        path: Path to the log file.

    Returns:
        List of event dictionaries with normalized keys.
    """
    events: List[Dict[str, Any]] = []
    format_type = detect_format(path)

    try:
        with open(path, "r", encoding="utf-8-sig") as stream:
            lines = stream.readlines()
    except OSError as exc:
        logger.warning("Cannot read log file %s: %s", path, exc)
        return events

    if not lines:
        return events

    if format_type == "json":
        for line in lines:
            text = line.strip()
            if not text or text.startswith("//"):
                continue
            try:
                raw = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, dict):
                events.append(_normalize_json(raw))
        return events

    current: Optional[Dict[str, Any]] = None
    for line in lines:
        text = line.strip()
        if not text:
            continue
        match = TS_RE.match(text)
        if match:
            if current is not None:
                events.append(current)
            current = _empty_event(_normalize_timestamp(match.group(1)))
            continue
        if current is None or text.startswith("//"):
            continue
        _parse_fields(text, current)

    if current is not None:
        events.append(current)
    return events


def _parse_file_safely(path: str) -> List[Dict[str, Any]]:
    """Parse one file, turning unexpected failures into an empty result."""
    try:
        return parse_log_file(path)
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
        logger.exception("Unexpected failure while parsing %s: %s", path, exc)
        return []


def parse_log_directory(
    path: str,
    max_workers: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Parse all ``.log`` files in a directory.

    Files are parsed concurrently; results are returned in filename order so
    the output stays deterministic regardless of worker completion order.

    Nested directories are scanned only when the top level has no logs.

    Args:
        path: Path to the directory containing log files.
        max_workers: Worker count.  Defaults to :data:`DEFAULT_MAX_WORKERS`.

    Returns:
        List of event dictionaries with normalized keys.
    """
    dir_path = Path(path)
    if not dir_path.exists():
        logger.warning("Log directory does not exist: %s", path)
        return []

    log_files = sorted(dir_path.glob("*.log"))
    if not log_files:
        log_files = sorted(dir_path.rglob("*.log"))
    if not log_files:
        return []

    paths = [str(log_file) for log_file in log_files]
    if len(paths) == 1:
        return _parse_file_safely(paths[0])

    workers = max_workers or DEFAULT_MAX_WORKERS
    workers = max(1, min(workers, len(paths)))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="log-parse") as executor:
        file_events = list(executor.map(_parse_file_safely, paths))

    events: List[Dict[str, Any]] = []
    for chunk in file_events:
        events.extend(chunk)
    return events
