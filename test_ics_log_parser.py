"""Tests for :mod:`ics_log_parser` (DEBT-001).

Covers the tolerant text parser (BUG-001), concurrent directory parsing
(PERF-001) and the JSON-lines variant.
"""

import json
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ics_log_parser
from ics_log_parser import (
    detect_format,
    parse_event_block,
    parse_log_directory,
    parse_log_file,
)


def _write(path: Path, text: str, encoding: str = "utf-8") -> str:
    path.write_text(text, encoding=encoding)
    return str(path)


def _write_bytes(path: Path, data: bytes) -> str:
    """Write raw bytes so line endings are not normalised by the platform."""
    path.write_bytes(data)
    return str(path)


#: Character pool for the seeded fuzz tests.  It mixes field syntax, quotes,
#: backslashes, ANSI/CR control characters and Cyrillic log words.
FUZZ_ALPHABET = (
    'abcXYZ019=,:."\\|%+-[](){} \t\r\x1b'
    "NUsrDurationEXCPГотовоЧас файлов строк/s"
)

#: Real-looking prefixes make the random lines reach more parser branches
#: without making the expected event count depend on the seed.
FUZZ_PREFIXES = (
    "",
    "05.04.2024 10:00:00",
    "    N=CALL, Usr=",
    '    Descr="',
    "// comment ",
    "[INFO] ",
)


def _fuzz_lines(seed: int, count: int = 300) -> list:
    """Return ``count`` deterministic pseudo-random text lines."""
    rng = random.Random(seed)
    lines = []
    for _ in range(count):
        prefix = rng.choice(FUZZ_PREFIXES)
        tail = "".join(
            rng.choice(FUZZ_ALPHABET) for _ in range(rng.randint(0, 60))
        )
        lines.append(prefix + tail)
    return lines


class TestDetectFormat(unittest.TestCase):
    def test_text_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp) / "a.log", "05.04.2024 10:00:00.000\n    N=CALL\n")
            self.assertEqual(detect_format(path), "text")

    def test_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp) / "a.log", '{"timestamp": "x", "N": "CALL"}\n')
            self.assertEqual(detect_format(path), "json")

    def test_missing_file_defaults_to_text(self):
        self.assertEqual(detect_format("does-not-exist.log"), "text")


class TestTextParser(unittest.TestCase):
    def _parse(self, text: str, encoding: str = "utf-8"):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(Path(tmp) / "a.log", text, encoding=encoding)
            return parse_log_file(path)

    def test_standard_event(self):
        events = self._parse(
            "05.04.2024 15:41:53.285067\n"
            "    N=EXCP, Usr=Иванов, p:processName=1CV8, t:process=42, "
            "AppID=app, Event=evt, Descr=\"Ошибка\", Duration=1500000\n"
        )
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["timestamp"], "05.04.2024 15:41:53.285067")
        self.assertEqual(event["name"], "EXCP")
        self.assertEqual(event["user"], "Иванов")
        self.assertEqual(event["process"], "1CV8")
        self.assertEqual(event["thread"], "42")
        self.assertEqual(event["app_id"], "app")
        self.assertEqual(event["event"], "evt")
        self.assertEqual(event["descr"], "Ошибка")
        self.assertEqual(event["duration"], 1500000)

    def test_timestamp_without_milliseconds(self):
        events = self._parse("05.04.2024 15:41:53\n    N=CALL\n")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["timestamp"], "05.04.2024 15:41:53")
        self.assertEqual(events[0]["name"], "CALL")

    def test_timestamp_with_comma_fraction_is_normalized(self):
        events = self._parse("05.04.2024 15:41:53,123456\n    N=CALL\n")
        self.assertEqual(events[0]["timestamp"], "05.04.2024 15:41:53.123456")

    def test_fields_without_space_after_comma(self):
        events = self._parse(
            "05.04.2024 10:00:00\nN=CALL,Usr=Петров,Duration=42\n"
        )
        self.assertEqual(events[0]["name"], "CALL")
        self.assertEqual(events[0]["user"], "Петров")
        self.assertEqual(events[0]["duration"], 42)

    def test_quoted_value_may_contain_commas(self):
        events = self._parse(
            "05.04.2024 10:00:00\n"
            '    N=EXCP, Descr="Ошибка, при выполнении", Usr=Иванов\n'
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["descr"], "Ошибка, при выполнении")
        self.assertEqual(events[0]["user"], "Иванов")

    def test_escaped_quotes_are_decoded(self):
        events = self._parse(
            "05.04.2024 10:00:00\n"
            '    N=EXCP, Descr="Ошибка \\"X\\"", Usr=Иванов\n'
        )
        self.assertEqual(events[0]["descr"], 'Ошибка "X"')

    def test_multiple_events_are_kept_apart(self):
        events = self._parse(
            "05.04.2024 10:00:00\n    N=CALL\n\n"
            "05.04.2024 10:00:01\n    N=EXCP\n"
        )
        self.assertEqual([event["name"] for event in events], ["CALL", "EXCP"])

    def test_utf8_bom_is_ignored(self):
        events = self._parse(
            "05.04.2024 10:00:00\n    N=CALL\n", encoding="utf-8-sig"
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["name"], "CALL")

    def test_comment_line_inside_event_is_ignored(self):
        events = self._parse(
            "05.04.2024 10:00:00\n// комментарий\n    N=CALL\n"
        )
        self.assertEqual(events[0]["name"], "CALL")

    def test_missing_file_returns_empty_list(self):
        self.assertEqual(parse_log_file("does-not-exist.log"), [])


class TestTextParserEdgeCases(unittest.TestCase):
    """Tolerant behaviour for broken values, long lines and odd line endings."""

    def _parse_bytes(self, payload: bytes):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_bytes(Path(tmp) / "a.log", payload)
            return parse_log_file(path)

    def test_permission_error_returns_empty_list_and_logs_warning(self):
        """An unreadable file (access denied) is reported, never raised."""
        denied = PermissionError(13, "Access is denied")
        with mock.patch("builtins.open", side_effect=denied):
            with self.assertLogs("ics_log_parser", level="WARNING") as captured:
                events = parse_log_file("denied.log")
        self.assertEqual(events, [])
        self.assertTrue(
            any("denied.log" in message for message in captured.output),
            msg=captured.output,
        )

    def test_unterminated_quote_keeps_the_event(self):
        """An unclosed quote swallows the rest of the line, but not the event."""
        events = self._parse_bytes(
            b'05.04.2024 10:00:00\n    N=EXCP, Descr="unclosed, Usr=Ivanov\n'
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["name"], "EXCP")
        self.assertTrue(events[0]["descr"].startswith('"'))

    def test_duration_edge_values_are_tolerated(self):
        """``abc`` becomes 0; zero and negative durations are kept as parsed."""
        cases = {
            b"05.04.2024 10:00:00\n    N=EXCP, Duration=abc\n": 0,
            b"05.04.2024 10:00:00\n    N=EXCP, Duration=0\n": 0,
            b"05.04.2024 10:00:00\n    N=EXCP, Duration=-42\n": -42,
        }
        for payload, expected in cases.items():
            with self.subTest(duration=expected):
                events = self._parse_bytes(payload)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["duration"], expected)

    def test_empty_name_is_kept_as_empty_string(self):
        """``N=`` yields an event with an empty name instead of being dropped."""
        events = self._parse_bytes(b"05.04.2024 10:00:00\n    N=, Usr=Ivanov\n")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["name"], "")
        self.assertEqual(events[0]["user"], "Ivanov")

    def test_one_megabyte_line_is_parsed(self):
        """A 1 MiB ``Descr`` value does not truncate or crash the parser."""
        value = "x" * (1024 * 1024)
        payload = (
            "05.04.2024 10:00:00\n"
            f'    N=EXCP, Descr="{value}"\n'
        ).encode("utf-8")
        events = self._parse_bytes(payload)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["name"], "EXCP")
        self.assertEqual(len(events[0]["descr"]), len(value))

    def test_crlf_bare_cr_and_mixed_line_endings(self):
        """Universal newlines keep every event for ``\\r\\n``, ``\\r`` and mixed files."""
        cases = {
            "crlf": (
                b"05.04.2024 10:00:00\r\n    N=A\r\n\r\n"
                b"05.04.2024 10:00:01\r\n    N=B\r\n"
            ),
            "bare_cr": (
                b"05.04.2024 10:00:00\r    N=A\r\r"
                b"05.04.2024 10:00:01\r    N=B\r"
            ),
            "mixed": (
                b"05.04.2024 10:00:00\r\n    N=A\r"
                b"05.04.2024 10:00:01\n    N=B\r\n"
            ),
        }
        for ending, payload in cases.items():
            with self.subTest(ending=ending):
                events = self._parse_bytes(payload)
                self.assertEqual([event["name"] for event in events], ["A", "B"])

    def test_seeded_fuzz_text_parser_does_not_crash(self):
        """300 deterministic random lines always produce a list of events."""
        lines = _fuzz_lines(seed=12345)
        payload = ("\n".join(lines) + "\n").encode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_bytes(Path(tmp) / "fuzz.log", payload)
            self.assertIn(detect_format(path), ("text", "json"))
            events = parse_log_file(path)
        self.assertIsInstance(events, list)
        for event in events:
            self.assertIn("timestamp", event)
            self.assertIn("name", event)
        # The helper used by the directory parser must be just as tolerant.
        for start in range(0, len(lines), 7):
            block = parse_event_block(lines[start:start + 7])
            self.assertIsInstance(block, dict)


class TestJsonParser(unittest.TestCase):
    def test_json_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                Path(tmp) / "a.log",
                json.dumps(
                    {
                        "timestamp": "2024-04-05T10:00:00,123",
                        "N": "EXCP",
                        "p:processName": "1CV8",
                        "t:process": "7",
                        "Usr": "Иванов",
                        "AppID": "app",
                        "Event": "evt",
                        "Descr": "boom",
                        "Duration": "1500",
                        "Context": "ctx",
                    },
                    ensure_ascii=False,
                )
                + "\n",
            )
            events = parse_log_file(path)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["timestamp"], "2024-04-05T10:00:00.123")
        self.assertEqual(event["name"], "EXCP")
        self.assertEqual(event["duration"], 1500)
        self.assertEqual(event["context"], "ctx")

    def test_malformed_json_line_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write(
                Path(tmp) / "a.log",
                '{"N": "CALL"}\nnot-json\n{"N": "EXCP"}\n',
            )
            events = parse_log_file(path)
        self.assertEqual([event["name"] for event in events], ["CALL", "EXCP"])


class TestDirectoryParser(unittest.TestCase):
    def test_parallel_parse_preserves_filename_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "b.log", "05.04.2024 10:00:02\n    N=B\n")
            _write(root / "a.log", "05.04.2024 10:00:01\n    N=A\n")
            _write(root / "c.log", "05.04.2024 10:00:03\n    N=C\n")
            events = parse_log_directory(str(root), max_workers=2)
        self.assertEqual([event["name"] for event in events], ["A", "B", "C"])

    def test_nested_directory_is_used_when_top_level_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = Path(tmp) / "sub"
            nested.mkdir()
            _write(nested / "a.log", "05.04.2024 10:00:00\n    N=NESTED\n")
            events = parse_log_directory(str(Path(tmp)))
        self.assertEqual([event["name"] for event in events], ["NESTED"])

    def test_missing_directory_returns_empty_list(self):
        self.assertEqual(parse_log_directory("does-not-exist"), [])

    def test_file_failure_does_not_abort_the_directory(self):
        """A file that fails mid-read is logged; other results are dropped safely."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "a.log", "05.04.2024 10:00:00\n    N=A\n")
            _write(root / "b.log", "05.04.2024 10:00:00\n    N=B\n")
            with mock.patch.object(
                ics_log_parser, "parse_log_file", side_effect=RuntimeError("boom")
            ):
                with self.assertLogs("ics_log_parser", level="ERROR"):
                    events = parse_log_directory(str(root))
        self.assertEqual(events, [])

    def test_empty_directory_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(parse_log_directory(tmp), [])


class TestParseEventBlock(unittest.TestCase):
    def test_block_parsing_accepts_colon_keys(self):
        event = parse_event_block(
            [
                "05.04.2024 10:00:00",
                "    N=CALL",
                "    p:processName=1CV8",
            ]
        )
        self.assertEqual(event["timestamp"], "05.04.2024 10:00:00")
        self.assertEqual(event["N"], "CALL")
        self.assertEqual(event["p:processName"], "1CV8")


if __name__ == "__main__":
    unittest.main()
