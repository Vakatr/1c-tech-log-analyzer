"""Tests for :mod:`ics_events_registry` (DEBT-001 / BUG-003)."""

import json
import tempfile
import unittest
from pathlib import Path

import ics_events_registry as registry


VALID = {
    "source": "test",
    "events": [
        {
            "code": "EXCP",
            "title": "Исключение",
            "description": "Ошибка",
            "fields": [{"name": "Descr"}, {"name": "Context"}],
        },
        {
            "code": "CALL",
            "title": "Вызов",
            "fields": [{"name": "Duration"}],
        },
    ],
}


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self._original_path = registry._EVENTS_PATH
        registry._registry = None
        registry._load_error = None
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        registry._EVENTS_PATH = self._original_path
        registry._registry = None
        registry._load_error = None
        self._tmp.cleanup()

    def _point_to(self, name: str) -> Path:
        path = self.root / name
        registry._EVENTS_PATH = path
        return path

    def test_valid_registry_is_loaded(self):
        path = self._point_to("events.json")
        path.write_text(json.dumps(VALID, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(registry.load_registry()["events"], VALID["events"])
        self.assertIsNone(registry.registry_error())

    def test_missing_file_returns_empty_registry_and_records_error(self):
        self._point_to("missing.json")
        self.assertEqual(registry.load_registry(), {"events": []})
        self.assertIsNotNone(registry.registry_error())
        self.assertIn("missing.json", registry.registry_error())

    def test_corrupt_json_records_error(self):
        path = self._point_to("events.json")
        path.write_text("{not json", encoding="utf-8")
        self.assertEqual(registry.load_registry(), {"events": []})
        self.assertIsNotNone(registry.registry_error())

    def test_wrong_structure_records_error(self):
        path = self._point_to("events.json")
        path.write_text('{"events": "nope"}', encoding="utf-8")
        self.assertEqual(registry.load_registry(), {"events": []})
        self.assertIsNotNone(registry.registry_error())

    def test_registry_is_cached(self):
        path = self._point_to("events.json")
        path.write_text(json.dumps(VALID, ensure_ascii=False), encoding="utf-8")
        first = registry.load_registry()
        path.write_text('{"events": []}', encoding="utf-8")
        self.assertIs(registry.load_registry(), first)

    def test_force_reload_reads_new_content(self):
        path = self._point_to("events.json")
        path.write_text(json.dumps(VALID, ensure_ascii=False), encoding="utf-8")
        registry.load_registry()
        path.write_text('{"events": []}', encoding="utf-8")
        self.assertEqual(registry.load_registry(force_reload=True)["events"], [])

    def test_all_event_codes(self):
        path = self._point_to("events.json")
        path.write_text(json.dumps(VALID, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(registry.all_event_codes(), ["EXCP", "CALL"])

    def test_get_event(self):
        path = self._point_to("events.json")
        path.write_text(json.dumps(VALID, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(registry.get_event("EXCP")["title"], "Исключение")
        self.assertIsNone(registry.get_event("NOPE"))

    def test_known_field_names(self):
        path = self._point_to("events.json")
        path.write_text(json.dumps(VALID, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(
            registry.known_field_names(), {"Descr", "Context", "Duration"}
        )

    def test_known_field_names_on_missing_file_is_empty(self):
        self._point_to("missing.json")
        self.assertEqual(registry.known_field_names(), set())


if __name__ == "__main__":
    unittest.main()
