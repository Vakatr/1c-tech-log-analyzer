"""Integration tests for typed import progress (plan item P1-03).

The import runner streams LogT's ``stdout`` and ``stderr`` through
:class:`logt_progress.LogTProgressParser` and reports throttled
:class:`import_progress.ImportProgress` snapshots to ``on_progress``, while the
legacy string callback keeps working unchanged.

These tests are fully offline: ClickHouse is replaced by a ``count_tables``
mock and LogT by a scripted fake child process.  The stdout/stderr pumps are
real threads, so the tests also pin the lock/ordering contract between them.
"""

import os
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import logt_adapter
from import_progress import ImportStage
from logt_progress import is_tqdm_line

#: Password supplied by the GUI override.  It must never reach a callback.
SECRET = "override-SECRET-42"


# ---------------------------------------------------------------------------
# Fake child process with scripted pipes
# ---------------------------------------------------------------------------


class _ScriptedPipe:
    """File-like stub yielding scripted lines, optionally gated and released.

    ``gate``/``gate_after`` let a test interleave the two pipes deterministically:
    after ``gate_after`` lines the reader waits for ``gate`` before continuing.
    ``release`` unblocks a waiting reader, like a killed child closing its pipe.
    """

    def __init__(self, lines, *, gate=None, gate_after=None, done=None):
        self._lines = list(lines)
        self._gate = gate
        self._gate_after = gate_after
        self._done = done
        self._yielded = 0
        self._released = threading.Event()
        self.closed = False

    def release(self):
        """Unblock a pending reader with EOF."""
        self._released.set()

    def readline(self):
        if (
            self._gate is not None
            and self._gate_after is not None
            and self._yielded >= self._gate_after
        ):
            deadline = time.monotonic() + 5.0
            while (
                not self._gate.is_set()
                and not self._released.is_set()
                and time.monotonic() < deadline
            ):
                self._gate.wait(timeout=0.01)
            self._gate = None
            if self._released.is_set():
                return ""
        if self._released.is_set():
            return ""
        if self._lines:
            self._yielded += 1
            return self._lines.pop(0)
        if self._done is not None:
            self._done.set()
        return ""

    def read(self):
        return ""

    def close(self):
        self.closed = True
        self._released.set()


def _fake_process_factory(
    stdout_lines,
    stderr_lines,
    *,
    exit_code=0,
    running=False,
    gate_after=None,
):
    """Build a ``Popen`` stand-in over two scripted pipes.

    Args:
        stdout_lines: Lines the fake child writes to stdout.
        stderr_lines: Lines the fake child writes to stderr.
        exit_code: Exit code reported once the child has "finished".
        running: When true the child keeps running until it is terminated
            (used by the cancellation test).
        gate_after: Number of stdout lines after which the stdout pipe waits
            for stderr to reach EOF.  Makes the interleaving deterministic.

    Returns:
        A class suitable for ``mock.patch.object(logt_adapter.subprocess, "Popen")``.
    """
    stderr_done = threading.Event()
    stdout_pipe = _ScriptedPipe(
        stdout_lines,
        gate=stderr_done if gate_after is not None else None,
        gate_after=gate_after,
    )
    stderr_pipe = _ScriptedPipe(stderr_lines, done=stderr_done)

    class FakeProcess:
        def __init__(self, argv, **kwargs):
            self.argv = list(argv)
            self.stdout = stdout_pipe
            self.stderr = stderr_pipe
            self.stdin = None
            self.pid = 1
            self.returncode = None if running else exit_code
            self.terminated = False

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15
            stdout_pipe.release()
            stderr_pipe.release()

        def kill(self):
            self.terminated = True
            self.returncode = -9
            stdout_pipe.release()
            stderr_pipe.release()

    return FakeProcess


def _run_import(
    *,
    stdout_lines=(),
    stderr_lines=(),
    counts=None,
    exit_code=0,
    on_progress=None,
    progress_callback=None,
    count_lines_for_progress=True,
    cancel_event=None,
    running=False,
    gate_after=None,
    config_password=SECRET,
):
    """Run :func:`logt_adapter.run_logt_import` against the fake child.

    Every external dependency (entry point, ClickHouse, environment, local
    config) is stubbed so the test needs neither ``vendor/logt`` nor a database.

    Returns:
        The :class:`logt_adapter.ImportResult`.
    """
    scratch = Path(logt_adapter.REPO_ROOT) / ".tmp"
    scratch.mkdir(exist_ok=True)
    fake_popen = _fake_process_factory(
        list(stdout_lines),
        list(stderr_lines),
        exit_code=exit_code,
        running=running,
        gate_after=gate_after,
    )
    table_counts = {"events": 7} if counts is None else counts
    with mock.patch.dict(os.environ, {"LOGT_WORK_DIR": str(scratch)}, clear=False):
        os.environ.pop(logt_adapter.ENV_PASSWORD, None)
        with mock.patch.object(logt_adapter, "load_local_config", return_value={}):
            with mock.patch.object(logt_adapter.subprocess, "Popen", fake_popen):
                with mock.patch.object(
                    logt_adapter, "count_tables", return_value=table_counts
                ):
                    with mock.patch.object(
                        logt_adapter,
                        "_resolve_logt_entrypoint",
                        return_value=Path(logt_adapter.REPO_ROOT) / "logt_adapter.py",
                    ):
                        return logt_adapter.run_logt_import(
                            ".",
                            {"database": "tracelog", "password": config_password},
                            progress_callback=progress_callback,
                            cancel_event=cancel_event,
                            dataset_name="ds",
                            on_progress=on_progress,
                            count_lines_for_progress=count_lines_for_progress,
                        )


#: A realistic LogT stdout: scan, hour start and the final operations count.
STDOUT_LINES = [
    "[INFO] Найдено файлов rphost: 10, rmngr: 4\n",
    "[INFO] Часовых групп: 2\n",
    "[INFO] Час 2024-01-15_10: файлов 7 (rphost+rmngr)\n",
    "[INFO] Готово: обработано 1234 операций\n",
]

#: tqdm ticks with the hour prefix, without it, without a total, and with a
#: postfix.
STDERR_LINES = [
    "Час 1/2:  40%|####      | 400/1000 [00:01<00:02, 400строк/s]\n",
    "Час 2/2:  50%|#####     | 50.0k/100.0k [00:05<00:05, 9.80kстрок/s]\n",
    " 55%|#####     | 550/1000 [00:05<00:05, 100строк/s]\n",
    " 65%|######    | 650строк [00:05<00:05, 100строк/s]\n",
    "100%|##########| 1000/1000 [00:06<00:00, 160строк/s]\n",
]

#: Number of stdout lines that precede the first typed stderr tick.
STDOUT_BEFORE_TICKS = 3


class TestTypedProgressStages(unittest.TestCase):
    """Stage order, monotonic percentages and the ``DONE`` contract."""

    def test_full_sequence_reaches_done(self):
        """PREPARING → SCANNING → PROCESSING → INSERTING → COUNTING → DONE."""
        events = []
        result = _run_import(
            stdout_lines=STDOUT_LINES,
            stderr_lines=STDERR_LINES,
            on_progress=events.append,
            gate_after=STDOUT_BEFORE_TICKS,
        )

        self.assertTrue(result.success)
        stages = [event.stage for event in events]
        self.assertIs(stages[0], ImportStage.PREPARING)
        self.assertIs(stages[-1], ImportStage.DONE)
        collapsed = [
            stage
            for index, stage in enumerate(stages)
            if index == 0 or stage is not stages[index - 1]
        ]
        self.assertEqual(
            collapsed,
            [
                ImportStage.PREPARING,
                ImportStage.SCANNING,
                ImportStage.PROCESSING,
                ImportStage.INSERTING,
                ImportStage.COUNTING,
                ImportStage.DONE,
            ],
        )
        self.assertEqual(events[-1].percent, 100.0)
        self.assertIn("7 событий", events[-1].message)

    def test_known_percentages_never_decrease(self):
        """A coarse source cannot make the bar jump backwards."""
        events = []
        _run_import(
            stdout_lines=STDOUT_LINES,
            stderr_lines=STDERR_LINES,
            on_progress=events.append,
            gate_after=STDOUT_BEFORE_TICKS,
        )

        known = [event.percent for event in events if event.percent is not None]
        self.assertGreater(len(known), 3)
        self.assertEqual(known, sorted(known), f"percent regressed: {known!r}")

    def test_legacy_callback_still_receives_stdout_lines(self):
        """The old string callback is untouched by the typed bridge."""
        messages = []
        result = _run_import(
            stdout_lines=STDOUT_LINES,
            stderr_lines=STDERR_LINES,
            progress_callback=messages.append,
            gate_after=STDOUT_BEFORE_TICKS,
        )

        self.assertTrue(result.success)
        self.assertTrue(any("Найдено файлов" in message for message in messages))
        self.assertTrue(any("Готово" in message for message in messages))

    def test_counting_before_done(self):
        """COUNTING is reported just before ClickHouse counts the rows."""
        events = []
        _run_import(
            stdout_lines=STDOUT_LINES,
            stderr_lines=STDERR_LINES,
            on_progress=events.append,
            gate_after=STDOUT_BEFORE_TICKS,
        )

        counting = [event for event in events if event.stage is ImportStage.COUNTING]
        self.assertEqual(len(counting), 1)
        self.assertEqual(counting[0].percent, 95.0)


class TestStderrDiagnostics(unittest.TestCase):
    """tqdm ticks must not crowd real errors out of ``stderr_tail``."""

    def test_real_error_survives_and_ticks_are_dropped(self):
        stderr_lines = [
            "Час 1/2:  40%|####      | 400/1000 [00:01<00:02, 400строк/s, Операции=400]\n",
            "Fatal: cannot insert line 77\n",
            " 55%|#####     | 550/1000 [00:05<00:05, 100строк/s]\n",
        ]
        result = _run_import(
            stdout_lines=["[INFO] starting\n"],
            stderr_lines=stderr_lines,
            exit_code=1,
            counts={},
            on_progress=[].append,
        )

        self.assertFalse(result.success)
        self.assertEqual(result.exit_code, 1)
        self.assertTrue(
            any("Fatal: cannot insert line 77" in line for line in result.stderr_tail),
            result.stderr_tail,
        )
        joined = "\n".join(result.stderr_tail)
        self.assertNotIn("Час 1/2", joined)
        self.assertNotIn("Операции=", joined)
        self.assertNotIn("строк/s", joined)

    def test_is_tqdm_line_classifies_the_samples_as_ticks(self):
        """Guard the filter itself, not only its effect on the tail."""
        for line in STDERR_LINES:
            self.assertTrue(is_tqdm_line(line), line)
        self.assertFalse(is_tqdm_line("[INFO] Часовых групп: 2"))


class TestProgressRobustness(unittest.TestCase):
    """Failures on the progress path never fail the import."""

    def test_raising_on_progress_callback_is_swallowed(self):
        def explode(progress):
            raise RuntimeError("UI is gone")

        result = _run_import(
            stdout_lines=STDOUT_LINES,
            stderr_lines=STDERR_LINES,
            on_progress=explode,
            gate_after=STDOUT_BEFORE_TICKS,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.rows_loaded, 7)

    def test_rows_loaded_comes_from_clickhouse_not_stdout(self):
        """A misleading 100 % / Готово on stdout changes nothing."""
        optimistic = [
            "[INFO] Найдено файлов rphost: 10, rmngr: 4\n",
            "[INFO] Часовых групп: 1\n",
            "100%|##########| 1000/1000 [00:06<00:00, 160строк/s]\n",
            "[INFO] Готово: обработано 999999 операций\n",
        ]
        events = []
        result = _run_import(
            stdout_lines=optimistic,
            counts={"events": 7},
            on_progress=events.append,
        )

        self.assertTrue(result.success)
        self.assertEqual(result.rows_loaded, 7)
        self.assertIn("7 событий", events[-1].message)

    def test_cancelled_import_never_emits_done(self):
        cancel_event = threading.Event()
        cancel_event.set()
        events = []
        result = _run_import(
            on_progress=events.append,
            cancel_event=cancel_event,
            running=True,
        )

        self.assertTrue(result.cancelled)
        self.assertFalse(result.success)
        self.assertIs(events[0].stage, ImportStage.PREPARING)
        self.assertNotIn(ImportStage.DONE, [event.stage for event in events])

    def test_zero_rows_never_emits_done(self):
        events = []
        result = _run_import(
            stdout_lines=STDOUT_LINES,
            counts={"events": 0},
            on_progress=events.append,
        )

        self.assertFalse(result.success)
        self.assertNotIn(ImportStage.DONE, [event.stage for event in events])

    def test_override_password_is_redacted_in_typed_progress(self):
        events = []
        stdout_lines = [
            f"[INFO] Час pw-{SECRET}: файлов 7\n",
            "[INFO] Найдено файлов rphost: 1, rmngr: 1\n",
        ]
        result = _run_import(
            stdout_lines=stdout_lines,
            on_progress=events.append,
            count_lines_for_progress=True,
        )

        rendered = "\n".join(
            f"{event.message}\n{event.detail or ''}" for event in events
        )
        self.assertNotIn(SECRET, rendered)
        self.assertIn("***", rendered)
        self.assertNotIn(SECRET, result.stdout)
        self.assertNotIn(SECRET, result.stderr)


class TestCountLinesForProgress(unittest.TestCase):
    """The LogT payload exposes ``processing.count_lines_for_progress``."""

    @staticmethod
    def _config():
        return {
            "host": "127.0.0.1",
            "port": 9000,
            "user": "default",
            "password": "pw",
            "database": "tracelog",
        }

    def test_payload_defaults_to_true(self):
        payload = logt_adapter.build_logt_config("/logs", self._config())
        self.assertIs(payload["processing"]["count_lines_for_progress"], True)

    def test_payload_honours_false(self):
        payload = logt_adapter.build_logt_config(
            "/logs", self._config(), count_lines_for_progress=False
        )
        self.assertIs(payload["processing"]["count_lines_for_progress"], False)

    def test_run_import_forwards_the_flag(self):
        real = logt_adapter.build_logt_config
        with mock.patch.object(
            logt_adapter, "build_logt_config", wraps=real
        ) as builder:
            _run_import(
                stdout_lines=["[INFO] starting\n"],
                count_lines_for_progress=False,
            )

        self.assertIs(builder.call_args.kwargs["count_lines_for_progress"], False)

    def test_run_import_forwards_true_by_default(self):
        real = logt_adapter.build_logt_config
        with mock.patch.object(
            logt_adapter, "build_logt_config", wraps=real
        ) as builder:
            _run_import(stdout_lines=["[INFO] starting\n"])

        self.assertIs(builder.call_args.kwargs["count_lines_for_progress"], True)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
