@echo off
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0set_trigger.ps1" -Mode chance
echo.
pause
