@echo off
chcp 65001 >nul
set DST=C:\微信机器人
if not exist "%DST%" (
  echo 目标目录不存在：%DST%
  pause
  exit /b 1
)
echo 先停掉机器人和控制台（两个 exe 正在被占用时会复制失败）...
taskkill /F /IM 微信机器人.exe >nul 2>&1
taskkill /F /IM 机器人控制台.exe >nul 2>&1
timeout /t 2 /nobreak >nul
echo 复制两个 exe（约 10MB，_internal / _console 不用动）...
copy /Y "%~dp0微信机器人\微信机器人.exe" "%DST%\微信机器人.exe"
if errorlevel 1 goto fail
copy /Y "%~dp0微信机器人\机器人控制台.exe" "%DST%\机器人控制台.exe"
if errorlevel 1 goto fail
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0set_trigger.ps1" -Mode chance
echo.
echo 完成。打开控制台：
echo   1) 「配置 → 回复方式」可调概率和必回关键词，点「保存配置」
echo   2) 点「启动」或「重启」
pause
exit /b 0
:fail
echo.
echo 复制失败：请确认已关掉控制台和机器人，且没有安装窗口卡在复制中。
pause
exit /b 1
