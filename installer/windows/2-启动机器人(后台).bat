@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist "%~dp0bot.env" goto :no_env

call "%~dp0installer\find_python.bat"
if errorlevel 1 goto :fail

"%BOT_PY%" "%~dp0bot_runner.py" start-bg
if errorlevel 1 goto :fail

echo.
"%BOT_PY%" "%~dp0bot_runner.py" webui open
echo.
pause
exit /b 0

:no_env
echo.
echo [提示] 未找到配置文件 bot.env！
echo 请先双击运行「1-一键安装与配置.bat」完成初始化配置。
echo.
pause
exit /b 1

:fail
echo.
echo [提示] 启动未成功，可双击「6-环境体检(Doctor).bat」排查原因。
echo.
pause
exit /b 1
