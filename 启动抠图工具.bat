@echo off
chcp 936 >nul
set PYTHONIOENCODING=gbk
set PYTHONUTF8=0
title 批量抠图工具（洋）
cd /d "%~dp0"

powercfg /setactive SCHEME_MIN >nul 2>&1

echo 正在启动批量抠图工具（洋），请稍候...
echo 提示：想更快就关闭本窗口，右键“以管理员身份运行”本文件
echo        RMBG-3.0 可提速约 2 倍（128 秒 -^> 67~71 秒）
echo.

"runtime\python\python.exe" server.py

echo.
echo 程序已退出
pause
