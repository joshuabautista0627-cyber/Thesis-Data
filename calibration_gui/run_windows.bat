@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo The virtual environment is missing. Run setup_windows.bat first.
    exit /b 1
)
set QT_ENABLE_HIGHDPI_SCALING=1
".venv\Scripts\python.exe" app.py %*
endlocal

