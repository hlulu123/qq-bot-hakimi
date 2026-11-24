@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

if not exist "%~dp0bot.env" goto :no_env

call "%~dp0installer\find_python.bat"
if errorlevel 1 goto :fail

echo ==========================================
echo   QQ 机器人「哈基米」前台调试模式
echo   按 Ctrl+C 可随时停止
echo ==========================================
echo.
"%BOT_PY%" "%~dp0bot_runner.py" start
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
pause
exit /b 1
