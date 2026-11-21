# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification for 1C Log Analyzer.

This file is the single source of truth for the build: ``build.bat`` only
installs PyInstaller and invokes it. The spec used to be dead weight -- the
batch file passed command-line flags instead -- so a stale spec never broke
anything and the omitted driver went unnoticed.

Three details matter, and all of them are asserted by ``test_build_spec.py``:

* ``clickhouse_driver`` is imported **lazily** inside
  ``logt_adapter._import_driver``. PyInstaller follows only ordinary imports,
  so without an explicit ``hiddenimports`` entry the module is missing from the
  bundle and every ClickHouse call in the frozen app fails with
  "clickhouse-driver package is required".
* The driver ships compiled extensions (``.pyd``) and a data directory, so
  ``collect_all`` is used to pull in the package, its binaries and its
  submodules.

The same applies to ``openpyxl``: ``export.py`` imports it inside
``_import_openpyxl`` so CSV and JSON keep working without it, which means the
XLSX writer is invisible to PyInstaller unless it is collected here as well.
A build-time guard fails the build with an actionable message when the package
is missing, instead of shipping an ``.exe`` whose XLSX button raises
"pip install openpyxl".

Paths are anchored to ``SPECPATH`` because PyInstaller evaluates the spec with
the spec's own directory as the base, not the project root.
"""

import os
from importlib.util import find_spec

from PyInstaller.utils.hooks import collect_all

# Repository root: the spec lives at the top level of the checkout.
ROOT = os.path.abspath(SPECPATH)

# Fail early and clearly instead of producing a build without XLSX support.
# ``export.py`` imports openpyxl lazily, so PyInstaller cannot detect it.
if find_spec("openpyxl") is None:
    raise SystemExit(
        "openpyxl is not installed: the build would ship without XLSX export.\n"
        "Run: pip install -r requirements.txt"
    )

#: ``clickhouse_driver`` is imported lazily, so it must be declared by hand.
#: ``tzlocal`` is a hard dependency of the driver; ``pytz`` and ``numpy`` are
#: used by its column code. The tkinter submodules are imported indirectly.
#: ``openpyxl`` is imported lazily by ``export.py`` and is collected below.
HIDDEN_IMPORTS = [
    "clickhouse_driver",
    "clickhouse_driver.client",
    "clickhouse_driver.connection",
    "clickhouse_driver.dbapi",
    "clickhouse_driver.numpy",
    "openpyxl",
    "tzlocal",
    "pytz",
    "numpy",
    "ctypes",
    "ctypes.wintypes",
    "tkinter",
    "tkinter.ttk",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "tkinter.simpledialog",
]

# Pull in the whole driver package: pure modules, compiled extensions and data.
driver_datas, driver_binaries, driver_hiddenimports = collect_all("clickhouse_driver")

# ``openpyxl`` is pure Python but large and has submodules (styles, cell,
# writer) that a lazy import never reveals to the analyser.
openpyxl_datas, openpyxl_binaries, openpyxl_hiddenimports = collect_all("openpyxl")

a = Analysis(
    [os.path.join(ROOT, "ics_log_analyzer.py")],
    pathex=[ROOT],
    binaries=driver_binaries + openpyxl_binaries,
    datas=[
        (os.path.join(ROOT, "docs", "events.json"), "docs"),
        (os.path.join(ROOT, "docs", "events.md"), "docs"),
    ]
    + driver_datas
    + openpyxl_datas,
    hiddenimports=HIDDEN_IMPORTS + driver_hiddenimports + openpyxl_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="1C_Log_Analyzer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="1C_Log_Analyzer",
)
