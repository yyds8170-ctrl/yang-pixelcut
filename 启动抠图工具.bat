@echo off
title 批量抠图工具（洋）
cd /d "%~dp0"

rem ===== 高性能电源模式：RMBG-3.0 推理提速约 2 倍 =====
rem 需要管理员权限；未提权时自动跳过，不影响使用
powercfg /setactive SCHEME_MIN >nul 2>&1
if %errorlevel%==0 (
  echo [优化] 已切换到高性能电源模式
) else (
  echo [提示] 想要更快速度，可右键"以管理员身份运行"本工具
)

echo 正在启动批量抠图工具，请稍候...
"runtime\python\python.exe" server.py
echo.
echo 程序已退出
pause