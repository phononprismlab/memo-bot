@echo off
chcp 936 >nul
title memo-bot 脑内抽屉
cd /d "%~dp0"

echo.
echo   脑内抽屉
echo   =============================================
echo.

rem 找 Python：先 PATH 里的 python，再 py 启动器
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (
  where py >nul 2>&1 && set "PY=py"
)
if not defined PY goto nopython

echo   正在起服务，浏览器会自动打开（默认 http://127.0.0.1:8765 ）。
echo   关掉浏览器不会停服务；要停就在这个窗口按 Ctrl+C，
echo   或点页面顶部的「停止服务」。
echo.
%PY% brain-drawer.py --serve

rem Python 退出时会把控制台切回 UTF-8，不改回来后面几行中文会花屏
chcp 936 >nul
echo.
echo   脑内抽屉已退出。
pause
exit /b 0

:nopython
echo   [X] 没找到 Python。
echo.
echo   先装 Python 吧：https://www.python.org/downloads/
echo   安装时务必勾选「Add python.exe to PATH」，
echo   装完回来重新双击这个文件就行。
echo.
pause
exit /b 1
