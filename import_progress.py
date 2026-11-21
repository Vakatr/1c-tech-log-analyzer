"""Typed import progress for the 1C Log Analyzer.

The LogT import runs in background pump threads while the Tk GUI stays
responsive.  This module is the single, UI-free definition of *what* progress
looks like, so the adapter, the parser of LogT output and the GUI can share one
vocabulary without importing Tkinter, ClickHouse or each other.

Design notes
------------
* **One model.** :class:`ImportProgress` is an immutable snapshot.  It is safe
  to hand it to another thread (for example through a queue) without copying.
* **Unknown is a first-class value.** ``percent=None`` means "progress is
  unknown", not "zero".  The GUI must render an indeterminate indicator in that
  case.  The same applies to :attr:`ImportProgress.current` and
  :attr:`ImportProgress.total`.
* **Monotonic by contract.** :class:`ProgressReporter` never lets a coarse
  source make the bar jump backwards: known percentages are clamped to a
  running maximum and stages never regress (see :meth:`ProgressReporter.emit`).
* **Failures do not break the import.** An exception raised by the GUI callback
  is caught and logged; the pump thread keeps running.
* **No UI calls from this module.** The callback is invoked on the thread that
  called :meth:`ProgressReporter.emit`; marshalling to the Tk thread is the
  GUI's job (P1-04).

This module has no Tkinter or ClickHouse imports so it can be exercised
headless.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable

logger = logging.getLogger(__name__)

__all__ = ["ImportStage", "ImportProgress", "ProgressReporter"]


class ImportStage(str, Enum):
    """Ordered phases of a LogT import.

    The ``str`` mixin keeps the enum JSON-friendly and lets callers write
    ``"processing"`` in logs and tests without importing the enum.
    """

    PREPARING = "preparing"
    SCHEMA = "schema"
    SCANNING = "scanning"
    PROCESSING = "processing"
    INSERTING = "inserting"
    COUNTING = "counting"
    DONE = "done"


#: Monotonic rank of each stage.  ``preparing`` and ``schema`` share rank 0 on
#: purpose: they are both "before the real work" and may be reported in either
#: order by different code paths.
_STAGE_RANK = {
    ImportStage.PREPARING: 0,
    ImportStage.SCHEMA: 0,
    ImportStage.SCANNING: 1,
    ImportStage.PROCESSING: 2,
    ImportStage.INSERTING: 3,
    ImportStage.COUNTING: 4,
    ImportStage.DONE: 5,
}


def _coerce_count(value: int | None) -> int | None:
    """Return ``value`` as a non-negative ``int`` or ``None``.

    ``None`` and non-numeric values are returned as ``None``; negative counts
    are clamped to zero because a negative amount of work is meaningless for a
    progress bar.  The function is deliberately lenient: bad data from a log
    line must never crash the import.
    """
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        logger.warning("Ignoring non-numeric progress count %r", value)
        return None
    return number if number >= 0 else 0


@dataclass(frozen=True, slots=True)
class ImportProgress:
    """An immutable snapshot of one import step.

    Attributes:
        stage: Current phase.  A plain string is coerced in ``__post_init__``
            so call sites that build the value from parsed text cannot fail.
        message: Short human-readable status line.
        current: Completed units of the current step, or ``None`` if unknown.
        total: Total units of the current step.  ``None`` or a non-positive
            value means "unknown".
        percent: Overall completion in ``0..100`` rounded to one decimal, or
            ``None`` when it cannot be determined.  ``None`` is a valid value:
            the GUI shows an indeterminate indicator.
        detail: Optional extra context (file name, current hour, ...).

    Raises:
        Nothing for malformed input: unknown stages fall back to
        :attr:`ImportStage.PREPARING`, invalid counts become ``None`` and an
        invalid percentage becomes ``None``.  This keeps the UI path alive.
    """

    stage: ImportStage
    message: str = ""
    current: int | None = None
    total: int | None = None
    percent: float | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        """Normalise the raw values (frozen dataclass uses ``object.__setattr__``)."""
        stage = self.stage
        if not isinstance(stage, ImportStage):
            try:
                stage = ImportStage(stage)
            except (TypeError, ValueError):
                logger.warning(
                    "Unknown import stage %r; falling back to %r",
                    stage,
                    ImportStage.PREPARING.value,
                )
                stage = ImportStage.PREPARING
            object.__setattr__(self, "stage", stage)

        current = _coerce_count(self.current)
        object.__setattr__(self, "current", current)

        total = _coerce_count(self.total)
        if total is not None and total <= 0:
            total = None
        object.__setattr__(self, "total", total)

        percent = self.percent
        if percent is None:
            if current is not None and total is not None:
                percent = 100.0 * current / total
        else:
            try:
                percent = float(percent)
            except (TypeError, ValueError):
                logger.warning("Ignoring non-numeric progress percent %r", percent)
                percent = None
            else:
                if not math.isfinite(percent):
                    logger.warning("Ignoring non-finite progress percent %r", percent)
                    percent = None

        if percent is not None:
            percent = round(min(100.0, max(0.0, percent)), 1)
        object.__setattr__(self, "percent", percent)

    @property
    def is_indeterminate(self) -> bool:
        """Return ``True`` when the completion percentage is unknown."""
        return self.percent is None


class ProgressReporter:
    """Thread-safe, throttled bridge between an importer and the GUI.

    The reporter owns three pieces of state: the highest stage rank seen, the
    highest known percentage and the time of the last delivered event.  Every
    :meth:`emit` call updates that state under :class:`threading.Lock`; the
    user callback is then invoked *outside* the lock so a slow or blocking GUI
    handler cannot stall the other pump thread.

    Contract:
        * The first event, any stage change and every :attr:`ImportStage.DONE`
          event are delivered immediately, regardless of throttling.
        * Other events are delivered at most once per ``min_interval`` seconds.
        * A stage that would go backwards is **rewritten to the current stage**
          instead of being dropped: the counts and message still reach the GUI,
          but the visible phase never regresses.  This is safer than dropping a
          late ``scanning`` line that still carries useful file counts.
        * A known percentage never decreases (a running maximum is applied).
          An event with ``percent=None`` stays indeterminate and does not reset
          the known maximum.
        * ``DONE`` always carries ``percent=100.0``.
        * An exception from the callback is logged and swallowed.
        * After :meth:`close` every further event is ignored.
    """

    def __init__(
        self,
        callback: Callable[[ImportProgress], None],
        *,
        min_interval: float = 0.1,
        clock: Callable[[], float] = time.monotonic,
    ):
        """Create a reporter.

        Args:
            callback: Receives every delivered :class:`ImportProgress`.  It is
                called on the emitting thread; the GUI must marshal it to the
                Tk thread itself.
            min_interval: Minimum number of seconds between two non-forced
                events.  ``0`` disables throttling.
            clock: Monotonic time source; injectable for deterministic tests.
        """
        if not callable(callback):
            raise TypeError("callback must be callable")
        self._callback = callback
        self._min_interval = max(0.0, float(min_interval))
        self._clock = clock
        self._lock = threading.Lock()
        self._closed = False
        self._last_stage: ImportStage | None = None
        self._last_rank = -1
        self._last_percent: float | None = None
        self._last_emit_at: float | None = None

    def emit(self, progress: ImportProgress, *, force: bool = False) -> bool:
        """Deliver one snapshot, respecting monotonicity and throttling.

        Args:
            progress: Snapshot to deliver.
            force: Skip the time-based throttle.  Stage changes and ``DONE``
                are forced automatically.

        Returns:
            ``True`` if the callback was invoked (even if it raised), ``False``
            if the event was throttled, rewritten into a duplicate of the
            current state, or the reporter is closed.
        """
        with self._lock:
            if self._closed:
                return False

            stage = progress.stage
            rank = _STAGE_RANK.get(stage)
            if rank is None:  # Defensive: ImportProgress guarantees membership.
                logger.warning("Dropping progress event with unknown stage %r", stage)
                return False

            if rank < self._last_rank and self._last_stage is not None:
                stage = self._last_stage
                rank = self._last_rank

            percent = progress.percent
            if stage is ImportStage.DONE:
                percent = 100.0
            elif percent is not None and self._last_percent is not None:
                percent = max(percent, self._last_percent)

            stage_changed = stage is not self._last_stage
            forced = force or stage_changed or stage is ImportStage.DONE
            now = self._clock()
            if not forced:
                if (
                    self._last_emit_at is not None
                    and now - self._last_emit_at < self._min_interval
                ):
                    return False

            if percent is not None:
                self._last_percent = percent
            self._last_stage = stage
            self._last_rank = rank
            self._last_emit_at = now

            event = progress
            if stage is not progress.stage or percent != progress.percent:
                event = ImportProgress(
                    stage=stage,
                    message=progress.message,
                    current=progress.current,
                    total=progress.total,
                    percent=percent,
                    detail=progress.detail,
                )

        try:
            self._callback(event)
        except Exception:  # noqa: BLE001 - a GUI failure must not stop the import.
            logger.exception("Import progress callback failed")
        return True

    def close(self) -> None:
        """Stop delivering events.  Idempotent; emits no final event."""
        with self._lock:
            self._closed = True
