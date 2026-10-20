@echo off
rem Windows launcher for the Polymarket 1X2 collector.
rem Double-click this file to start; press Ctrl+C to stop and save the CSV.
rem Keep this file ASCII-only: cmd.exe mis-parses non-ASCII batch files.
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "PROJECT_DIR=%~dp0"
pushd "%PROJECT_DIR%"
if errorlevel 1 (
  echo [ERROR] Cannot enter project directory: %PROJECT_DIR%
  pause
  exit /b 1
)

set "VENV_PY=%PROJECT_DIR%.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
  echo [ERROR] Python virtual environment not found:
  echo         %PROJECT_DIR%.venv
  echo.
  echo Create it once from this folder:
  echo     py -3 -m venv .venv
  echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
  echo.
  pause
  popd
  exit /b 1
)

echo Starting Polymarket EPL / La Liga / Bundesliga odds collector.
echo Press Ctrl+C to stop. CSV files are saved in:
echo     %PROJECT_DIR%
echo.

"%VENV_PY%" "%PROJECT_DIR%polymarket_epl_odds.py" --output polymarket_odds.csv
set "status=%ERRORLEVEL%"
echo.
if "%status%"=="0" (
  echo Collector finished.
) else (
  echo Collector exited with code %status%.
)
echo.
pause
popd
exit /b %status%
