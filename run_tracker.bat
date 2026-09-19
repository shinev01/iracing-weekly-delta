@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found: .venv\Scripts\python.exe
    echo Create it with:
    echo   python -m venv .venv
    echo   .venv\Scripts\python.exe -m pip install -r requirements.txt
    pause
    exit /b 1
)

echo Starting iRacing Weekly Tracker...
echo Open http://127.0.0.1:8000 in your browser.
echo.

".venv\Scripts\python.exe" main.py serve

if errorlevel 1 (
    echo.
    echo [ERROR] Tracker stopped with an error.
    pause
)

