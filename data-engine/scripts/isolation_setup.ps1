# 系统级数据隔离（产品化三期）——enable/disable/status 三态幂等脚本
# 由管理端在 UAC 提权后调用：powershell -ExecutionPolicy Bypass -File isolation_setup.ps1 -Mode enable
#
# 效果（enable）：
#   1. 建本地服务账号 SuperAIDB-Svc（随机密码不落盘）
#   2. 密钥交接：主密钥从操作者凭据管理器导出 → db/.vault/master.key
#      （凭据管理器是 per-user 的，服务账号读不到操作者 vault；
#        密钥文件由 db/ 的 ACL 保护，隔离模式的密钥后端，key_manager 自动切换）
#   3. ACL 收紧到 {SYSTEM, Administrators, 服务账号, 操作者} 四方，其余 OS 账号物理拒绝：
#      db/（含密钥文件）：服务账号 M、操作者 M（管理端 auth/元库直读仍需）
#      config/*.yml|*.env：服务账号 M、操作者 M（管理端读配置）
#      config/runtime/：服务账号 M（写 daemon.json）、操作者 R（读 IPC 令牌）——
#        其他 OS 账号读不到令牌，跨用户 RPC 面在此封死（安全评审 M1）
#   4. 注册计划任务 SuperAIDB-DataDaemon：开机即以服务账号启动 daemon（不论登录与否）
#   5. 终止用户态 daemon，以任务方式拉起服务态 daemon
# disable：完整回滚（删任务、杀服务态 daemon、删隔离标志与密钥文件、ACL 恢复继承、删账号）
# status：纯探测（无提权），输出 JSON 状态
[CmdletBinding()]
param([ValidateSet("enable", "disable", "status")][string]$Mode = "status",
      [string]$PythonExe = "")

$ErrorActionPreference = "Stop"
$svcUser = "SuperAIDB-Svc"
$taskName = "SuperAIDB-DataDaemon"
$engineRoot = Split-Path -Parent $PSScriptRoot   # scripts/ 的上级 = data-engine
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
# Python 解析：调用方（管理端）传入 sys.executable 优先，不吃 PATH/商店别名陷阱
if (-not $PythonExe) { $PythonExe = (Get-Command python).Source }

if ($Mode -eq "status") {
    $svc = Get-LocalUser -Name $svcUser -ErrorAction SilentlyContinue
    $daemonRunning = $false
    if ($task) {
        $proc = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like '*core.daemon.server*' }
        # 服务态 daemon 的属主是服务账号
        $daemonRunning = [bool]($proc | Where-Object {
            (Invoke-CimMethod -InputObject $_ -MethodName GetOwner).User -eq $svcUser })
    }
    @{ active = [bool]($task -and $svc); daemon_as_service = $daemonRunning;
       account = $svcUser } | ConvertTo-Json -Compress
    exit 0
}

# enable/disable 需要管理员（由调用方负责提权）
# 提权子进程的 stdout 不回传父进程——脚本自己记日志（Start-Transcript）
$logDir = Join-Path $engineRoot "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
Start-Transcript -Path (Join-Path $logDir "isolation_setup.log") -Append | Out-Null
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
            ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { Write-Error "需要管理员权限（请经 UAC 提权后运行）"; exit 1 }

# 操作者账号（UAC 提权不改身份，拿到的就是交互管理员本人）——
# 管理端/控制台以该账号运行，隔离后仍需读 db/ 与 config/
$opUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

if ($Mode -eq "enable") {
    # 1. 服务账号
    if (-not (Get-LocalUser -Name $svcUser -ErrorAction SilentlyContinue)) {
        $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        $bytes = New-Object byte[] 24
        $rng.GetBytes($bytes)
        $pwd = [Convert]::ToBase64String($bytes) -replace "[^a-zA-Z0-9]", "x"
        New-LocalUser -Name $svcUser -Password (ConvertTo-SecureString $pwd -AsPlainText -Force) `
            -PasswordNeverExpires -UserMayNotChangePassword | Out-Null
    }

    # 2. 密钥交接（在 ACL 收紧前做，脚本进程有 Administrators:F 兜底）。
    #    fail-closed：已有加密库而钥匙交接失败 → 中止 enable，绝不进入
    #    "daemon 生成新钥匙、旧库永久锁死" 的半隔离态（评审三轮 P0）
    $runtimeDir = Join-Path $engineRoot "config\runtime"
    if (-not (Test-Path $runtimeDir)) { New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null }
    $vaultDir = Join-Path $engineRoot "db\.vault"
    if (-not (Test-Path $vaultDir)) { New-Item -ItemType Directory -Path $vaultDir -Force | Out-Null }
    $keyFile = Join-Path $vaultDir "master.key"
    if (-not (Test-Path $keyFile)) {
        # 从操作者凭据管理器取既有主密钥（显式校验退出码——原生命令不吃
        # $ErrorActionPreference，静默失败曾是本链最大隐患）
        $existing = & $PythonExe -c "import keyring; print(keyring.get_password('SuperAIDB','db_master_key') or '')"
        if ($LASTEXITCODE -ne 0) { throw "凭据管理器读取失败（exit $LASTEXITCODE），已中止" }
        if ($existing) {
            [IO.File]::WriteAllText($keyFile, $existing.Trim())
        } else {
            # 无既有密钥：若已存在加密库则交接失败必须中止（否则旧库永久锁死）
            $hasEncrypted = $false
            Get-ChildItem (Join-Path $engineRoot "db\*.db") -File -ErrorAction SilentlyContinue | ForEach-Object {
                $fs = [IO.File]::OpenRead($_.FullName); $buf = New-Object byte[] 16
                $n = $fs.Read($buf, 0, 16); $fs.Close()
                if ($n -eq 16 -and -not ([Text.Encoding]::ASCII.GetString($buf).StartsWith("SQLite format 3"))) {
                    $hasEncrypted = $true
                }
            }
            if ($hasEncrypted) {
                throw "存在加密数据库但凭据管理器中没有主密钥——交接失败已中止（数据保护优先）。请先在用户态正常启动一次应用（生成密钥）再开启隔离。"
            }
            # 全新部署：留空由服务态 daemon 首启生成进密钥文件
        }
    }
    New-Item -ItemType File -Path (Join-Path $runtimeDir "isolated.flag") -Force | Out-Null

    # 3. ACL：全部收紧到 {SYSTEM, Administrators, svcUser, opUser}
    $dbDir = Join-Path $engineRoot "db"
    if (Test-Path $dbDir) {
        icacls $dbDir /inheritance:r /grant:r "SYSTEM:OI:CI:F" "Administrators:OI:CI:F" "${svcUser}:OI:CI:M" "${opUser}:OI:CI:M" | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "icacls 失败: $dbDir" }
    }
    Get-ChildItem (Join-Path $engineRoot "config\*") -Include *.yml, *.env, *.yaml -File |
        ForEach-Object { icacls $_.FullName /inheritance:r /grant:r "SYSTEM:F" "Administrators:F" "${svcUser}:M" "${opUser}:M" | Out-Null; if ($LASTEXITCODE -ne 0) { throw "icacls 失败: $($_.FullName)" } }
    icacls $runtimeDir /inheritance:r /grant:r "SYSTEM:OI:CI:F" "Administrators:OI:CI:F" "${svcUser}:OI:CI:M" "${opUser}:OI:CI:R" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls 失败: $runtimeDir" }
    # 注意：操作者对 runtime/ 只读——服务态 daemon 失联时，用户态进程无法自动重拉
    #（写不了拉起锁），会如实报错；此时应 Start-ScheduledTask 恢复服务态，而非降级。

    # 4. 计划任务（开机自启，服务账号，最高权限；解释器用调用方传入的真实路径；
    #    经 cmd /c 注入 DAEMON_MODE=false——daemon 的环境契约，缺了它服务态
    #    daemon 业务调用自锁死且 ping 假活，评审五轮 D2）
    $python = $PythonExe
    $action = New-ScheduledTaskAction -Execute "cmd.exe" `
        -Argument "/c set DAEMON_MODE=false&& `"$python`" -m core.daemon.server" `
        -WorkingDirectory $engineRoot
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $trigger.Enabled = $true
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
    $principal = New-ScheduledTaskPrincipal -UserId $svcUser -LogonType ServiceAccount -RunLevel Highest
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null

    # 5. 杀掉用户态 daemon，以服务态拉起
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*core.daemon.server*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-ScheduledTask -TaskName $taskName
    Stop-Transcript | Out-Null
    Write-Output '{"ok": true, "message": "系统级数据隔离已启用（daemon 以服务账号运行；db/、config 密钥文件、IPC 令牌目录已收紧到服务账号+操作者）"}'
    exit 0
}

if ($Mode -eq "disable") {
    if ($task) { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false }
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like '*core.daemon.server*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    # 密钥回程：master.key 先回导操作者凭据管理器，成功才删密钥文件——
    # 密钥是唯一数据生命线，任何路径下都不得静默销毁最后一份副本（评审三轮 P0）
    $keyFile = Join-Path $engineRoot "db\.vault\master.key"
    $keyBack = $true
    if (Test-Path $keyFile) {
        & $PythonExe -c "import keyring, pathlib; keyring.set_password('SuperAIDB','db_master_key', pathlib.Path(r'$keyFile').read_text(encoding='utf-8').strip())"
        if ($LASTEXITCODE -ne 0) {
            $keyBack = $false
            Write-Warning "密钥回导凭据管理器失败——已保留 $keyFile（未删除）。请手动备份后再处理。"
        }
    }
    Remove-Item (Join-Path $engineRoot "config\runtime\isolated.flag") -Force -ErrorAction SilentlyContinue
    if ($keyBack) { Remove-Item $keyFile -Force -ErrorAction SilentlyContinue }
    $dbDir = Join-Path $engineRoot "db"
    if (Test-Path $dbDir) { icacls $dbDir /inheritance:e /remove:r $svcUser | Out-Null }
    Get-ChildItem (Join-Path $engineRoot "config\*") -Include *.yml, *.env, *.yaml -File -ErrorAction SilentlyContinue |
        ForEach-Object { icacls $_.FullName /inheritance:e /remove:r $svcUser | Out-Null }
    $runtimeDir = Join-Path $engineRoot "config\runtime"
    if (Test-Path $runtimeDir) { icacls $runtimeDir /inheritance:e /remove:r $svcUser | Out-Null }
    if (Get-LocalUser -Name $svcUser -ErrorAction SilentlyContinue) {
        Remove-LocalUser -Name $svcUser
    }
    Stop-Transcript | Out-Null
    if (-not $keyBack) {
        Write-Output '{"ok": true, "message": "系统级数据隔离已关闭，但密钥回导失败——db/.vault/master.key 已保留，请手动备份"}'
        exit 0
    }
    Write-Output '{"ok": true, "message": "系统级数据隔离已关闭（密钥已回导凭据管理器，daemon 回到用户态，目录权限已还原）"}'
    exit 0
}
