@echo off
rem Keep this file pure ASCII: cmd.exe mangles GBK Chinese paths under chcp 65001.
rem All real work + Chinese messages live in update_program.ps1 (UTF-8 with BOM).
pushd "%~dp0" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_program.ps1"
set RC=%ERRORLEVEL%
popd >nul 2>&1
echo.
if not "%RC%"=="0" echo Update FAILED (exit code %RC%). Please screenshot this window.
pause
