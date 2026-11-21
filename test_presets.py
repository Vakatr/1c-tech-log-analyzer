"""Tests for :mod:`presets`.

The suite pins the public contract of the persistent filter presets: the
project-local default path, the ``ICS_PRESETS_PATH`` override, the allow-list
that keeps credentials out of the file, validation and normalization rules,
partial recovery from corrupt documents, atomic writes and archive pruning.

Only ``test_presets.py`` is needed: the module is Tk-free and filter objects are
duck-typed, so the tests use :class:`types.SimpleNamespace` instead of importing
the adapter or the GUI.
"""

from __future__ import annotations

import ast
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import presets
from presets import (
    ALLOWED_FILTER_FIELDS,
    DEFAULT_PRESETS_PATH,
    MAX_DURATION_MS,
    MAX_FILE_BYTES,
    MAX_NAME_LENGTH,
    MAX_PRESETS,
    MAX_STRING_LENGTH,
    PresetStore,
    PresetsStoreError,
    PresetsValidationError,
    SCHEMA_VERSION,
    dict_to_filter_kwargs,
    filters_to_dict,
)


def sample_filters(**overrides: object) -> SimpleNamespace:
    """Build a duck-typed filter object with all allow-listed fields set."""
    values: dict[str, object] = {
        "dataset": None,
        "date_from": None,
        "date_to": None,
        "user": None,
        "event_name": None,
        "search": None,
        "process_name": None,
        "app_id": None,
        "min_duration_ms": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class PresetsTestCase(unittest.TestCase):
    """Base class that provides an isolated temporary store path."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.path = self.root / "config" / "presets.json"

    def make_store(self) -> PresetStore:
        """Return a store bound to the temporary path."""
        return PresetStore(self.path)

    def read_document(self) -> dict:
        """Parse the store file as JSON."""
        return json.loads(self.path.read_text(encoding="utf-8"))

    def write_document(self, document: object) -> None:
        """Write ``document`` as JSON to the store path."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(document, ensure_ascii=False), encoding="utf-8"
        )

    def archives(self) -> list[Path]:
        """Return every corrupt-file archive next to the store."""
        return sorted(self.path.parent.glob("presets.corrupt-*.json"))


class TestDefaults(PresetsTestCase):
    """Path resolution and empty-store behaviour."""

    def test_default_presets_path_is_project_config(self) -> None:
        """The default file is ``<project>/config/presets.json``."""
        expected = Path(presets.__file__).resolve().parent / "config" / "presets.json"
        self.assertEqual(DEFAULT_PRESETS_PATH, expected)
        self.assertEqual(DEFAULT_PRESETS_PATH.name, "presets.json")
        self.assertEqual(DEFAULT_PRESETS_PATH.parent.name, "config")
        self.assertEqual(SCHEMA_VERSION, 1)

    def test_environment_override_selects_path(self) -> None:
        """``ICS_PRESETS_PATH`` wins over the project default."""
        with mock.patch.dict(
            os.environ, {"ICS_PRESETS_PATH": str(self.path)}, clear=False
        ):
            store = PresetStore()
            self.assertEqual(store.path, self.path)
            self.assertEqual(store.load(), [])

    def test_missing_file_is_empty_and_not_created(self) -> None:
        """A missing file is an empty store and reading does not create it."""
        store = self.make_store()
        self.assertEqual(store.load(), [])
        self.assertEqual(store.names(), [])
        self.assertIsNone(store.last_error)
        self.assertFalse(self.path.exists())


class TestFilterSerialization(PresetsTestCase):
    """Allow-list, normalization and round-trips of filter values."""

    def test_save_and_load_round_trip(self) -> None:
        """Saved filters and metadata survive a reload unchanged."""
        store = self.make_store()
        saved = store.save_preset(
            "Errors",
            sample_filters(user="alice", event_name="Error", search="timeout"),
        )
        self.assertEqual(saved["name"], "Errors")
        self.assertIn("created_at", saved)
        self.assertIn("updated_at", saved)

        document = self.read_document()
        self.assertEqual(document["schema_version"], 1)
        self.assertIsNone(document["last_used"])
        self.assertEqual(len(document["presets"]), 1)
        datetime.fromisoformat(document["presets"][0]["created_at"])

        reloaded = self.make_store()
        loaded = reloaded.load()
        self.assertEqual(len(loaded), 1)
        # Copies are returned: mutating them must not corrupt the cache.
        loaded[0]["filters"]["user"] = "mutated"
        preset = reloaded.get("Errors")
        self.assertIsNotNone(preset)
        self.assertEqual(preset["filters"]["user"], "alice")

        kwargs = dict_to_filter_kwargs(preset["filters"])
        self.assertEqual(kwargs["user"], "alice")
        self.assertEqual(kwargs["event_name"], "Error")
        self.assertEqual(kwargs["search"], "timeout")

    def test_allow_list_excludes_sensitive_fields_and_dataset(self) -> None:
        """Only allow-listed fields are stored; dataset is opt-in."""
        secret = "super-secret-password"
        filters = sample_filters(
            user="alice",
            dataset="import_2026",
            password=secret,
            host="clickhouse.local",
            log_dir="D:/logs",
        )
        serialized = filters_to_dict(filters)
        self.assertEqual(serialized, {"user": "alice"})
        for field in ("dataset", "password", "host", "log_dir"):
            self.assertNotIn(field, serialized)

        saved = self.make_store().save_preset("Safe", filters)
        self.assertNotIn("dataset", saved["filters"])
        self.assertNotIn(secret, self.path.read_text(encoding="utf-8"))
        for field in ("password", "host", "log_dir"):
            self.assertNotIn(field, self.read_document()["presets"][0]["filters"])

        unknown = dict_to_filter_kwargs(
            {"user": "bob", "password": secret, "filters": "ignored"}
        )
        self.assertEqual(unknown, {"user": "bob"})

    def test_include_dataset_is_opt_in(self) -> None:
        """``include_dataset=True`` keeps a trimmed dataset value."""
        filters = sample_filters(dataset="  import_2026  ", user="alice")
        store = self.make_store()
        saved = store.save_preset("With dataset", filters, include_dataset=True)
        self.assertEqual(saved["filters"]["dataset"], "import_2026")
        self.assertIn('"dataset": "import_2026"', self.path.read_text("utf-8"))
        self.assertEqual(
            dict_to_filter_kwargs(saved["filters"])["dataset"], "import_2026"
        )

    def test_whitespace_and_control_characters_are_cleaned(self) -> None:
        """Strings are stripped, blanks dropped and control chars removed."""
        serialized = filters_to_dict(
            sample_filters(
                user="  alice  ",
                event_name="\x00Error\x07",
                search="   ",
                process_name="\t1cv8\r\n",
            )
        )
        self.assertEqual(serialized["user"], "alice")
        self.assertEqual(serialized["event_name"], "Error")
        self.assertEqual(serialized["process_name"], "1cv8")
        self.assertNotIn("search", serialized)

        long_value = filters_to_dict(sample_filters(search="x" * 2000))
        self.assertEqual(len(long_value["search"]), MAX_STRING_LENGTH)

    def test_date_round_trip_and_ordering(self) -> None:
        """Dates are stored as ISO strings and restored as datetimes."""
        filters = sample_filters(
            date_from=datetime(2026, 1, 2, 3, 4, 5),
            date_to=datetime(2026, 2, 3),
        )
        saved = self.make_store().save_preset("Dates", filters)
        self.assertEqual(saved["filters"]["date_from"], "2026-01-02T03:04:05")
        self.assertEqual(saved["filters"]["date_to"], "2026-02-03T00:00:00")

        kwargs = dict_to_filter_kwargs(saved["filters"])
        self.assertEqual(kwargs["date_from"], datetime(2026, 1, 2, 3, 4, 5))
        self.assertEqual(kwargs["date_to"], datetime(2026, 2, 3))

        date_only = dict_to_filter_kwargs(
            {"date_from": "2026-01-02", "date_to": "2026-01-03"}
        )
        self.assertEqual(date_only["date_from"], datetime(2026, 1, 2))
        self.assertEqual(
            filters_to_dict(sample_filters(date_from="2026-01-02")),
            {"date_from": "2026-01-02T00:00:00"},
        )

        with self.assertRaises(PresetsValidationError):
            filters_to_dict(
                sample_filters(
                    date_from=datetime(2026, 2, 1),
                    date_to=datetime(2026, 1, 1),
                )
            )
        with self.assertRaises(PresetsValidationError):
            dict_to_filter_kwargs(
                {"date_from": "2026-02-01", "date_to": "2026-01-01"}
            )
        with self.assertRaises(PresetsValidationError):
            dict_to_filter_kwargs({"date_from": "not-a-date"})

    def test_min_duration_validation(self) -> None:
        """Duration is a positive bounded int; bool, zero and negatives differ."""
        self.assertEqual(
            filters_to_dict(sample_filters(min_duration_ms=1500)),
            {"min_duration_ms": 1500},
        )
        for value in (0, -5):
            with self.subTest(value=value):
                self.assertNotIn(
                    "min_duration_ms",
                    filters_to_dict(sample_filters(min_duration_ms=value)),
                )
                self.assertEqual(
                    dict_to_filter_kwargs({"min_duration_ms": value}), {}
                )
        self.assertEqual(
            dict_to_filter_kwargs({"min_duration_ms": MAX_DURATION_MS})[
                "min_duration_ms"
            ],
            MAX_DURATION_MS,
        )

        invalid = (True, MAX_DURATION_MS + 1, "1500", 1.5)
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(PresetsValidationError):
                    filters_to_dict(sample_filters(min_duration_ms=value))
                with self.assertRaises(PresetsValidationError):
                    dict_to_filter_kwargs({"min_duration_ms": value})

    def test_allow_list_constant_is_stable(self) -> None:
        """The allow-list keeps the documented field order."""
        self.assertEqual(
            ALLOWED_FILTER_FIELDS,
            (
                "dataset",
                "date_from",
                "date_to",
                "user",
                "event_name",
                "search",
                "process_name",
                "app_id",
                "min_duration_ms",
            ),
        )


class TestNamesAndMutations(PresetsTestCase):
    """Name validation, updates, deletion and ``last_used``."""

    def test_name_validation(self) -> None:
        """Names are stripped, bounded and free of control characters."""
        store = self.make_store()
        invalid = ("", "   ", "\nbad", "bad\tname", 123, None, "x" * (MAX_NAME_LENGTH + 1))
        for name in invalid:
            with self.subTest(name=name):
                with self.assertRaises(PresetsValidationError):
                    store.save_preset(name, sample_filters(user="alice"))
        self.assertFalse(self.path.exists())

        saved = store.save_preset("  Padded  ", sample_filters(user="alice"))
        self.assertEqual(saved["name"], "Padded")
        self.assertEqual(self.read_document()["presets"][0]["name"], "Padded")

    def test_duplicate_save_updates_in_place(self) -> None:
        """Re-saving a name preserves ``created_at`` and refreshes ``updated_at``."""
        store = self.make_store()
        first = store.save_preset("P", sample_filters(user="alice"))
        self.assertEqual(first["created_at"], first["updated_at"])

        document = self.read_document()
        document["presets"][0]["created_at"] = "2020-01-01T00:00:00+00:00"
        document["presets"][0]["updated_at"] = "2020-01-01T00:00:00+00:00"
        self.write_document(document)

        reloaded = self.make_store()
        second = reloaded.save_preset(" P ", sample_filters(user="bob"))
        self.assertEqual(second["created_at"], "2020-01-01T00:00:00+00:00")
        self.assertNotEqual(second["updated_at"], "2020-01-01T00:00:00+00:00")
        self.assertEqual(len(reloaded.load()), 1)
        self.assertEqual(reloaded.get("P")["filters"]["user"], "bob")

    def test_delete_preset(self) -> None:
        """Deleting returns ``True`` once, then ``False`` for a missing name."""
        store = self.make_store()
        store.save_preset("Keep", sample_filters(user="alice"))
        store.save_preset("Drop", sample_filters(user="bob"))
        store.set_last_used("Drop")

        self.assertTrue(store.delete_preset("Drop"))
        self.assertFalse(store.delete_preset("Drop"))
        self.assertFalse(store.delete_preset(""))
        self.assertEqual(store.names(), ["Keep"])
        self.assertIsNone(store.get_last_used())
        self.assertEqual(
            [item["name"] for item in self.read_document()["presets"]], ["Keep"]
        )

    def test_last_used_only_accepts_existing_names(self) -> None:
        """``last_used`` persists existing names and clears everything else."""
        store = self.make_store()
        store.save_preset("A", sample_filters(user="alice"))
        store.save_preset("B", sample_filters(user="bob"))
        self.assertIsNone(store.get_last_used())

        store.set_last_used("A")
        self.assertEqual(store.get_last_used(), "A")
        self.assertEqual(self.make_store().get_last_used(), "A")
        self.assertEqual(self.read_document()["last_used"], "A")

        store.set_last_used("missing")
        self.assertIsNone(store.get_last_used())
        store.set_last_used("B")
        store.set_last_used(None)
        self.assertIsNone(store.get_last_used())
        self.assertIsNone(self.read_document()["last_used"])

    def test_unicode_names_round_trip(self) -> None:
        """Unicode names are stored as UTF-8, not escaped ASCII."""
        name = "Пресет №1 — отчёты"
        store = self.make_store()
        store.save_preset(name, sample_filters(user="Иванов"))
        raw = self.path.read_text(encoding="utf-8")
        self.assertIn(name, raw)

        reloaded = self.make_store()
        self.assertEqual(reloaded.names(), [name])
        self.assertEqual(reloaded.get(name)["filters"]["user"], "Иванов")


class TestCorruptDocuments(PresetsTestCase):
    """Recovery paths that must never crash the application."""

    def test_corrupt_entry_is_skipped(self) -> None:
        """One bad entry is dropped, valid entries load and the reason is kept."""
        self.write_document(
            {
                "schema_version": 1,
                "last_used": "Good",
                "presets": [
                    {
                        "name": "Good",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "updated_at": "2026-01-01T00:00:00+00:00",
                        "filters": {"user": "alice"},
                    },
                    {
                        "name": "Bad filters",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "updated_at": "2026-01-01T00:00:00+00:00",
                        "filters": "not-an-object",
                    },
                    "not-an-object",
                ],
            }
        )
        store = self.make_store()
        loaded = store.load()
        self.assertEqual([item["name"] for item in loaded], ["Good"])
        self.assertEqual(store.get_last_used(), "Good")
        self.assertIsNotNone(store.last_error)
        self.assertIn("Skipped 2 invalid preset(s)", store.last_error)
        self.assertEqual(store.get("Good")["filters"]["user"], "alice")

    def test_invalid_json_is_reported(self) -> None:
        """Invalid JSON yields an empty store with an explanatory error."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{ this is not json", encoding="utf-8")
        store = self.make_store()
        self.assertEqual(store.load(), [])
        self.assertEqual(store.names(), [])
        self.assertIsNotNone(store.last_error)
        self.assertIn("not valid JSON", store.last_error)

    def test_invalid_schema_version_is_reported(self) -> None:
        """A missing or non-integer ``schema_version`` invalidates the file."""
        documents = (
            {"presets": []},
            {"schema_version": "1", "presets": []},
            {"schema_version": True, "presets": []},
        )
        for document in documents:
            with self.subTest(document=document):
                self.write_document(document)
                store = self.make_store()
                self.assertEqual(store.load(), [])
                self.assertIsNotNone(store.last_error)
                self.assertIn("schema_version", store.last_error)

    def test_oversized_file_is_refused(self) -> None:
        """Files above one MiB are ignored and archived on the next save."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"{" + b" " * MAX_FILE_BYTES + b"}")
        store = self.make_store()
        self.assertEqual(store.load(), [])
        self.assertIsNotNone(store.last_error)
        self.assertIn("too large", store.last_error)

        store.save_preset("Fresh", sample_filters(user="alice"))
        self.assertEqual(store.names(), ["Fresh"])
        self.assertEqual(len(self.archives()), 1)
        self.assertIn("Fresh", [item["name"] for item in self.read_document()["presets"]])

    def test_too_many_presets_raises(self) -> None:
        """A document above the preset limit raises but keeps an explanation."""
        entries = [
            {
                "name": f"Preset {index}",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "filters": {"user": "alice"},
            }
            for index in range(MAX_PRESETS + 1)
        ]
        self.write_document(
            {"schema_version": 1, "last_used": None, "presets": entries}
        )
        store = self.make_store()
        with self.assertRaises(PresetsValidationError):
            store.load()
        self.assertIn("more than the limit", store.last_error)

    def test_newer_schema_is_read_only(self) -> None:
        """A future schema loads nothing and blocks writes until reset."""
        self.write_document({"schema_version": 2, "last_used": None, "presets": []})
        store = self.make_store()
        self.assertEqual(store.load(), [])
        self.assertIn("newer version", store.last_error)

        with self.assertRaises(PresetsValidationError):
            store.save_preset("Blocked", sample_filters(user="alice"))
        with self.assertRaises(PresetsValidationError):
            store.delete_preset("Blocked")
        with self.assertRaises(PresetsValidationError):
            store.set_last_used(None)

        store.reset()
        self.assertIsNone(store.last_error)
        self.assertEqual(store.load(), [])
        store.save_preset("Allowed", sample_filters(user="alice"))
        self.assertEqual(store.names(), ["Allowed"])


class TestPersistence(PresetsTestCase):
    """Atomic writes, archive pruning, reset and unwritable locations."""

    def test_atomicity_when_json_dump_fails(self) -> None:
        """A serialization failure leaves the old file byte-for-byte intact."""
        store = self.make_store()
        store.save_preset("Keep", sample_filters(user="keep"))
        before = self.path.read_bytes()

        with mock.patch("presets.json.dump", side_effect=RuntimeError("boom")):
            with self.assertRaises(PresetsStoreError):
                store.save_preset("New", sample_filters(user="new"))

        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([item.name for item in self.path.parent.iterdir()], ["presets.json"])
        self.assertEqual(self.make_store().names(), ["Keep"])

    def test_atomicity_when_replace_fails(self) -> None:
        """A failed ``os.replace`` also preserves the previous document."""
        store = self.make_store()
        store.save_preset("Keep", sample_filters(user="keep"))
        before = self.path.read_bytes()

        with mock.patch(
            "presets.os.replace", side_effect=OSError("replace failed")
        ):
            with self.assertRaises(PresetsStoreError):
                store.save_preset("New", sample_filters(user="new"))

        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual([item.name for item in self.path.parent.iterdir()], ["presets.json"])
        self.assertEqual(self.make_store().names(), ["Keep"])

    def test_archives_are_capped_at_three(self) -> None:
        """Repeated recovery archives keep only the three newest copies."""
        store = self.make_store()
        store.save_preset("Keep", sample_filters(user="alice"))
        for _ in range(5):
            store.reset()
        archives = self.archives()
        self.assertEqual(len(archives), 3)

    def test_reset_writes_empty_document(self) -> None:
        """``reset`` archives the current file and writes a valid empty store."""
        store = self.make_store()
        store.save_preset("Keep", sample_filters(user="alice"))
        store.set_last_used("Keep")
        store.reset()

        self.assertEqual(
            self.read_document(),
            {"schema_version": 1, "last_used": None, "presets": []},
        )
        self.assertEqual(store.load(), [])
        self.assertIsNone(store.get_last_used())
        self.assertIsNone(store.last_error)
        self.assertEqual(len(self.archives()), 1)

    def test_unwritable_directory_raises_store_error(self) -> None:
        """A read-only/blocked location raises a clear error but still loads."""
        blocker = self.root / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        store = PresetStore(blocker / "presets.json")

        with self.assertRaises(PresetsStoreError) as raised:
            store.save_preset("Blocked", sample_filters(user="alice"))
        self.assertIn("presets", str(raised.exception).lower())
        # The GUI can continue without presets: reading still returns empty.
        self.assertEqual(store.names(), [])

    def test_module_has_no_gui_or_adapter_imports(self) -> None:
        """The module stays headless: no tkinter and no logt_adapter imports."""
        source = Path(presets.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])

        self.assertNotIn("tkinter", imported)
        self.assertNotIn("logt_adapter", imported)
        self.assertNotIn("ics_log_analyzer", imported)


if __name__ == "__main__":
    unittest.main()
