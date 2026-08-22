@echo off

chcp 65001 >nul

title 创建桌面快捷方式

cd /d %~dp0



echo.

echo  ============================================

echo    创建桌面快捷方式（无窗口模式）

echo  ============================================

echo.



powershell -Command ^

  "$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source; " ^

  "if (-not $pythonw) { " ^

  "  $python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source; " ^

  "  if ($python) { $pythonw = $python -replace 'python\.exe$', 'pythonw.exe' } " ^

  "} " ^

  "if (-not $pythonw -or -not (Test-Path $pythonw)) { " ^

  "  Write-Output '  [错误] 未找到 pythonw.exe，请检查 Python 安装'; exit 1 " ^

  "} " ^

  "$ws = New-Object -ComObject WScript.Shell; " ^

  "$desktop = [Environment]::GetFolderPath('Desktop'); " ^

  "$lnk = $ws.CreateShortcut(\"$desktop\SuperAIDB.lnk\"); " ^

  "$lnk.TargetPath = $pythonw; " ^

  "$lnk.Arguments = '%~dp0agent\management\launcher.py'; " ^

  "$lnk.WorkingDirectory = '%~dp0'; " ^

  "$lnk.Description = 'SuperAIDB 智能数据助手'; " ^

  "$lnk.WindowStyle = 7; " ^

  "$lnk.IconLocation = '%~dp0assets\app-icon.ico'; " ^

  "$lnk.Save(); " ^

  "Write-Output '  桌面快捷方式已创建: SuperAIDB.lnk'; " ^

  "Write-Output \"  目标: $pythonw\"; " ^

  "Write-Output '  参数: agent\management\launcher.py'"



echo.

echo  使用方法:

echo    双击桌面 "SuperAIDB" 图标即可启动系统

echo    - 完全无黑窗口弹出

echo    - 浏览器自动打开 http://localhost:3000

echo    - 后端在后台运行（无窗口）

echo.

echo    停止服务: 前端控制台点"停止后端" 或 双击 stop.bat

echo    查看日志: type backend.log

echo.

echo  按任意键退出...

pause >nul

exit

