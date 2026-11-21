"""Streaming tests for the LogT import runner (plan items P1-00a and P1-00b).

The import acceptance criterion is that pressing "Cancel" stops the child
process within two seconds.  These tests pin that budget down at the two
places that used to blow it: :func:`logt_adapter._terminate` (``terminate()``
followed by two five-second waits) and :func:`logt_adapter._drain_process`
(five-second joins per pipe pump).

They also pin the carriage-return contract: tqdm redraws its progress line
with a bare ``\\r`` and no newline, and the import must turn every redraw into
a separate progress message while the child is still running.

No ClickHouse connection is needed, so the module is fully offline.
"""

import inspect
import io
import os
import subprocess
import sys
import threading
import time
import unittest

import logt_adapter

#: Acceptance budget for a cancelled import, in seconds.
CANCEL_BUDGET = 2.0


class _BlockingStream:
    """Text-mode pipe stand-in whose ``readline`` blocks until released.

    Mimics a silent child that keeps its stdout/stderr pipes open: the pump
    thread cannot see EOF until :meth:`release` is called, which is what a
    killed child does implicitly when its pipe write ends disappear.
    """

    def __init__(self) -> None:
        self._released = threading.Event()
        self.closed = False

    def release(self) -> None:
        """Unblock any pending :meth:`readline` with EOF."""
        self._released.set()

    def readline(self) -> str:
        """Block until released, then report EOF."""
        self._released.wait(timeout=5.0)
        if self.closed:
            raise ValueError("I/O operation on closed file")
        return ""

    def close(self) -> None:
        """Close the pipe and release a pending reader."""
        self.closed = True
        self._released.set()


class _StuckStream(_BlockingStream):
    """Pipe stand-in that stays blocked even after ``close()``.

    Models a pipe still held open by an orphaned grandchild, i.e. the case the
    old five-second joins punished the user for.
    """

    def close(self) -> None:
        """Mark the stream closed without unblocking the reader."""
        self.closed = True


class _FakeProcess:
    """Minimal :class:`subprocess.Popen` stand-in used by the tests.

    Every blocking call is bounded so a broken test fails instead of hanging.

    Args:
        ignore_terminate: When true ``terminate()`` is ignored, forcing the
            ``kill()`` escalation path.
        wait_hook: Optional ``hook(process, call_index)`` invoked from
            :meth:`wait` before the built-in behaviour.
    """

    def __init__(self, *, ignore_terminate: bool = False, wait_hook=None) -> None:
        self.returncode = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0
        self.stdout = _BlockingStream()
        self.stderr = _BlockingStream()
        self.stdin = None
        self._ignore_terminate = ignore_terminate
        self._wait_hook = wait_hook

    def poll(self):
        """Return the exit code, or ``None`` while the child "runs"."""
        return self.returncode

    def terminate(self) -> None:
        """Ask the child to stop unless the test ignores that signal."""
        self.terminate_calls += 1
        if not self._ignore_terminate:
            self.returncode = -15
            self.stdout.release()
            self.stderr.release()

    def kill(self) -> None:
        """Kill the child unconditionally."""
        self.kill_calls += 1
        self.returncode = -9
        self.stdout.release()
        self.stderr.release()

    def wait(self, timeout=None):
        """Block briefly, then either return the exit code or time out."""
        self.wait_calls += 1
        if self._wait_hook is not None:
            self._wait_hook(self, self.wait_calls)
        if self.returncode is not None:
            return self.returncode
        # A real Popen.wait() blocks for the full timeout; a short pause keeps
        # the deadline semantics visible without slowing the suite down.
        time.sleep(min(timeout if timeout is not None else 0.05, 0.05))
        raise subprocess.TimeoutExpired(cmd="fake-child", timeout=timeout)


class _BrokenProcess:
    """Process whose every operation fails with ``OSError``."""

    def poll(self):
        """Fail like a handle that was already closed."""
        raise OSError("broken handle")

    def terminate(self) -> None:
        """Fail like a handle that was already closed."""
        raise OSError("broken handle")

    def kill(self) -> None:
        """Fail like a handle that was already closed."""
        raise OSError("broken handle")

    def wait(self, timeout=None):
        """Fail like a handle that was already closed."""
        raise OSError("broken handle")


def _spawn_sleeper() -> subprocess.Popen:
    """Start a real Python child that sleeps far longer than any test."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _reap(process: subprocess.Popen) -> None:
    """Force a real child to exit and be reaped; safe to call repeatedly."""
    try:
        if process.poll() is None:
            process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass
    logt_adapter._close_streams(process)


#: Number of ``\r`` progress ticks the real child emits.
CR_TICKS = 3

#: Seconds the child sleeps between ticks, i.e. the pacing a tqdm bar has.
CR_INTERVAL = 0.15

#: Wall-clock ceiling for every real-child read in these tests.
STREAM_TIMEOUT = 5.0

#: Child script that redraws one line with a bare ``\r``, like tqdm does.
CR_SCRIPT = (
    "import sys, time\n"
    "for tick in range({ticks}):\n"
    "    sys.stdout.write('tick %d\\r' % tick)\n"
    "    sys.stdout.flush()\n"
    "    time.sleep({interval})\n"
).format(ticks=CR_TICKS, interval=CR_INTERVAL)


def _spawn_cr_child() -> subprocess.Popen:
    """Start a real child that writes ``\\r``-terminated ticks to stdout.

    The ``Popen`` options mirror :func:`logt_adapter.run_logt_import` exactly
    (``text=True`` with UTF-8 and replacement errors, default universal
    newlines): that combination is the contract under test.

    Returns:
        The running child; callers must reap it with :func:`_reap`.
    """
    return subprocess.Popen(
        [sys.executable, "-u", "-c", CR_SCRIPT],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _text_pipe(payload: bytes) -> io.TextIOWrapper:
    """Text-mode stand-in for a ``Popen`` pipe, with universal newlines.

    ``newline=None`` is what ``Popen(text=True)`` uses, so ``\\r`` and
    ``\\r\\n`` are translated to ``\\n`` exactly as they are for a real child.

    Args:
        payload: Raw bytes the "child" wrote into the pipe.

    Returns:
        A readable text wrapper over those bytes.
    """
    return io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8", errors="replace")


class TestTerminateBudget(unittest.TestCase):
    """``_terminate`` must escalate promptly and stay inside the budget."""

    def test_ignored_terminate_escalates_to_kill(self):
        """A child that ignores ``terminate()`` is killed and reaped."""
        process = _FakeProcess(ignore_terminate=True)
        started = time.monotonic()
        logt_adapter._terminate(process)
        elapsed = time.monotonic() - started

        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 1)
        self.assertGreaterEqual(process.wait_calls, 2)
        self.assertIsNotNone(process.poll())
        self.assertLess(elapsed, CANCEL_BUDGET)

    def test_terminate_does_not_kill_cooperative_child(self):
        """A child that honours ``terminate()`` is never killed."""
        process = _FakeProcess()
        logt_adapter._terminate(process)

        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 0)
        self.assertIsNotNone(process.poll())

    def test_finished_process_is_left_alone(self):
        """An already exited child is not signalled again."""
        process = _FakeProcess()
        process.returncode = 0

        logt_adapter._terminate(process)

        self.assertEqual(process.terminate_calls, 0)
        self.assertEqual(process.kill_calls, 0)

    def test_os_errors_are_swallowed(self):
        """Best-effort semantics: broken handles must not raise."""
        logt_adapter._terminate(_BrokenProcess())

    def test_repeated_cancellation_is_idempotent(self):
        """Calling ``_terminate`` again after a kill is a harmless no-op."""
        process = _FakeProcess(ignore_terminate=True)
        logt_adapter._terminate(process)
        calls = (process.terminate_calls, process.kill_calls)

        for _ in range(3):
            logt_adapter._terminate(process)

        self.assertEqual((process.terminate_calls, process.kill_calls), calls)
        self.assertIsNotNone(process.poll())

    def test_grace_defaults_are_within_budget(self):
        """The documented defaults leave the whole cancel path under 2 s."""
        signature = inspect.signature(logt_adapter._terminate)
        grace = signature.parameters["grace"].default

        self.assertEqual(grace, logt_adapter.TERMINATE_GRACE)
        self.assertLessEqual(grace + logt_adapter.KILL_GRACE, CANCEL_BUDGET)


class TestDrainProcessCancellation(unittest.TestCase):
    """``_drain_process`` returns quickly and leaves no live child."""

    def test_already_set_cancel_event_stops_silent_child(self):
        """Blocking pipes plus a pre-set cancel event return in under 2 s."""
        process = _FakeProcess()
        cancel_event = threading.Event()
        cancel_event.set()

        started = time.monotonic()
        logt_adapter._drain_process(process, [], [], cancel_event=cancel_event)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, CANCEL_BUDGET)
        self.assertEqual(process.terminate_calls, 1)
        self.assertIsNotNone(process.poll())

    def test_cancel_during_wait_is_detected(self):
        """Cancel raised while waiting is noticed on the next loop pass."""
        cancel_event = threading.Event()

        def hook(process, call_index):
            """Signal cancellation on the first wait, then time out."""
            if call_index == 1:
                cancel_event.set()
                raise subprocess.TimeoutExpired(cmd="fake-child", timeout=0.1)

        process = _FakeProcess(wait_hook=hook)
        started = time.monotonic()
        logt_adapter._drain_process(process, [], [], cancel_event=cancel_event)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, CANCEL_BUDGET)
        self.assertEqual(process.terminate_calls, 1)
        self.assertIsNotNone(process.poll())

    def test_uncancelled_child_is_waited_for(self):
        """Without cancellation the drain simply waits for the exit code."""
        process = _FakeProcess()

        def hook(process, call_index):
            """Let the fake child finish on its second wait."""
            if call_index == 2:
                process.returncode = 0

        process._wait_hook = hook
        logt_adapter._drain_process(process, [], [])

        self.assertEqual(process.terminate_calls, 0)
        self.assertIsNotNone(process.poll())

    def test_pump_join_is_bounded_when_pipes_stay_open(self):
        """A pipe held open by a grandchild cannot stall cancellation."""
        process = _FakeProcess()
        process.stdout = _StuckStream()
        process.stderr = _StuckStream()
        cancel_event = threading.Event()
        cancel_event.set()

        started = time.monotonic()
        logt_adapter._drain_process(process, [], [], cancel_event=cancel_event)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, CANCEL_BUDGET)
        self.assertIsNotNone(process.poll())

    def test_default_poll_interval_is_short(self):
        """Cancellation is polled at least ten times a second by default."""
        signature = inspect.signature(logt_adapter._drain_process)

        self.assertLessEqual(signature.parameters["poll_interval"].default, 0.1)

    def test_pumps_exit_when_pipes_close(self):
        """Closing the pipes lets both pump threads finish."""
        process = _FakeProcess()
        cancel_event = threading.Event()
        cancel_event.set()

        logt_adapter._drain_process(process, [], [], cancel_event=cancel_event)
        # Terminate released the pipes, so both readers must have seen EOF.
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)


class TestRealProcessCancellation(unittest.TestCase):
    """End-to-end timing against a real, silent child process."""

    def test_real_sleeping_child_is_cancelled_within_budget(self):
        """A real child is killed, reaped and drained in under two seconds."""
        process = _spawn_sleeper()
        cancel_event = threading.Event()
        cancel_event.set()
        try:
            started = time.monotonic()
            logt_adapter._drain_process(
                process, [], [], cancel_event=cancel_event
            )
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, CANCEL_BUDGET)
            self.assertIsNotNone(process.poll())
        finally:
            _reap(process)

    @unittest.skipUnless(os.name != "nt", "zombie reaping is POSIX-specific")
    def test_real_child_is_reaped_not_zombie(self):
        """After cancellation the child is gone, not a zombie entry."""
        process = _spawn_sleeper()
        cancel_event = threading.Event()
        cancel_event.set()
        try:
            logt_adapter._drain_process(
                process, [], [], cancel_event=cancel_event
            )
            self.assertIsNotNone(process.poll())
            # If Popen already reaped the child, waitpid reports ECHILD.
            with self.assertRaises(ChildProcessError):
                os.waitpid(process.pid, os.WNOHANG)
        finally:
            _reap(process)


class TestCarriageReturnStreaming(unittest.TestCase):
    """Pin the ``\\r``-update contract that tqdm progress bars rely on.

    A tqdm bar redraws a single line with a bare ``\\r`` and no newline.  The
    import runs the child in text mode with default universal newlines, so
    :func:`logt_adapter._pump_stream` must still see every redraw as its own
    line.  If it did not, progress would jump from 0 % to 100 % only when the
    child exited, and the ticks would arrive glued together.
    """

    def test_real_child_ticks_arrive_as_separate_lines(self):
        """A real child's ``\\r`` ticks reach the callback one at a time."""
        process = _spawn_cr_child()
        sink = []
        ticks = []
        arrivals = []
        started = time.monotonic()

        def observe(text: str) -> None:
            """Record one callback together with the moment it arrived."""
            ticks.append(text)
            arrivals.append(time.monotonic() - started)

        pump = threading.Thread(
            target=logt_adapter._pump_stream,
            args=(process.stdout, sink),
            kwargs={"on_line": observe},
            daemon=True,
        )
        pump.start()
        try:
            try:
                exit_code = process.wait(timeout=STREAM_TIMEOUT)
            except subprocess.TimeoutExpired:
                self.fail(f"child did not exit within {STREAM_TIMEOUT} s")
        finally:
            _reap(process)
            pump.join(timeout=STREAM_TIMEOUT)

        self.assertFalse(pump.is_alive(), "stdout pump never saw EOF")
        self.assertEqual(exit_code, 0)
        expected = [f"tick {index}" for index in range(CR_TICKS)]
        self.assertEqual(len(sink), CR_TICKS)
        self.assertEqual(sink, expected)
        self.assertEqual(ticks, expected)
        self.assertFalse(
            any("\r" in line for line in sink),
            f"carriage return leaked into the lines: {sink!r}",
        )
        # Glued output ("tick 0tick 1tick 2") would collapse into one element,
        # so the length check above also proves the lines were split.
        self.assertGreaterEqual(
            arrivals[-1] - arrivals[0],
            CR_INTERVAL,
            f"ticks arrived in one lump at exit: {arrivals!r}",
        )

    def test_drain_process_streams_ticks_through_pump_threads(self):
        """The full drain path used by the import yields every tick live."""
        process = _spawn_cr_child()
        stdout_sink = []
        stderr_sink = []
        ticks = []
        outcome = {}

        def drain() -> None:
            """Run the real drain loop and report how it ended."""
            try:
                logt_adapter._drain_process(
                    process, stdout_sink, stderr_sink, on_line=ticks.append
                )
                outcome["finished"] = True
            except BaseException as exc:  # pragma: no cover - reported below
                outcome["error"] = exc

        worker = threading.Thread(target=drain, daemon=True)
        started = time.monotonic()
        worker.start()
        try:
            worker.join(timeout=STREAM_TIMEOUT)
        finally:
            _reap(process)
            worker.join(timeout=STREAM_TIMEOUT)

        self.assertFalse(worker.is_alive(), "_drain_process did not return")
        self.assertNotIn("error", outcome)
        self.assertTrue(outcome.get("finished"))
        self.assertLess(time.monotonic() - started, STREAM_TIMEOUT)
        expected = [f"tick {index}" for index in range(CR_TICKS)]
        self.assertEqual(stdout_sink, expected)
        self.assertEqual(ticks, expected)
        self.assertEqual(stderr_sink, [])
        self.assertIsNotNone(process.poll())

    def test_text_pipe_splits_bare_carriage_returns(self):
        """``\\r``-separated writes become separate, ``\\r``-free lines."""
        sink = []

        logt_adapter._pump_stream(_text_pipe(b"a\rb\r"), sink)

        self.assertEqual(sink, ["a", "b"])
        self.assertFalse(any("\r" in line for line in sink))

    def test_crlf_does_not_leave_carriage_return(self):
        """Windows line endings are reduced to plain text lines."""
        sink = []

        logt_adapter._pump_stream(_text_pipe(b"line1\r\nline2\r\n"), sink)

        self.assertEqual(sink, ["line1", "line2"])
        self.assertFalse(any("\r" in line for line in sink))

    def test_empty_lines_and_eof_do_not_break_reading(self):
        """Blank lines are kept in the sink but never reach the callback."""
        sink = []
        ticks = []

        logt_adapter._pump_stream(
            _text_pipe(b"\n\n\nlast\r\n"), sink, on_line=ticks.append
        )

        self.assertEqual(sink, ["", "", "", "last"])
        self.assertEqual(ticks, ["last"])

    def test_final_line_without_terminator_is_kept(self):
        """A last line written without ``\\r``/``\\n`` is still reported."""
        sink = []

        logt_adapter._pump_stream(_text_pipe(b"tail"), sink)

        self.assertEqual(sink, ["tail"])


if __name__ == "__main__":
    unittest.main()
