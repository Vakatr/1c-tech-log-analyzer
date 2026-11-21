"""Build-configuration tests: the compiled app must ship its lazy dependencies.

The adapter imports ``clickhouse_driver`` **lazily** inside
``_import_driver`` and ``export.py`` imports ``openpyxl`` lazily inside
``_import_openpyxl``. PyInstaller only follows ordinary imports, so without an
explicit ``hiddenimports`` entry the packages are missing from the bundle and
the ClickHouse operations or the XLSX export fail in the compiled build with
"package is required" -- while the source checkout keeps working, which makes
the bug invisible until a user runs the ``.exe``.

These tests read the spec, the build script, the requirements and the export
module as text and assert the contract between them. They need neither
PyInstaller nor a ClickHouse server.
"""

import ast
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
SPEC = REPO_ROOT / "1C_Log_Analyzer.spec"
BUILD = REPO_ROOT / "build.bat"
REQUIREMENTS = REPO_ROOT / "requirements.txt"
EXPORT = REPO_ROOT / "export.py"


def _spec_text() -> str:
    """Return the spec source.

    Returns:
        Contents of ``1C_Log_Analyzer.spec``.

    Raises:
        AssertionError: If the spec is missing.
    """
    if not SPEC.exists():
        raise AssertionError(f"{SPEC.name} not found; the build has no spec")
    return SPEC.read_text(encoding="utf-8")


class TestSpecDeclaresTheLazyDriver(unittest.TestCase):
    """P0: the lazily imported driver is declared as a hidden import."""

    def test_spec_exists_and_is_used_by_the_build_script(self):
        """The batch file must invoke the spec, not duplicate its options."""
        script = BUILD.read_text(encoding="utf-8")
        self.assertIn("1C_Log_Analyzer.spec", script)
        # The old script passed every option on the command line, so the spec
        # was dead weight and could silently drift out of date.
        self.assertNotIn("--add-data", script)
        self.assertNotIn("--onefile", script)

    def test_spec_declares_clickhouse_driver_hiddenimports(self):
        text = _spec_text()
        self.assertRegex(
            text,
            r"hiddenimports\s*=\s*HIDDEN_IMPORTS",
            msg="hiddenimports must be composed from HIDDEN_IMPORTS",
        )
        # Every name the adapter may import lazily must be listed verbatim.
        declared = set(re.findall(r'"([A-Za-z_][A-Za-z0-9_.]*)"', text))
        self.assertIn("clickhouse_driver", declared)
        self.assertIn("tzlocal", declared)
        self.assertIn("ctypes.wintypes", declared)
        self.assertIn("tkinter", declared)

    def test_spec_collects_driver_binaries(self):
        """Compiled extensions of the driver must be collected, not guessed."""
        text = _spec_text()
        self.assertIn("collect_all(", text)
        self.assertIn("clickhouse_driver", text)
        self.assertRegex(text, r"binaries\s*=\s*driver_binaries")

    def test_lazy_import_still_exists(self):
        """Guard against the spec fix being made unnecessary by mistake.

        If the adapter ever imports the driver at module level this test (and
        the reason for the hidden import) becomes obsolete -- which is a
        deliberate signal, not a silent failure.
        """
        source = (REPO_ROOT / "logt_adapter.py").read_text(encoding="utf-8")
        self.assertIn("def _import_driver", source)
        tree = ast.parse(source)
        module_level = [
            node
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            and any(
                (alias.name or "").startswith("clickhouse_driver")
                for alias in getattr(node, "names", [])
            )
        ]
        self.assertEqual(
            module_level,
            [],
            "driver is now imported at module level; the hidden import may be "
            "reducible, and logt_adapter no longer needs _require_driver",
        )


class TestSpecIsSyntacticallyValid(unittest.TestCase):
    """The spec is Python; a syntax error breaks the build with a poor message."""

    def test_spec_parses(self):
        ast.parse(_spec_text())

    def test_spec_has_no_absolute_developer_paths(self):
        """No machine-specific paths may be baked into the build."""
        text = _spec_text()
        self.assertNotRegex(
            text,
            r"[A-Za-z]:\\\\Users\\\\",
            msg="spec contains an absolute developer path",
        )


class TestXlsxDependencyIsShipped(unittest.TestCase):
    """P2: the lazily imported XLSX writer must reach the frozen application."""

    def test_requirements_pin_openpyxl_and_et_xmlfile(self):
        """Both packages are reproducibly pinned, not floating."""
        text = REQUIREMENTS.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^openpyxl==3\.1\.5\s*$")
        self.assertRegex(text, r"(?m)^et-xmlfile==2\.0\.0\s*$")

    def test_spec_collects_openpyxl(self):
        """The package, its submodules and data are collected like the driver."""
        text = _spec_text()
        declared = set(re.findall(r'"([A-Za-z_][A-Za-z0-9_.]*)"', text))
        self.assertIn("openpyxl", declared)
        self.assertRegex(text, r'collect_all\(\s*"openpyxl"\s*\)')
        self.assertRegex(text, r"hiddenimports\s*=\s*HIDDEN_IMPORTS")
        self.assertRegex(
            text, r"binaries\s*=\s*driver_binaries\s*\+\s*openpyxl_binaries"
        )
        self.assertRegex(
            text, r"hiddenimports\s*=\s*HIDDEN_IMPORTS\s*\+\s*driver_hiddenimports"
        )

    def test_spec_fails_the_build_without_openpyxl(self):
        """A missing package must break the build, not the shipped ``.exe``."""
        text = _spec_text()
        self.assertRegex(text, r'find_spec\(\s*"openpyxl"\s*\)')
        self.assertRegex(text, r"raise\s+SystemExit")
        self.assertIn("pip install -r requirements.txt", text)
        # The guard has to run before Analysis starts collecting modules.
        self.assertLess(
            text.index('find_spec("openpyxl")'),
            text.index("Analysis("),
            "the openpyxl guard must run before Analysis",
        )

    def test_build_script_checks_both_lazy_dependencies(self):
        """The batch file installs requirements when either import fails."""
        script = BUILD.read_text(encoding="utf-8")
        self.assertIn("import clickhouse_driver, openpyxl", script)

    def test_export_imports_openpyxl_lazily(self):
        """The helper must exist, and no module-level import may shadow it."""
        source = EXPORT.read_text(encoding="utf-8")
        self.assertIn("def _import_openpyxl", source)
        tree = ast.parse(source)
        module_level = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                module_level.extend(
                    alias.name
                    for alias in node.names
                    if (alias.name or "").startswith("openpyxl")
                )
            elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "openpyxl"
            ):
                module_level.append(node.module)
        self.assertEqual(
            module_level,
            [],
            "openpyxl is imported at module level; the hidden import would be "
            "detected automatically, but CSV/JSON would stop working without it",
        )

    def test_export_module_is_headless(self):
        """Serialisation must stay usable from a worker thread without a display."""
        source = EXPORT.read_text(encoding="utf-8")
        self.assertNotIn("tkinter", source.lower())
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("tkinter", imported)


if __name__ == "__main__":
    unittest.main()
