@echo off
rem Same as 启动后台.vbs but launched by explorer.exe so that the python
rem process is not a child of the current shell's job object.
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0manager.ps1" start
