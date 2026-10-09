@echo off
chcp 936 >nul
title 摸鱼bot 配置中心
cd /d "%~dp0"

echo.
echo   摸鱼bot 配置中心
echo   =============================================
echo.

rem ── 1) 找 Python（优先 PATH 里的 python，其次 py 启动器）─────
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY (
  where py >nul 2>&1 && set "PY=py"
)
if not defined PY goto nopython

rem ── 2) 缺依赖就装；装过就直接跳过 ───────────────────────────
%PY% -c "import botpy, dotenv, markdown, PIL" >nul 2>&1
if errorlevel 1 (
  echo   [1/2] 第一次运行，正在安装依赖（大约 1 分钟，请耐心等）...
  echo.
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 goto pipfail
  echo.
  echo   依赖装好了。
) else (
  echo   [1/2] 依赖已就绪。
)
echo.

rem ── 3) 起配置中心 ───────────────────────────────────────────
echo   [2/2] 正在打开配置页。
echo         要停止：在本窗口按 Ctrl+C，或点配置页上的「关闭配置中心」。
echo.
%PY% config-web.py

rem Python 启动时会把控制台代码页切到 UTF-8，这里切回来，
rem 否则下面几行的中文会花屏。
chcp 936 >nul
echo.
echo   配置中心已退出。
pause
exit /b 0

:nopython
echo   [X] 没找到 Python。
echo.
echo   请先安装 Python（免费）：https://www.python.org/downloads/
echo.
echo   安装时务必勾选「Add python.exe to PATH」这一项，
echo   装完后关掉本窗口、重新双击本文件即可。
echo.
pause
exit /b 1

:pipfail
echo.
echo   [X] 依赖安装失败。常见原因：网络不通，或需要代理。
echo.
echo   可以手动打开「命令提示符」跑：pip install -r requirements.txt
echo.
pause
exit /b 1
