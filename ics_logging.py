"""Process-wide logging configuration for the 1C Log Analyzer.

DEBT-003: diagnostics from every module go through the standard
:mod:`logging` package instead of ``print`` or silently swallowed exceptions.
:func:`configure_logging` is the only place that installs a handler, so the GUI
entry point, tests and any future CLI share one format and one level switch.

The log level is read from the ``ICS_LOG_LEVEL`` environment variable (a name
such as ``DEBUG``/``INFO`` or a numeric level).  When no stream is available —
a windowed PyInstaller build has ``sys.stderr`` set to ``None`` — the function
installs no handler and leaves the application silent instead of crashing.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, Optional, Union

#: Environment variable that overrides the default log level.
ENV_LOG_LEVEL = "ICS_LOG_LEVEL"

#: Level used when neither an argument nor the environment variable is set.
DEFAULT_LEVEL = logging.WARNING

#: Format shared by the GUI and command-line runs.
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

#: Marker attribute used to recognise handlers installed by this module.
_HANDLER_MARKER = "_ics_logging_handler"

_configured = False


def resolve_level(value: Union[str, int, None] = None) -> int:
    """Resolve a log level from an argument, the environment or the default.

    Args:
        value: Optional level name (``"DEBUG"``) or numeric level.  When
            omitted, ``ICS_LOG_LEVEL`` is consulted.

    Returns:
        A :mod:`logging` level.  Unknown names fall back to
        :data:`DEFAULT_LEVEL` instead of raising, so a typo in an environment
        variable never prevents the application from starting.
    """
    if value is None:
        value = os.environ.get(ENV_LOG_LEVEL)
    if value is None or value == "":
        return DEFAULT_LEVEL
    if isinstance(value, int):
        return value

    text = str(value).strip().upper()
    if text.isdigit():
        return int(text)
    level = getattr(logging, text, None)
    return level if isinstance(level, int) else DEFAULT_LEVEL


def configure_logging(
    level: Union[str, int, None] = None,
    stream: Optional[Any] = None,
    force: bool = False,
) -> logging.Logger:
    """Install the application's single stream handler exactly once.

    Args:
        level: Optional level override.  See :func:`resolve_level`.
        stream: Destination for log records.  Defaults to ``sys.stderr``; when
            that is ``None`` (windowed build) no handler is installed.
        force: Rebuild the handler and level even if logging was already
            configured.  Used by tests; the application never needs it.

    Returns:
        The ``ics_log_analyzer`` application logger.  Module loggers remain
        children of the root logger, so they propagate to the same handler.
    """
    global _configured

    root = logging.getLogger()
    if _configured and not force:
        return logging.getLogger("ics_log_analyzer")

    if force:
        for handler in list(root.handlers):
            if getattr(handler, _HANDLER_MARKER, False):
                root.removeHandler(handler)

    destination = stream if stream is not None else sys.stderr
    if destination is not None:
        handler = logging.StreamHandler(destination)
        setattr(handler, _HANDLER_MARKER, True)
        handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
        root.addHandler(handler)

    root.setLevel(resolve_level(level))
    _configured = True
    return logging.getLogger("ics_log_analyzer")


def is_configured() -> bool:
    """Return whether :func:`configure_logging` has run in this process."""
    return _configured
