"""Tests for :mod:`import_progress`.

These tests pin the public contract of the typed progress model: the exact
stage values, percentage derivation/clamping, the monotonic guarantees of
:class:`import_progress.ProgressReporter`, throttling on an injected fake
clock, resilience against a failing GUI callback and thread safety.

No test sleeps: all time comes from :class:`FakeClock` and concurrency is
arranged with :class:`threading.Barrier` plus ``join``.
"""

from __future__ import annotations

import threading
import unittest

from import_progress import ImportProgress, ImportStage, ProgressReporter


class FakeClock:
    """Deterministic monotonic clock advanced explicitly by a test."""

    def __init__(self, step: float = 0.01, start: float = 0.0):
        """Store the step between ticks and the initial value.

        Args:
            step: Seconds added per :meth:`advance` call.
            start: Initial reading of the clock.
        """
        self._step = float(step)
        self._start = float(start)
        self._ticks = 0

    def __call__(self) -> float:
        """Return the current fake time in seconds."""
        return self._start + self._ticks * self._step

    def advance(self, ticks: int = 1) -> None:
        """Move the clock forward by ``ticks`` steps."""
        self._ticks += int(ticks)


class TestImportStage(unittest.TestCase):
    """The enum is stable: log parsers and the GUI depend on its values."""

    def test_exact_values_and_order(self):
        """Stages have exactly the documented values, in pipeline order."""
        self.assertEqual(
            [stage.value for stage in ImportStage],
            [
                "preparing",
                "schema",
                "scanning",
                "processing",
                "inserting",
                "counting",
                "done",
            ],
        )

    def test_enum_members_are_strings(self):
        """The ``str`` mixin keeps the enum usable where a string is expected."""
        self.assertIsInstance(ImportStage.PROCESSING, str)
        self.assertEqual(ImportStage.PROCESSING, "processing")


class TestImportProgress(unittest.TestCase):
    """Normalisation rules of the immutable snapshot."""

    def test_percent_is_computed_from_counts(self):
        """10 of 40 units is 25 percent."""
        progress = ImportProgress(ImportStage.PROCESSING, current=10, total=40)
        self.assertEqual(progress.percent, 25.0)
        self.assertFalse(progress.is_indeterminate)

    def test_percent_is_clamped_low_and_high(self):
        """Explicit percentages outside 0..100 are clamped."""
        self.assertEqual(
            ImportProgress(ImportStage.PROCESSING, percent=-5).percent, 0.0
        )
        self.assertEqual(
            ImportProgress(ImportStage.PROCESSING, percent=150).percent, 100.0
        )

    def test_percent_is_rounded_to_one_decimal(self):
        """A computed percentage is rounded to 0.1."""
        self.assertEqual(
            ImportProgress(ImportStage.PROCESSING, current=1, total=3).percent,
            33.3,
        )

    def test_missing_or_non_positive_total_is_unknown(self):
        """``total`` of ``None``, zero or negative means indeterminate."""
        for total in (None, 0, -10):
            with self.subTest(total=total):
                progress = ImportProgress(
                    ImportStage.SCANNING, current=5, total=total
                )
                self.assertIsNone(progress.total)
                self.assertIsNone(progress.percent)
                self.assertTrue(progress.is_indeterminate)

    def test_stage_accepts_a_string(self):
        """A parsed string is coerced to the enum member."""
        progress = ImportProgress(stage="scanning")
        self.assertIs(progress.stage, ImportStage.SCANNING)
        self.assertEqual(progress.stage, "scanning")

    def test_unknown_stage_falls_back_to_preparing(self):
        """Garbage from a parser must not crash the UI path."""
        progress = ImportProgress(stage="not-a-stage")
        self.assertIs(progress.stage, ImportStage.PREPARING)

    def test_negative_current_is_clamped_to_zero(self):
        """A negative unit count becomes zero instead of a negative bar."""
        progress = ImportProgress(
            ImportStage.PROCESSING, current=-5, total=40
        )
        self.assertEqual(progress.current, 0)
        self.assertEqual(progress.percent, 0.0)

    def test_snapshot_is_immutable(self):
        """The frozen dataclass cannot be mutated after construction."""
        progress = ImportProgress(ImportStage.DONE)
        with self.assertRaises(AttributeError):
            progress.stage = ImportStage.PREPARING


class TestProgressReporterMonotonicity(unittest.TestCase):
    """Stages never regress and known percentages never decrease."""

    def test_percent_never_decreases(self):
        """50 -> 40 stays at 50; a later 55 is allowed to grow."""
        events = []
        reporter = ProgressReporter(
            events.append, min_interval=0.0, clock=FakeClock()
        )
        reporter.emit(ImportProgress(ImportStage.PROCESSING, percent=50))
        reporter.emit(ImportProgress(ImportStage.PROCESSING, percent=40))
        self.assertEqual(events[-1].percent, 50.0)
        reporter.emit(ImportProgress(ImportStage.PROCESSING, percent=55))
        self.assertEqual(events[-1].percent, 55.0)

    def test_indeterminate_does_not_reset_known_percent(self):
        """``percent=None`` stays unknown but keeps the running maximum."""
        events = []
        reporter = ProgressReporter(
            events.append, min_interval=0.0, clock=FakeClock()
        )
        reporter.emit(ImportProgress(ImportStage.PROCESSING, percent=50))
        reporter.emit(ImportProgress(ImportStage.PROCESSING))
        self.assertTrue(events[-1].is_indeterminate)
        reporter.emit(ImportProgress(ImportStage.PROCESSING, percent=40))
        self.assertEqual(events[-1].percent, 50.0)

    def test_stage_does_not_regress_and_is_rewritten(self):
        """A late ``scanning`` event keeps its data but shows ``processing``."""
        events = []
        reporter = ProgressReporter(
            events.append, min_interval=0.0, clock=FakeClock()
        )
        reporter.emit(ImportProgress(ImportStage.PROCESSING, message="processing"))
        reporter.emit(
            ImportProgress(
                ImportStage.SCANNING, message="scanning", current=1, total=10
            )
        )
        self.assertEqual(len(events), 2)
        self.assertIs(events[-1].stage, ImportStage.PROCESSING)
        self.assertEqual(events[-1].message, "scanning")
        self.assertEqual(events[-1].percent, 10.0)

    def test_preparing_and_schema_share_the_lowest_rank(self):
        """Both pre-work stages are order-insensitive and do not regress."""
        events = []
        reporter = ProgressReporter(
            events.append, min_interval=0.0, clock=FakeClock()
        )
        reporter.emit(ImportProgress(ImportStage.SCHEMA))
        reporter.emit(ImportProgress(ImportStage.PREPARING))
        self.assertIs(events[-1].stage, ImportStage.PREPARING)


class TestProgressReporterThrottling(unittest.TestCase):
    """Time-based throttling and the events that always bypass it."""

    def test_rapid_updates_are_throttled(self):
        """20 events over 0.2 s produce at most 3 deliveries."""
        clock = FakeClock(step=0.01)
        events = []
        reporter = ProgressReporter(events.append, min_interval=0.1, clock=clock)
        for index in range(20):
            reporter.emit(
                ImportProgress(ImportStage.PROCESSING, current=index, total=100)
            )
            clock.advance()
        self.assertGreaterEqual(len(events), 2)
        self.assertLessEqual(len(events), 3)
        self.assertLess(len(events), 20)

    def test_force_bypasses_the_throttle(self):
        """``force=True`` delivers even inside the minimum interval."""
        clock = FakeClock(step=1.0)
        events = []
        reporter = ProgressReporter(events.append, min_interval=10.0, clock=clock)
        self.assertTrue(reporter.emit(ImportProgress(ImportStage.PROCESSING)))
        self.assertFalse(reporter.emit(ImportProgress(ImportStage.PROCESSING)))
        self.assertTrue(
            reporter.emit(ImportProgress(ImportStage.PROCESSING), force=True)
        )
        self.assertEqual(len(events), 2)

    def test_stage_change_is_forced(self):
        """A new stage is delivered without waiting for the interval."""
        clock = FakeClock(step=0.0)
        events = []
        reporter = ProgressReporter(events.append, min_interval=10.0, clock=clock)
        self.assertTrue(reporter.emit(ImportProgress(ImportStage.PREPARING)))
        self.assertTrue(reporter.emit(ImportProgress(ImportStage.PROCESSING)))
        self.assertEqual([event.stage for event in events], [
            ImportStage.PREPARING,
            ImportStage.PROCESSING,
        ])

    def test_done_is_forced_and_always_100_percent(self):
        """``DONE`` is delivered immediately and overrides the given percent."""
        clock = FakeClock(step=0.0)
        events = []
        reporter = ProgressReporter(events.append, min_interval=10.0, clock=clock)
        reporter.emit(ImportProgress(ImportStage.PROCESSING, percent=42))
        self.assertTrue(
            reporter.emit(ImportProgress(ImportStage.DONE, percent=42))
        )
        self.assertIs(events[-1].stage, ImportStage.DONE)
        self.assertEqual(events[-1].percent, 100.0)
        self.assertTrue(reporter.emit(ImportProgress(ImportStage.DONE)))
        self.assertEqual(len(events), 3)


class TestProgressReporterSafety(unittest.TestCase):
    """Failures, closing and the locking discipline of the reporter."""

    def test_callback_exception_is_logged_and_swallowed(self):
        """A broken GUI callback never escapes into the import thread."""
        invocations = []

        def failing_callback(progress: ImportProgress) -> None:
            invocations.append(progress)
            raise RuntimeError("boom")

        reporter = ProgressReporter(
            failing_callback, min_interval=0.0, clock=FakeClock()
        )
        with self.assertLogs("import_progress", level="ERROR") as captured:
            delivered = reporter.emit(ImportProgress(ImportStage.PREPARING))
        self.assertTrue(delivered)
        self.assertEqual(len(invocations), 1)
        self.assertIn("Import progress callback failed", captured.output[0])

    def test_close_blocks_further_events(self):
        """After ``close`` every emit is ignored, even a forced ``DONE``."""
        events = []
        reporter = ProgressReporter(
            events.append, min_interval=0.0, clock=FakeClock()
        )
        self.assertTrue(reporter.emit(ImportProgress(ImportStage.PROCESSING)))
        reporter.close()
        self.assertFalse(
            reporter.emit(ImportProgress(ImportStage.DONE), force=True)
        )
        self.assertFalse(reporter.emit(ImportProgress(ImportStage.COUNTING)))
        self.assertEqual(len(events), 1)
        reporter.close()

    def test_callback_is_invoked_outside_the_lock(self):
        """A GUI callback can re-enter the reporter's owner without deadlock."""
        seen_locked = []
        reporter = None

        def callback(progress: ImportProgress) -> None:
            seen_locked.append(reporter._lock.locked())

        reporter = ProgressReporter(
            callback, min_interval=0.0, clock=FakeClock()
        )
        reporter.emit(ImportProgress(ImportStage.PROCESSING))
        self.assertEqual(seen_locked, [False])

    def test_concurrent_emits_do_not_lose_or_corrupt_state(self):
        """Many threads emitting at once keep every event and stay in range."""
        workers = 8
        per_worker = 25
        total = workers * per_worker
        events = []
        events_lock = threading.Lock()
        errors = []

        def callback(progress: ImportProgress) -> None:
            with events_lock:
                events.append(progress)

        reporter = ProgressReporter(
            callback, min_interval=0.0, clock=lambda: 0.0
        )
        barrier = threading.Barrier(workers)

        def worker(base: int) -> None:
            try:
                barrier.wait(timeout=5)
                for offset in range(per_worker):
                    reporter.emit(
                        ImportProgress(
                            ImportStage.PROCESSING,
                            current=base + offset,
                            total=total,
                        )
                    )
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        threads = [
            threading.Thread(target=worker, args=(index * per_worker,))
            for index in range(workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(events), total)
        self.assertTrue(all(event.stage is ImportStage.PROCESSING for event in events))
        self.assertTrue(
            all(
                event.percent is not None and 0.0 <= event.percent <= 100.0
                for event in events
            )
        )
        self.assertEqual(max(event.percent for event in events), 99.5)


if __name__ == "__main__":
    unittest.main()
