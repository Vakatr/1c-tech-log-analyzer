@echo off
REM Build standalone Windows executable for 1C Log Analyzer.
REM
REM The build is driven by 1C_Log_Analyzer.spec -- keep build options there, not
REM here, so there is only one place to edit. In particular the spec declares
REM clickhouse_driver and openpyxl as hidden imports: both are imported lazily,
REM so PyInstaller cannot discover them on its own and the ClickHouse tabs or
REM the XLSX export would fail in the compiled build with "package is
REM required".
REM
REM Output: dist\1C_Log_Analyzer\1C_Log_Analyzer.exe (one-folder build).
setlocal
cd /d "%~dp0"

echo [1/4] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python was not found on PATH.
    exit /b 1
)

echo [2/4] Installing build requirements if missing...
python -m pip show pyinstaller >nul 2>&1 || python -m pip install pyinstaller
REM Both packages must be importable at build time so the spec can collect
REM them: the driver for ClickHouse tabs, openpyxl for XLSX export.
python -c "import clickhouse_driver, openpyxl" >nul 2>&1 || python -m pip install -r requirements.txt

echo [3/4] Cleaning previous build...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [4/4] Building from 1C_Log_Analyzer.spec...
python -m PyInstaller --noconfirm --clean 1C_Log_Analyzer.spec
if errorlevel 1 (
    echo ERROR: Build failed.
    exit /b 1
)

echo.
echo Done. Output: dist\1C_Log_Analyzer\1C_Log_Analyzer.exe
endlocal
