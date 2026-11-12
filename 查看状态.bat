@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ================= Bot Status =================
if not exist bot.pid goto :nopid
set /p PID=<bot.pid
echo bot.pid = %PID%
echo ---------------------------------------------
tasklist /FI "PID eq %PID%" /NH | findstr /i "python"
if errorlevel 1 echo   ^>^> process NOT found : stale pid, bot is NOT running
if not errorlevel 1 echo   ^>^> bot is RUNNING
goto :logs
:nopid
echo   ^>^> bot.pid not found : bot is NOT running
:logs
echo.
echo ================= bot.log ====================
if exist bot.log (type bot.log) else (echo [none])
echo.
echo ================= error.log ==================
if exist error.log (type error.log) else (echo [none])
echo.
echo =============================================
pause
