@echo off
cd /d "%~dp0"
set "TARGET="
for %%f in ("*.ps1") do (
  if /i not "%%~nxf"=="manager.ps1" set "TARGET=%%~nxf"
)
if not defined TARGET (
  echo [ERROR] No .ps1 found in this folder.
  pause
  exit /b 1
)
echo Launching: %TARGET%
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -NoExit -File "%TARGET%"
