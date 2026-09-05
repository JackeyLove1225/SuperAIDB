@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title SuperAIDB Console
cd /d %~dp0

echo.
echo  ============================================
echo    SuperAIDB - Startup (Console Mode)
echo  ============================================
echo.

REM 清理与停止的唯一实现都在 launcher（PID 文件 + 进程身份校验，宁不杀不错杀）——
REM 此前 netstat/taskkill 裸杀端口是第二套无校验实现
echo  [1/3] 残留清理由启动器接管（身份校验，不误杀无关进程）...

REM Step 2: Start backend via launcher (console mode, includes tray monitor)
echo.
echo  [2/3] Starting backend (Management API + Frontend)...
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
python agent/management/launcher.py

REM Launcher returned: stop services via the same safe path
echo.
echo  Stopping services...
python agent/management/launcher.py stop
echo  All services stopped.
exit
