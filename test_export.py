"""Tests for :mod:`export`.

The export module is the single, UI-free writer for CSV, JSON and XLSX.  These
tests pin the public contract: encodings, byte-for-byte CSV compatibility with
the historical GUI writer, streaming JSON, XLSX limits, spreadsheet-injection
defusing, row limits and cancellation, and the atomic write guarantee (an
existing file survives every failure and no ``*.part`` files are left behind).

XLSX tests are skipped when ``openpyxl`` is not installed; everything else runs
on the standard library only.
"""

from __future__ import annotations

import ast
import csv
import io
import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

import export
from export import (
    ExportCancelled,
    ExportDependencyError,
    ExportError,
    ExportFormat,
    ExportFormatError,
    ExportLimitExceeded,
    defuse_formula,
    export_rows,
    filetypes,
    iter_paged_rows,
    iter_records,
    openpyxl_available,
    suggest_filename,
)

OPENPYXL_AVAILABLE = openpyxl_available()

BOM = b"\xef\xbb\xbf"


class ExportTestCase(unittest.TestCase):
    """Base class providing an isolated output directory per test."""

    def setUp(self) -> None:
        """Create a temporary directory that is removed after the test."""
        self._temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp_dir.cleanup)
        self.directory = Path(self._temp_dir.name)

    def target(self, name: str) -> Path:
        """Return a path inside the per-test directory."""
        return self.directory / name

    def part_files(self) -> list[str]:
        """Return the names of leftover atomic-write temporaries."""
        return sorted(path.name for path in self.directory.iterdir() if path.name.endswith(".part"))


class TestFormatAndResult(ExportTestCase):
    """Format lookup and the shape of :class:`export.ExportResult`."""

    def test_format_lookup_is_case_insensitive_and_rejects_unknown_values(self):
        """The enum accepts strings in any case; anything else is a clear error."""
        self.assertEqual([member.value for member in ExportFormat], ["csv", "json", "xlsx"])
        self.assertIs(ExportFormat("CSV"), ExportFormat.CSV)
        self.assertIs(ExportFormat("Xlsx"), ExportFormat.XLSX)
        with self.assertRaises(ExportFormatError):
            ExportFormat("xml")
        with self.assertRaises(ExportFormatError):
            export_rows(["a"], [[1]], self.target("x.csv"), "xml")
        # Format errors are also ValueErrors, so generic GUI handlers work.
        self.assertTrue(issubclass(ExportFormatError, ValueError))
        self.assertTrue(issubclass(ExportFormatError, ExportError))

    def test_result_describes_the_written_file(self):
        """The result carries path, format, counts, columns and file size."""
        destination = self.target("result.csv")
        result = export_rows(["a", "b"], [{"a": 1, "b": 2}], destination)
        self.assertEqual(result.path, str(destination.resolve()))
        self.assertIs(result.fmt, ExportFormat.CSV)
        self.assertEqual(result.row_count, 1)
        self.assertFalse(result.truncated)
        self.assertEqual(result.columns, ["a", "b"])
        self.assertEqual(result.file_size, destination.stat().st_size)
        self.assertGreater(result.file_size, 0)
        self.assertEqual(self.part_files(), [])


class TestCsvExport(ExportTestCase):
    """CSV must stay byte-for-byte compatible with the old GUI writer."""

    def test_bom_header_and_stringified_values(self):
        """CSV starts with a UTF-8 BOM and keeps the header in the first row."""
        destination = self.target("data.csv")
        export_rows(["a", "b", "c", "d"], [[1, 2.5, True, None]], destination)
        raw = destination.read_bytes()
        self.assertTrue(raw.startswith(BOM))
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline="")))
        self.assertEqual(rows[0], ["a", "b", "c", "d"])
        self.assertEqual(rows[1], ["1", "2.5", "True", ""])

    def test_output_matches_a_reference_writer(self):
        """Bytes are identical to ``csv.writer`` with the historical settings."""
        headers = ["Время", "Тип", "Значение"]
        rows = [
            ["2026-11-12 10:00:00", "EXCP", 1],
            ["2026-11-12 10:00:01", 'a,"b"', 2.5],
        ]
        destination = self.target("reference.csv")
        export_rows(headers, rows, destination)

        reference = io.StringIO(newline="")
        writer = csv.writer(reference)
        writer.writerow(headers)
        writer.writerows([[str(value) for value in row] for row in rows])
        expected = BOM + reference.getvalue().encode("utf-8")
        self.assertEqual(destination.read_bytes(), expected)

    def test_quotes_separators_quotes_and_newlines(self):
        """Values with CSV metacharacters round-trip through a reader."""
        destination = self.target("quoted.csv")
        values = ['a,b', 'say "hi"', "line1\nline2", " plain ", ""]
        export_rows(["value"], [[value] for value in values], destination)
        text = destination.read_bytes().decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text, newline="")))
        self.assertEqual([row[0] for row in rows[1:]], values)

    def test_empty_rows_write_only_the_header(self):
        """No data rows means a BOM, the header line and nothing else."""
        destination = self.target("empty.csv")
        result = export_rows(["a", "b"], [], destination)
        self.assertEqual(result.row_count, 0)
        self.assertFalse(result.truncated)
        text = destination.read_bytes().decode("utf-8-sig")
        self.assertEqual(text, "a,b\r\n")

    def test_mapping_rows_use_headers_and_custom_keys(self):
        """Mappings are read by header name unless ``keys`` overrides it."""
        destination = self.target("mapped.csv")
        export_rows(
            ["first", "second"],
            [{"first": 1, "second": 2}],
            destination,
        )
        self.assertEqual(
            destination.read_text(encoding="utf-8-sig").splitlines(),
            ["first,second", "1,2"],
        )

        reordered = self.target("keys.csv")
        rows = [{"x": "X", "y": "Y", "first": "ignored"}]
        export_rows(
            ["first", "second"],
            rows,
            reordered,
            keys=["x", "y"],
        )
        self.assertEqual(
            reordered.read_text(encoding="utf-8-sig").splitlines(),
            ["first,second", "X,Y"],
        )

    def test_row_width_mismatch_is_rejected(self):
        """A short row is an error, not silently padded or truncated."""
        with self.assertRaises(ExportError):
            list(iter_records([[1, 2], [3]], ["a", "b"]))
        destination = self.target("bad.csv")
        with self.assertRaises(ExportError):
            export_rows(["a", "b"], [[1, 2], [3]], destination)
        self.assertFalse(destination.exists())
        self.assertEqual(self.part_files(), [])
        with self.assertRaises(ExportError):
            export_rows(["a", "b"], None, destination)  # type: ignore[arg-type]

    def test_formula_like_strings_are_defused(self):
        """Leading formula characters get an apostrophe; numbers are untouched."""
        destination = self.target("defused.csv")
        values = ["=1+1", "+1", "-1", "@x", "\tx", "\rx", "plain", 5, -5]
        export_rows(["value"], [[value] for value in values], destination)
        text = destination.read_bytes().decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(text, newline="")))
        self.assertEqual(
            [row[0] for row in rows[1:]],
            ["'=1+1", "'+1", "'-1", "'@x", "'\tx", "'\rx", "plain", "5", "-5"],
        )
        self.assertEqual(defuse_formula(5), 5)
        self.assertEqual(defuse_formula(-5), -5)
        self.assertEqual(defuse_formula(True), True)

    def test_defusing_can_be_disabled(self):
        """``defuse_formulas=False`` writes the raw string."""
        destination = self.target("raw.csv")
        export_rows(["value"], [["=1+1"]], destination, defuse_formulas=False)
        self.assertIn("=1+1", destination.read_text(encoding="utf-8-sig"))


class TestJsonExport(ExportTestCase):
    """JSON is streamed, UTF-8 without BOM and carries authoritative metadata."""

    def test_structure_is_valid_json_without_bom(self):
        """The document parses and contains the documented fields."""
        destination = self.target("data.json")
        export_rows(
            ["a", "b"],
            [{"a": "2026-11-12", "b": "EXCP"}, {"a": "x", "b": 2}],
            destination,
            ExportFormat.JSON,
        )
        raw = destination.read_bytes()
        self.assertFalse(raw.startswith(BOM))
        data = json.loads(raw.decode("utf-8"))
        self.assertEqual(data["schema_version"], 1)
        self.assertIsNotNone(datetime.fromisoformat(data["exported_at"]).tzinfo)
        self.assertEqual(data["columns"], ["a", "b"])
        self.assertEqual(data["row_count"], 2)
        self.assertFalse(data["truncated"])
        self.assertEqual(
            data["rows"],
            [{"a": "2026-11-12", "b": "EXCP"}, {"a": "x", "b": 2}],
        )

    def test_metadata_is_merged_but_never_authoritative(self):
        """Caller metadata is merged; counts and columns come from the data."""
        destination = self.target("meta.json")
        metadata = {
            "source": "clickhouse",
            "dataset": "tracelog",
            "filters": {"user": "Иванов"},
            "row_count": 999,
            "columns": ["wrong"],
            "rows": ["wrong"],
            "schema_version": 7,
        }
        export_rows(["a", "b"], [[1, 2]], destination, "json", metadata=metadata)
        data = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(data["source"], "clickhouse")
        self.assertEqual(data["dataset"], "tracelog")
        self.assertEqual(data["filters"], {"user": "Иванов"})
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual(data["row_count"], 1)
        self.assertEqual(data["columns"], ["a", "b"])
        self.assertEqual(data["rows"], [{"a": 1, "b": 2}])

    def test_unicode_is_written_as_utf8(self):
        """``ensure_ascii=False`` keeps Cyrillic readable in the file."""
        destination = self.target("unicode.json")
        export_rows(["Пользователь"], [["Иванов"]], destination, "json")
        raw = destination.read_bytes()
        self.assertIn("Иванов".encode("utf-8"), raw)
        self.assertEqual(
            json.loads(raw.decode("utf-8"))["rows"], [{"Пользователь": "Иванов"}]
        )

    def test_duplicate_headers_raise_value_error(self):
        """JSON objects cannot hold two identical keys."""
        destination = self.target("dupe.json")
        with self.assertRaises(ValueError):
            export_rows(["a", "a"], [[1, 2]], destination, "json")
        self.assertFalse(destination.exists())

    def test_empty_rows(self):
        """An empty dataset still produces a valid document."""
        destination = self.target("empty.json")
        result = export_rows(["a"], [], destination, "json")
        data = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(data["rows"], [])
        self.assertEqual(data["row_count"], 0)
        self.assertEqual(result.row_count, 0)

    def test_rows_are_streamed_and_truncation_stops_consumption(self):
        """The writer never materialises the whole iterable."""
        destination = self.target("streamed.json")

        def rows():
            yield {"a": 1}
            yield {"a": 2}
            yield {"a": 3}
            raise AssertionError("export consumed rows past the truncation point")

        result = export_rows(
            ["a"], rows(), destination, "json", max_rows=2, allow_truncate=True
        )
        self.assertTrue(result.truncated)
        self.assertEqual(result.row_count, 2)
        data = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(data["rows"], [{"a": 1}, {"a": 2}])
        self.assertTrue(data["truncated"])
        self.assertEqual(data["row_count"], 2)


@unittest.skipUnless(OPENPYXL_AVAILABLE, "openpyxl is not installed")
class TestXlsxExport(ExportTestCase):
    """XLSX output is readable by openpyxl and respects Excel's limits."""

    @staticmethod
    def _load(destination: Path):
        from openpyxl import load_workbook

        return load_workbook(destination)

    def test_headers_values_freeze_pane_and_filter(self):
        """The data sheet has bold headers, frozen top row and an auto-filter."""
        destination = self.target("data.xlsx")
        export_rows(
            ["Время", "Тип", "Значение"],
            [["2026-11-12", "EXCP", 1], ["2026-11-13", "INFO", 2.5]],
            destination,
            "xlsx",
        )
        workbook = self._load(destination)
        self.assertEqual(workbook.sheetnames, ["Данные"])
        sheet = workbook["Данные"]
        self.assertEqual([cell.value for cell in sheet[1]], ["Время", "Тип", "Значение"])
        self.assertIs(sheet["A1"].font.bold, True)
        self.assertEqual(sheet.freeze_panes, "A2")
        self.assertEqual(sheet.auto_filter.ref, "A1:C3")
        self.assertEqual([cell.value for cell in sheet[2]], ["2026-11-12", "EXCP", 1])
        self.assertEqual(sheet["C3"].value, 2.5)
        self.assertGreater(sheet.column_dimensions["A"].width, 0)

    def test_metadata_sheet_is_added(self):
        """Metadata produces a second worksheet with key/value rows."""
        destination = self.target("meta.xlsx")
        export_rows(
            ["a"],
            [[1]],
            destination,
            "xlsx",
            metadata={"source": "clickhouse", "filters": {"user": "Иванов"}},
        )
        workbook = self._load(destination)
        self.assertEqual(workbook.sheetnames, ["Данные", "Метаданные"])
        sheet = workbook["Метаданные"]
        values = {str(row[0].value): row[1].value for row in sheet.iter_rows(min_row=2)}
        self.assertEqual(values["source"], "clickhouse")
        self.assertIn("Иванов", values["filters"])
        self.assertEqual(values["row_count"], 1)
        self.assertIs(values["truncated"], False)

    def test_control_characters_are_stripped_and_long_cells_truncated(self):
        """Excel rejects control chars and cells over 32 767 characters."""
        destination = self.target("cells.xlsx")
        dirty = "a\x00b\x07c\nd\te"
        long_text = "x" * 40_000
        export_rows(["dirty", "long"], [[dirty, long_text]], destination, "xlsx")
        sheet = self._load(destination)["Данные"]
        self.assertEqual(sheet["A2"].value, "abc\nd\te")
        self.assertEqual(len(sheet["B2"].value), 32_767)

    def test_formulas_are_written_as_text(self):
        """A formula-looking value never becomes an executable formula."""
        destination = self.target("formula.xlsx")
        export_rows(["value"], [["=1+1"]], destination, "xlsx")
        sheet = self._load(destination)["Данные"]
        self.assertEqual(sheet["A2"].value, "'=1+1")
        self.assertEqual(sheet["A2"].data_type, "s")

        raw = self.target("formula-raw.xlsx")
        export_rows(["value"], [["=1+1"]], raw, "xlsx", defuse_formulas=False)
        sheet = self._load(raw)["Данные"]
        self.assertEqual(sheet["A2"].value, "=1+1")
        self.assertEqual(sheet["A2"].data_type, "s")


class TestLimitsCancellationAndAtomicity(ExportTestCase):
    """Limits, cancellation and failures never damage an existing file."""

    def _formats(self):
        """Yield every format, skipping XLSX when openpyxl is missing."""
        for fmt in ExportFormat:
            if fmt is ExportFormat.XLSX and not OPENPYXL_AVAILABLE:
                continue
            yield fmt

    def test_limit_exceeded_leaves_existing_target_untouched(self):
        """A row-limit error must not modify the destination."""
        for fmt in self._formats():
            with self.subTest(fmt=fmt.value):
                destination = self.target(f"existing.{fmt.value}")
                destination.write_bytes(b"original")
                with self.assertRaises(ExportLimitExceeded):
                    export_rows(
                        ["a"], [[1], [2]], destination, fmt, max_rows=1
                    )
                self.assertEqual(destination.read_bytes(), b"original")
                self.assertEqual(self.part_files(), [])

    def test_allow_truncate_reports_and_writes_only_the_limit(self):
        """Truncation is explicit and deterministic."""
        destination = self.target("truncated.csv")
        result = export_rows(
            ["a"], [[1], [2], [3]], destination, max_rows=2, allow_truncate=True
        )
        self.assertTrue(result.truncated)
        self.assertEqual(result.row_count, 2)
        lines = destination.read_text(encoding="utf-8-sig").splitlines()
        self.assertEqual(lines, ["a", "1", "2"])

        # Exactly the limit is not truncation.
        exact = self.target("exact.csv")
        result = export_rows(
            ["a"], [[1], [2]], exact, max_rows=2, allow_truncate=True
        )
        self.assertFalse(result.truncated)
        self.assertEqual(result.row_count, 2)

    def test_max_rows_none_is_unlimited(self):
        """``None`` disables the caller limit for non-spreadsheet formats."""
        destination = self.target("unlimited.csv")
        result = export_rows(
            ["a"], ({"a": index} for index in range(1_200)), destination, max_rows=None
        )
        self.assertEqual(result.row_count, 1_200)
        self.assertFalse(result.truncated)

    def test_progress_is_throttled_and_ends_with_the_final_count(self):
        """Progress fires every 1 000 rows plus once at the end, same thread."""
        destination = self.target("progress.csv")
        calls: list[int] = []
        main_thread = threading.current_thread()

        def progress(count: int) -> None:
            self.assertIs(threading.current_thread(), main_thread)
            calls.append(count)

        result = export_rows(
            ["a"],
            ({"a": index} for index in range(2_500)),
            destination,
            max_rows=None,
            progress=progress,
        )
        self.assertEqual(calls, [1_000, 2_000, 2_500])
        self.assertEqual(result.row_count, 2_500)

    def test_cancel_before_start_writes_nothing(self):
        """A pre-set event stops the export before any file is created."""
        destination = self.target("cancelled.csv")
        destination.write_bytes(b"original")
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(ExportCancelled):
            export_rows(["a"], [[1]], destination, cancel=cancel)
        self.assertEqual(destination.read_bytes(), b"original")
        self.assertEqual(self.part_files(), [])

    def test_cancel_mid_export_cleans_up(self):
        """Cancellation after some rows removes the temporary file."""
        for fmt in self._formats():
            with self.subTest(fmt=fmt.value):
                destination = self.target(f"mid.{fmt.value}")
                destination.write_bytes(b"original")
                cancel = threading.Event()

                def rows():
                    yield {"a": 1}
                    yield {"a": 2}
                    cancel.set()
                    yield {"a": 3}

                with self.assertRaises(ExportCancelled):
                    export_rows(["a"], rows(), destination, fmt, cancel=cancel)
                self.assertEqual(destination.read_bytes(), b"original")
                self.assertEqual(self.part_files(), [])

    def test_invalid_row_preserves_target_and_removes_temporaries(self):
        """A validation failure is as safe as an I/O failure."""
        for fmt in self._formats():
            with self.subTest(fmt=fmt.value):
                destination = self.target(f"bad-row.{fmt.value}")
                destination.write_bytes(b"original")
                with self.assertRaises(ExportError):
                    export_rows(["a", "b"], [[1, 2], [3]], destination, fmt)
                self.assertEqual(destination.read_bytes(), b"original")
                self.assertEqual(self.part_files(), [])

    def test_invalid_destination_raises_export_error(self):
        """A directory or a missing parent is a clear ``ExportError``."""
        with self.assertRaises(ExportError):
            export_rows(["a"], [[1]], self.directory)
        with self.assertRaises(ExportError):
            export_rows(["a"], [[1]], self.directory / "missing" / "x.csv")


class TestIterPagedRows(ExportTestCase):
    """Paged iteration must stop on every documented condition."""

    def test_zero_total_makes_no_requests_and_short_page_stops(self):
        """``total=0`` is free; a short page ends iteration early."""
        calls: list[tuple[int, int]] = []

        def never(offset: int, limit: int):
            calls.append((offset, limit))
            return []

        self.assertEqual(list(iter_paged_rows(never, 0)), [])
        self.assertEqual(calls, [])

        short_calls: list[tuple[int, int]] = []

        def short(offset: int, limit: int):
            short_calls.append((offset, limit))
            return [1, 2, 3]

        self.assertEqual(list(iter_paged_rows(short, 10, page_size=5)), [1, 2, 3])
        self.assertEqual(short_calls, [(0, 5)])

    def test_total_boundary_is_not_crossed(self):
        """The iterator yields exactly ``total`` rows, page size regardless."""
        calls: list[tuple[int, int]] = []

        def fetch(offset: int, limit: int):
            calls.append((offset, limit))
            return list(range(offset, min(offset + limit, 5)))

        rows = list(iter_paged_rows(fetch, 5, page_size=2))
        self.assertEqual(rows, [0, 1, 2, 3, 4])
        self.assertEqual(calls, [(0, 2), (2, 2), (4, 2)])

    def test_max_rows_shrinks_the_last_request(self):
        """The page size is capped by the remaining row budget."""
        calls: list[tuple[int, int]] = []

        def fetch(offset: int, limit: int):
            calls.append((offset, limit))
            return list(range(offset, offset + min(limit, 10 - offset)))

        rows = list(iter_paged_rows(fetch, 10, page_size=3, max_rows=4))
        self.assertEqual(rows, [0, 1, 2, 3])
        self.assertEqual(calls, [(0, 3), (3, 1)])

    def test_cancel_raises_between_rows(self):
        """The event is checked per row, not only per page."""
        cancel = threading.Event()
        iterator = iter_paged_rows(
            lambda offset, limit: [1, 2, 3], 3, cancel=cancel
        )
        self.assertEqual(next(iterator), 1)
        cancel.set()
        with self.assertRaises(ExportCancelled):
            next(iterator)

    def test_source_ignoring_offset_still_terminates(self):
        """A broken source cannot loop forever: ``total`` is a hard stop."""
        calls: list[tuple[int, int]] = []

        def broken(offset: int, limit: int):
            calls.append((offset, limit))
            return [0, 1]

        rows = list(iter_paged_rows(broken, 5, page_size=2))
        self.assertEqual(len(rows), 5)
        self.assertEqual(len(calls), 3)

    def test_progress_reports_the_final_count(self):
        """The progress callback ends with the number of yielded rows."""
        calls: list[int] = []
        rows = list(
            iter_paged_rows(
                lambda offset, limit: [1, 2, 3], 3, progress=calls.append
            )
        )
        self.assertEqual(rows, [1, 2, 3])
        self.assertEqual(calls, [3])


class TestHelpers(ExportTestCase):
    """File types, file names, dependency detection and module purity."""

    def test_filetypes_returns_plain_tuples(self):
        """The dialog helper needs no UI toolkit."""
        all_types = filetypes()
        self.assertEqual(
            all_types,
            [
                ("CSV files", "*.csv"),
                ("JSON files", "*.json"),
                ("Excel files", "*.xlsx"),
                ("All files", "*.*"),
            ],
        )
        self.assertEqual(
            filetypes("CSV"), [("CSV files", "*.csv"), ("All files", "*.*")]
        )
        for label, pattern in all_types:
            self.assertIsInstance(label, str)
            self.assertIsInstance(pattern, str)
        with self.assertRaises(ExportFormatError):
            filetypes("pdf")

    def test_suggest_filename_is_safe_and_has_the_right_extension(self):
        """Illegal characters are replaced and the timestamp is stable."""
        moment = datetime(2026, 11, 12, 10, 15, 0)
        self.assertEqual(
            suggest_filename("errors", ExportFormat.CSV, moment),
            "errors_20261112_101500.csv",
        )
        self.assertEqual(
            suggest_filename("errors", "JSON", moment), "errors_20261112_101500.json"
        )
        self.assertTrue(
            suggest_filename("errors", ExportFormat.XLSX, moment).endswith(".xlsx")
        )
        sanitised = suggest_filename('a/b:c*? "d"', ExportFormat.CSV, moment)
        self.assertNotRegex(sanitised, r'[<>:"/\\|?*]')
        self.assertTrue(sanitised.endswith(".csv"))
        self.assertTrue(suggest_filename("", ExportFormat.CSV, moment).startswith("export_"))

    def test_openpyxl_available_and_lazy_import(self):
        """Availability matches the environment; the import helper is lazy."""
        import importlib.util

        expected = importlib.util.find_spec("openpyxl") is not None
        self.assertEqual(openpyxl_available(), expected)
        if expected:
            module = export._import_openpyxl()
            self.assertEqual(module.__name__, "openpyxl")
        else:  # pragma: no cover - depends on the environment
            with self.assertRaises(ExportDependencyError):
                export._import_openpyxl()

    def test_module_has_no_ui_imports(self):
        """The module stays headless: no UI toolkit and no module-level openpyxl."""
        source = (Path(__file__).resolve().parent / "export.py").read_text(encoding="utf-8")
        self.assertNotIn("tkinter", source.lower())
        tree = ast.parse(source)
        imported = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("tkinter", imported)
        self.assertNotIn("openpyxl", imported)


if __name__ == "__main__":
    unittest.main()
