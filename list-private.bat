@echo off
rem List recent private chats and check them against private_chats in config.json.
rem Output goes to list-private.txt (UTF-8 with BOM, readable in Notepad).
setlocal
cd /d "%~dp0"
if not exist "config.json" (
    echo [FAIL] config.json not found in "%~dp0"
    pause
    exit /b 1
)
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0list_private.py" "%~dp0list-private.txt"
echo.
echo ---- finished ----
pause
