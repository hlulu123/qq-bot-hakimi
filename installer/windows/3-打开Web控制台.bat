@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

call "%~dp0installer\find_python.bat"
if errorlevel 1 goto :fail

"%BOT_PY%" "%~dp0bot_runner.py" webui open
if errorlevel 1 goto :fail

echo.
echo Web 控制台已打开（http://127.0.0.1:8765）。
timeout /t 3 >nul
exit /b 0

:fail
echo.
pause
exit /b 1
