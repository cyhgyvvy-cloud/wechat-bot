@echo off
setlocal
set DST=C:\微信机器人
if not exist "%DST%" (
  echo 目标目录不存在：%DST%
  pause
  exit /b 1
)
echo 先停掉正在跑的机器人...
taskkill /F /IM 微信机器人.exe >nul 2>&1
timeout /t 2 /nobreak >nul
echo 复制 微信机器人.exe（约 8MB）...
copy /Y "%~dp0微信机器人\微信机器人.exe" "%DST%\微信机器人.exe"
if errorlevel 1 (
  echo.
  echo 复制失败：文件被占用。请先在控制台点「停止」再运行一次。
  pause
  exit /b 1
)
echo.
echo 完成。_internal 目录内容没变，不用更新。
echo 现在打开 控制台，点「启动」。
pause