@echo off
chcp 65001 >nul
cd /d %~dp0

echo ============================================
echo  SuperAIDB - Startup (Silent Mode)
echo ============================================
echo.

REM Stop old services first (silent: skip pause)
call stop.bat /silent >nul 2>&1
timeout /t 1 >nul

echo [1/2] Starting backend (silent)...
REM launcher.py starts Management API + Frontend automatically
start /b "" pythonw.exe agent/management/launcher.py

echo     Waiting for backend...
set retries=0
:wait_backend
timeout /t 2 >nul
powershell -Command "try { Invoke-WebRequest 'http://localhost:2025/api/health' -UseBasicParsing -TimeoutSec 5 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 goto backend_ok
set /a retries+=1
if %retries% GEQ 30 (
    echo  [ERROR] Backend startup timeout. Check backend.log and logs\*.log
    type backend.log 2>nul | findstr /C:"error" /C:"fail" 2>nul
    pause
    exit /b 1
)
goto wait_backend
:backend_ok
echo     Backend ready.
echo       Management API:    http://localhost:2025
echo.

echo [2/2] Waiting for frontend (display board)...
set retries=0
:wait_frontend
timeout /t 2 >nul
powershell -Command "try { Invoke-WebRequest 'http://localhost:3000' -UseBasicParsing -TimeoutSec 5 | Out-Null; exit 0 } catch { exit 1 }" >nul 2>&1
if not errorlevel 1 goto frontend_ok
set /a retries+=1
if %retries% GEQ 30 (
    echo  [WARNING] Frontend startup timeout. Open http://localhost:3000 manually.
    goto done
)
goto wait_frontend
:frontend_ok
echo     Frontend ready.
start http://localhost:3000

:done
echo.
echo ============================================
echo  SuperAIDB started (silent mode)
echo  - Display board: http://localhost:3000
echo  - API docs:      http://localhost:2025/docs
echo ============================================
echo.
echo Backend runs in background (no console window)
echo View logs: type backend.log
echo Stop services: double-click stop.bat
echo.
echo Press any key to close this window (backend keeps running)...
pause >nul
