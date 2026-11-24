@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

call "%~dp0installer\find_python.bat"
if errorlevel 1 goto :fail

"%BOT_PY%" "%~dp0bot_runner.py" doctor
echo.
pause
exit /b 0

:fail
echo.
pause
exit /b 1
