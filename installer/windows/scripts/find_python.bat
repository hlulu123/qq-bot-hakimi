@echo off
chcp 65001 >nul
setlocal EnableExtensions

set "_PROJ_DIR=%~1"
if not defined _PROJ_DIR (
  for %%I in ("%~dp0..") do set "_PROJ_DIR=%%~fI"
)
if "%_PROJ_DIR:~-1%"=="\" set "_PROJ_DIR=%_PROJ_DIR:~0,-1%"

set "_FOUND_PY="

call :try_py "%_PROJ_DIR%\.venv\Scripts\python.exe"
if defined _FOUND_PY goto :found

where py >nul 2>&1
if not errorlevel 1 (
  for /f "usebackq delims=" %%P in (`py -3 -c "import sys; print(sys.executable)" 2^>nul`) do (
    call :try_py "%%P"
    if defined _FOUND_PY goto :found
  )
)

for /f "usebackq delims=" %%P in (`where python 2^>nul`) do (
  call :try_py "%%P"
  if defined _FOUND_PY goto :found
)

for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "%ProgramFiles%\Python3*" "C:\Python3*") do (
  call :try_py "%%~fD\python.exe"
  if defined _FOUND_PY goto :found
)

goto :not_found

:try_py
set "_C=%~1"
if not defined _C exit /b 1
if not exist "%_C%" exit /b 1
echo "%_C%" | findstr /i /c:"WindowsApps" >nul
if not errorlevel 1 exit /b 1
"%_C%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>&1
if errorlevel 1 exit /b 1
set "_FOUND_PY=%_C%"
exit /b 0

:found
endlocal & set "BOT_PY=%_FOUND_PY%" & set "PYTHONUTF8=1" & set "PYTHONIOENCODING=utf-8"
exit /b 0

:not_found
echo.
echo [Error] Python 3.9+ was not found on this machine.
echo Please run "1-一键安装与配置.bat" first to install Python automatically.
echo.
endlocal
exit /b 1
