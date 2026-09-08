@echo off
rem Score Assistant launcher: run the GUI with the project venv (no console window).
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo [score-assistant] venv not found. Run: uv sync
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m score_tool
