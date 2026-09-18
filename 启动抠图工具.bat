@echo off
title 批量抠图工具
cd /d "%~dp0"
echo 正在启动批量抠图工具，请稍候...
"runtime\python\python.exe" server.py
echo.
echo 服务已退出。
pause
