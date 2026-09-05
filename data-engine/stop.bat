@echo off
chcp 65001 >nul
cd /d %~dp0

REM 停止路径唯一化：全部委托 launcher 的安全停止（PID 文件 + 进程身份校验，
REM 宁不杀不错杀）——此前 netstat/taskkill 裸杀 + wmic（Win11 23H2+ 已不预装）
REM 是第二套无校验实现
python agent\management\launcher.py stop
if errorlevel 1 (
    echo.
    echo   [提示] 系统 Python 不可用或未启动过——若用过 pythonw 启动，请用开始菜单的
    echo   "SuperAIDB" 快捷方式或托盘图标退出。
)

if /i not "%1"=="/silent" pause
