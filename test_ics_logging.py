"""Tests for :mod:`ics_logging` and the DEBT-003 logging strategy.

The application must not use ``print`` for diagnostics and must not swallow
unexpected exceptions silently.  These tests pin the single-handler setup, the
level switch and the module-level logging added to the parser, registry and GUI.
"""

from __future__ import annotations

import io
import logging
import unittest
from pathlib import Path
from unittest import mock

import ics_events_registry
import ics_log_analyzer
import ics_log_parser
import ics_logging


class LoggingTestBase(unittest.TestCase):
    """Keep the process-wide logging state clean between tests."""

    def setUp(self):
        self.stream = io.StringIO()
        self.handler = None
        ics_logging.configure_logging("DEBUG", stream=self.stream, force=True)
        for handler in logging.getLogger().handlers:
            if getattr(handler, ics_logging._HANDLER_MARKER, False):
                self.handler = handler
        self.addCleanup(self._restore)

    def _restore(self):
        root = logging.getLogger()
        for handler in list(root.handlers):
            if getattr(handler, ics_logging._HANDLER_MARKER, False):
                root.removeHandler(handler)
        root.setLevel(ics_logging.DEFAULT_LEVEL)
        ics_logging._configured = False

    def output(self) -> str:
        """Return the captured log text."""
        return self.stream.getvalue()


class TestResolveLevel(unittest.TestCase):
    """``ICS_LOG_LEVEL`` is parsed leniently."""

    def test_names_and_numbers(self):
        self.assertEqual(ics_logging.resolve_level("DEBUG"), logging.DEBUG)
        self.assertEqual(ics_logging.resolve_level("info"), logging.INFO)
        self.assertEqual(ics_logging.resolve_level("30"), logging.WARNING)
        self.assertEqual(ics_logging.resolve_level(logging.ERROR), logging.ERROR)

    def test_unknown_falls_back_to_default(self):
        self.assertEqual(ics_logging.resolve_level("LOUD"), ics_logging.DEFAULT_LEVEL)
        self.assertEqual(ics_logging.resolve_level(""), ics_logging.DEFAULT_LEVEL)

    def test_environment_variable_is_used(self):
        with mock.patch.dict("os.environ", {ics_logging.ENV_LOG_LEVEL: "ERROR"}):
            self.assertEqual(ics_logging.resolve_level(), logging.ERROR)


class TestConfigureLogging(LoggingTestBase):
    """One handler, one format, idempotent installation."""

    def test_records_reach_the_configured_stream(self):
        logging.getLogger("ics_log_parser").debug("hello")
        self.assertIn("hello", self.output())
        self.assertIn("ics_log_parser", self.output())

    def test_second_call_does_not_add_another_handler(self):
        before = len(logging.getLogger().handlers)
        with mock.patch.dict("os.environ", {ics_logging.ENV_LOG_LEVEL: "DEBUG"}):
            ics_logging.configure_logging("ERROR", stream=io.StringIO())
        self.assertEqual(len(logging.getLogger().handlers), before)

    def test_force_rebuilds_the_handler(self):
        replacement = io.StringIO()
        ics_logging.configure_logging("DEBUG", stream=replacement, force=True)
        logging.getLogger("ics_log_parser").debug("after-force")
        self.assertIn("after-force", replacement.getvalue())


class TestParserLogging(LoggingTestBase):
    """The parser reports failures through the logger, never ``print``."""

    def test_missing_directory_is_logged(self):
        with self.assertLogs("ics_log_parser", level="WARNING") as captured:
            events = ics_log_parser.parse_log_directory("no-such-directory-xyz")
        self.assertEqual(events, [])
        self.assertTrue(any("does not exist" in line for line in captured.output))

    def test_unreadable_file_is_logged(self):
        missing = str(Path("no-such-file-xyz.log"))
        with self.assertLogs("ics_log_parser", level="WARNING"):
            events = ics_log_parser.parse_log_file(missing)
        self.assertEqual(events, [])


class TestRegistryLogging(LoggingTestBase):
    """A broken registry is both recorded and logged."""

    def test_missing_registry_is_logged_and_reported(self):
        saved_registry = ics_events_registry._registry
        saved_error = ics_events_registry._load_error
        self.addCleanup(setattr, ics_events_registry, "_registry", saved_registry)
        self.addCleanup(setattr, ics_events_registry, "_load_error", saved_error)
        with mock.patch.object(
            ics_events_registry, "_EVENTS_PATH", Path("no-such-events.json")
        ):
            with mock.patch.object(ics_events_registry, "_registry", None):
                with self.assertLogs("ics_events_registry", level="WARNING"):
                    data = ics_events_registry.load_registry(force_reload=True)
                self.assertEqual(data, {"events": []})
                self.assertIsNotNone(ics_events_registry.registry_error())


class TestDispatcherLogging(LoggingTestBase):
    """A failing result handler must be logged instead of disappearing."""

    def test_handler_exception_is_logged(self):
        dispatcher = ics_log_analyzer.ResultDispatcher(mock.Mock())
        dispatcher.post("boom", object())

        def handler(kind, payload):
            raise RuntimeError("handler failed")

        with self.assertLogs("ics_log_analyzer", level="ERROR") as captured:
            dispatcher.drain(handler)
        self.assertTrue(any("handler failed" in line for line in captured.output))


if __name__ == "__main__":
    unittest.main()
