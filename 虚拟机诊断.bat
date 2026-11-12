@echo off
chcp 65001 >nul
cd /d "%~dp0"
cls
echo ================ 1. Host / Session ================
hostname
echo SESSIONNAME=%SESSIONNAME%
echo.
echo ================ 2. Base Python ===================
if not exist "D:\Python311\python.exe" echo [MISSING] D:\Python311\python.exe
if exist "D:\Python311\python.exe" "D:\Python311\python.exe" --version
echo.
echo ================ 3. venv ==========================
if not exist ".venv\Scripts\python.exe" echo [MISSING] .venv\Scripts\python.exe
if exist ".venv\Scripts\python.exe" echo [OK] .venv launcher present
if exist ".venv\Scripts\python.exe" ".venv\Scripts\python.exe" -c "import sys;print('[venv RUNS]',sys.version.split()[0]);print('[exe]',sys.executable)"
echo.
echo ================ 4. secret.txt / DPAPI =============
if not exist secret.txt echo [MISSING] secret.txt
if exist secret.txt if exist ".venv\Scripts\python.exe" ".venv\Scripts\python.exe" -c "import win32crypt;d=bytes.fromhex(open('secret.txt').read().strip());k=win32crypt.CryptUnprotectData(d,None,None,None,0)[1].decode('utf-16-le');print('[DPAPI OK] key length =',len(k))"
echo.
echo ================ 5. WeChat processes ===============
tasklist /FI "IMAGENAME eq weixin.exe" /NH | findstr /i weixin
if errorlevel 1 echo [NOT RUNNING] weixin.exe
tasklist /FI "IMAGENAME eq WeChat.exe" /NH | findstr /i wechat
if errorlevel 1 echo [NOT RUNNING] WeChat.exe
echo.
echo ================ 6. Desktop / GUI ==================
if exist ".venv\Scripts\python.exe" ".venv\Scripts\python.exe" -c "import ctypes;u=ctypes.windll.user32;print('[screen]',u.GetSystemMetrics(0),'x',u.GetSystemMetrics(1));print('[foreground hwnd]',u.GetForegroundWindow());print('[session id]',__import__('os').environ.get('SESSIONNAME'))"
echo.
echo ================ 7. bot state ======================
if exist bot.pid echo [PRESENT] bot.pid  ^<-- stale pid from another machine?
if not exist bot.pid echo [none] bot.pid
if exist bot.lock echo [PRESENT] bot.lock ^<-- stale lock from another machine?
if not exist bot.lock echo [none] bot.lock
echo.
echo ================ 8. error.log ======================
if exist error.log (type error.log) else (echo [none])
echo.
echo ================ 9. bot.log ========================
if exist bot.log (type bot.log) else (echo [none])
echo.
echo ===================================================
pause
