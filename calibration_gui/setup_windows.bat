@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py -3.11 -m venv .venv 2>nul || py -3 -m venv .venv
) else (
    where python >nul 2>nul || (
        echo Python 3.11 or later was not found. Install it from https://www.python.org/downloads/windows/
        exit /b 1
    )
    python -m venv .venv
)

call .venv\Scripts\activate.bat || exit /b 1
python -m pip install --upgrade pip || exit /b 1
python -m pip install -r requirements.txt || exit /b 1
echo Setup complete. Run run_windows.bat to start the application.
endlocal

