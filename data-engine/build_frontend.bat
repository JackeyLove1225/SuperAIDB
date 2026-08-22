@echo off
chcp 65001 >nul
cd /d %~dp0

echo ============================================================
echo  SuperAIDB 前端生产构建
echo ============================================================
echo.
echo 此脚本会将前端编译为生产版本，构建完成后启动速度从 ~30s 降至 ~5s
echo.
echo 构建中...（首次约 30-60 秒，请耐心等待）
echo.

REM 查找前端目录（data-engine 的同级 agent-chat-ui）
set "FRONTEND_DIR=%~dp0..\agent-chat-ui"
if not exist "%FRONTEND_DIR%\package.json" (
    echo [错误] 未找到前端目录: %FRONTEND_DIR%
    pause
    exit /b 1
)

REM 检测包管理器
if exist "%FRONTEND_DIR%\pnpm-lock.yaml" (
    set "PKG_MGR=pnpm"
) else (
    set "PKG_MGR=npm"
)

echo 前端目录: %FRONTEND_DIR%
echo 包管理器: %PKG_MGR%
echo.

cd /d "%FRONTEND_DIR%"

if "%PKG_MGR%"=="pnpm" (
    call pnpm build
) else (
    call npm run build
)

if %ERRORLEVEL% neq 0 (
    echo.
    echo [错误] 构建失败！请检查上方的错误信息
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  构建成功！✅
echo ============================================================
echo.
echo 下次启动系统时，前端将使用生产模式 (next start)
echo 启动时间从 ~30s 降至 ~5s
echo.
echo 如需切换到开发模式 (热更新)，修改 config\.env 中：
echo   FRONTEND_DEV_MODE=true
echo.
pause
