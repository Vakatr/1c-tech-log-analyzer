"""Event reference registry loaded from ``docs/events.json``.

The registry is optional: a missing or malformed file must never stop the
application.  Failures are recorded in :func:`registry_error` so the GUI can
show a warning instead of silently working with an empty reference.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

if getattr(sys, "frozen", False):
    _ROOT = Path(sys._MEIPASS)
else:
    _ROOT = Path(__file__).resolve().parent

#: Path of the machine-readable event reference.
_EVENTS_PATH = _ROOT / "docs" / "events.json"

_registry: Optional[Dict[str, Any]] = None
_load_error: Optional[str] = None


def events_path() -> Path:
    """Return the location of the event reference file."""
    return _EVENTS_PATH


def load_registry(force_reload: bool = False) -> Dict[str, Any]:
    """Load and cache ``docs/events.json``.

    Args:
        force_reload: Re-read the file even if it has already been loaded.

    Returns:
        Registry dictionary.  On failure an empty registry
        (``{"events": []}``) is returned and the reason is available through
        :func:`registry_error`.
    """
    global _registry, _load_error
    if _registry is not None and not force_reload:
        return _registry

    try:
        with open(_EVENTS_PATH, "r", encoding="utf-8") as stream:
            data = json.load(stream)
    except Exception as exc:  # noqa: BLE001 - the registry is optional
        _load_error = f"{_EVENTS_PATH}: {exc}"
        logger.warning("Cannot load event registry %s: %s", _EVENTS_PATH, exc)
        _registry = {"events": []}
        return _registry

    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        _load_error = (
            f"{_EVENTS_PATH}: expected an object with an 'events' list"
        )
        logger.warning("Malformed event registry %s: %s", _EVENTS_PATH, _load_error)
        _registry = {"events": []}
        return _registry

    _load_error = None
    _registry = data
    return _registry


def registry_error() -> Optional[str]:
    """Return the last registry load error, or ``None`` if loading succeeded.

    The value is only meaningful after :func:`load_registry` has been called.
    """
    return _load_error


def all_event_codes() -> List[str]:
    """Return every event code from the registry, in file order."""
    return [str(entry.get("code", "")) for entry in load_registry().get("events", [])]


def get_event(code: str) -> Optional[Dict[str, Any]]:
    """Return the registry entry for ``code``, or ``None`` when absent.

    Args:
        code: 1C event code (for example ``"EXCP"``).
    """
    for entry in load_registry().get("events", []):
        if entry.get("code") == code:
            return entry
    return None


def known_field_names() -> Set[str]:
    """Return every field name mentioned in the registry.

    Used by the parser to extend its fixed field map with registry-only names.
    """
    names: Set[str] = set()
    for entry in load_registry().get("events", []):
        for field in entry.get("fields", []):
            name = str(field.get("name", "")).strip()
            if name:
                names.add(name)
    return names
