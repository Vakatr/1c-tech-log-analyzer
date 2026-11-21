"""Shared log-analysis constants.

Keeping the error-event definition in one place prevents the in-memory and
ClickHouse paths from disagreeing about what counts as an error.
"""

from __future__ import annotations

from typing import Any, Mapping, Tuple

#: 1C event codes that represent errors in both the parser and ClickHouse.
ERROR_EVENT_NAMES: Tuple[str, ...] = ("EXCP", "EXCPCNTX")


def is_error_event_name(name: Any) -> bool:
    """Return whether ``name`` is one of :data:`ERROR_EVENT_NAMES`."""
    return str(name or "").strip() in ERROR_EVENT_NAMES


def is_error_event(event: Mapping[str, Any]) -> bool:
    """Return whether an event dictionary describes an error."""
    return is_error_event_name(event.get("name", ""))
