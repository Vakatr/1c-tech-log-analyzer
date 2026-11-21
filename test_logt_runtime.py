"""Preflight tests for running LogT from a frozen build (P1-00c, BUG-006).

In a PyInstaller one-folder build ``sys.executable`` is ``1C_Log_Analyzer.exe``
rather than a Python interpreter, and ``vendor/logt`` is not part of the bundle.
The adapter therefore resolves an external interpreter (``LOGT_PYTHON``, then
``PATH``) and the GUI calls :func:`logt_adapter.check_logt_runtime` before it
creates an import thread.

These tests are fully offline: no LogT checkout, ClickHouse server or real
child process is required.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import logt_adapter


class _EmptyPipe:
    """File-like stub that reports immediate EOF."""

    def readline(self):
        return ""

    def read(self):
        return ""

    def close(self):
        pass


class RuntimeTestCase(unittest.TestCase):
    """Base class that clears the LogT-related environment for every test."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("LOGT_PYTHON", "LOGT_ROOT"):
            os.environ.pop(name, None)

    def frozen(self):
        """Patch ``sys.frozen`` so the adapter takes its packaged-build path."""
        return mock.patch.object(sys, "frozen", True, create=True)


class TestResolveLogtPython(RuntimeTestCase):
    """Interpreter resolution: unchanged in source, explicit when frozen."""

    def test_source_checkout_uses_sys_executable(self):
        with mock.patch.object(sys, "frozen", False, create=True):
            self.assertEqual(logt_adapter.resolve_logt_python(), sys.executable)

    def test_frozen_uses_existing_logt_python_file(self):
        with tempfile.TemporaryDirectory() as scratch:
            interpreter = Path(scratch) / "python.exe"
            interpreter.write_text("", encoding="utf-8")
            os.environ[logt_adapter.ENV_LOGT_PYTHON] = str(interpreter)
            with self.frozen():
                self.assertEqual(
                    logt_adapter.resolve_logt_python(), str(interpreter)
                )

    def test_frozen_resolves_logt_python_command_through_path(self):
        os.environ[logt_adapter.ENV_LOGT_PYTHON] = "my-python"
        with self.frozen():
            with mock.patch.object(
                logt_adapter.shutil,
                "which",
                side_effect=lambda name: "/opt/my-python/bin/python" if name == "my-python" else None,
            ) as which:
                self.assertEqual(
                    logt_adapter.resolve_logt_python(), "/opt/my-python/bin/python"
                )
        which.assert_called_once_with("my-python")

    def test_frozen_falls_back_to_python_on_path(self):
        with self.frozen():
            with mock.patch.object(
                logt_adapter.shutil,
                "which",
                side_effect=lambda name: "/usr/bin/python" if name == "python" else None,
            ):
                self.assertEqual(logt_adapter.resolve_logt_python(), "/usr/bin/python")

    def test_frozen_without_any_interpreter_raises_actionable_error(self):
        with self.frozen():
            with mock.patch.object(logt_adapter.shutil, "which", return_value=None):
                with self.assertRaises(logt_adapter.AdapterError) as caught:
                    logt_adapter.resolve_logt_python()
        message = str(caught.exception)
        self.assertIn("LOGT_PYTHON", message)
        self.assertIn("LOGT_ROOT", message)
        self.assertIn("3.11", message)


class TestCheckLogtRuntime(RuntimeTestCase):
    """The public preflight used by the GUI."""

    def test_missing_sources_in_frozen_build_mention_logt_root(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.frozen():
                with mock.patch.object(logt_adapter, "REPO_ROOT", Path(empty)):
                    with self.assertRaises(logt_adapter.AdapterError) as caught:
                        logt_adapter.check_logt_runtime()
        message = str(caught.exception)
        self.assertIn("LOGT_ROOT", message)
        self.assertIn("vendor/logt", message)

    def test_returns_interpreter_when_sources_and_python_exist(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / "logt"
            (root / "src").mkdir(parents=True)
            (root / "src" / "main.py").write_text("# stub\n", encoding="utf-8")
            os.environ["LOGT_ROOT"] = str(root)
            os.environ[logt_adapter.ENV_LOGT_PYTHON] = sys.executable
            with self.frozen():
                self.assertEqual(
                    logt_adapter.check_logt_runtime(), sys.executable
                )

    def test_missing_python_is_reported_after_sources_are_found(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / "logt"
            (root / "src").mkdir(parents=True)
            (root / "src" / "main.py").write_text("# stub\n", encoding="utf-8")
            os.environ["LOGT_ROOT"] = str(root)
            with self.frozen():
                with mock.patch.object(logt_adapter.shutil, "which", return_value=None):
                    with self.assertRaises(logt_adapter.AdapterError) as caught:
                        logt_adapter.check_logt_runtime()
        self.assertIn("LOGT_PYTHON", str(caught.exception))


class TestFrozenImportArgv(RuntimeTestCase):
    """``run_logt_import`` must not launch the GUI binary as the child."""

    def test_frozen_import_forwards_resolved_interpreter(self):
        captured = {}

        class FakeProcess:
            def __init__(self, argv, **kwargs):
                captured["argv"] = list(argv)
                captured["kwargs"] = kwargs
                self.returncode = 0
                self.stdout = _EmptyPipe()
                self.stderr = _EmptyPipe()

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

            def terminate(self):
                pass

            def kill(self):
                pass

        with tempfile.TemporaryDirectory() as scratch:
            entry = Path(scratch) / "main.py"
            entry.write_text("# stub\n", encoding="utf-8")
            work_dir = Path(scratch) / "work"
            work_dir.mkdir()
            environ = {"LOGT_WORK_DIR": str(work_dir)}
            with mock.patch.dict(os.environ, environ, clear=False):
                with self.frozen():
                    with mock.patch.object(
                        logt_adapter,
                        "resolve_logt_python",
                        return_value="C:/Python311/python.exe",
                    ) as resolver:
                        with mock.patch.object(
                            logt_adapter,
                            "_resolve_logt_entrypoint",
                            return_value=entry,
                        ):
                            with mock.patch.object(
                                logt_adapter.subprocess, "Popen", FakeProcess
                            ):
                                with mock.patch.object(
                                    logt_adapter,
                                    "_write_private_config",
                                    side_effect=lambda directory, payload: directory
                                    / "config.json",
                                ):
                                    with mock.patch.object(
                                        logt_adapter,
                                        "count_tables",
                                        return_value={"events": 7},
                                    ):
                                        result = logt_adapter.run_logt_import(
                                            ".", {"database": "tracelog"}, dataset_name="ds"
                                        )

        resolver.assert_called_once_with()
        self.assertEqual(captured["argv"][0], "C:/Python311/python.exe")
        self.assertEqual(captured["argv"][1], "-u")
        self.assertTrue(captured["argv"][2].endswith("main.py"))
        self.assertTrue(result.success)
        self.assertEqual(result.rows_loaded, 7)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
