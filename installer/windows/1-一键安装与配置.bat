@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist "%~dp0installer\install.ps1" goto :missing_script

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\install.ps1"
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%

:missing_script
echo [Error] Missing file: installer\install.ps1
echo Please extract the complete ZIP archive before running.
echo.
pause
exit /b 1
