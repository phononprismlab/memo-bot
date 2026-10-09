@echo off
chcp 65001 >nul
title 摸鱼bot 配置中心
cd /d "%~dp0"

echo.
echo   摸鱼bot 配置中心
echo   ---------------------------------------------
echo   浏览器会自动打开一个配置页；在这个窗口按 Ctrl+C
echo   或在页面上点「关闭配置中心」即可停止。
echo.

python config-web.py

echo.
echo   配置中心已退出。
pause
