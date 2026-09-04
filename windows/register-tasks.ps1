# register-tasks.ps1 — 注册任务计划（等价 macOS 的 launchd plist 五件套）
# 需管理员或当前用户任务权限。卸载：unregister-tasks.ps1
$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $PSScriptRoot
$Py      = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Py) { $Py = (Get-Command python3).Source }
$User    = "$env:USERDOMAIN\$env:USERNAME"
# 让定时任务里的 Python 用 UTF-8 读写/输出（防 GBK 控制台 UnicodeEncodeError）
# 注意：参数名不能叫 $Args（PowerShell 保留自动变量，PS7 直接报错、5.1 被遮蔽为空）。
function PyAction($PyArgs) {
    New-ScheduledTaskAction -Execute "cmd.exe" `
        -Argument "/c set PYTHONUTF8=1&& `"$Py`" $PyArgs" -WorkingDirectory $RepoDir
}

# 无限重复：不给 Duration 在部分 Windows 只触发一次 → 显式给 ~10 年
$Forever = (New-TimeSpan -Days 3650)

function Register-Task($Name, $Action, $Trigger) {
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger -User $User `
        -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable) | Out-Null
    Write-Host "已注册 $Name"
}

# 1) dispatcher：登录时自启（等价 bots-autostart）。必须带 -ExecutionPolicy Bypass，
#    否则默认 Restricted 策略下 -File 脚本被拒 → 开机自启全哑。
Register-Task "claude-tgbot-dispatchers" `
    (New-ScheduledTaskAction -Execute "powershell" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSScriptRoot\start-bots.ps1`"") `
    (New-ScheduledTaskTrigger -AtLogOn)

# 2) self-initiate：每 10 分钟 tick（脚本内部随机 30min-24h 自节流），**每个 bot 一个任务**。
#    bot 名单来自注册表（configs\*.yml），加 bot 后重跑本脚本即可带上，不用手改本文件。
#    chat_id 自动从该 bot 的 access.json → allowFrom 取第一个纯数字项。
#    先判 $LASTEXITCODE 再 ConvertFrom-Json：读不出名单就整段跳过并 warn，其余任务照常注册。
Push-Location $RepoDir
$RegistryJson = & $Py -m bots_registry --format=json
$RegistryCode = $LASTEXITCODE
Pop-Location
if ($RegistryCode -ne 0 -or -not $RegistryJson) {
    Write-Warning "跳过全部 self-initiate：bots_registry 读不出 bot 名单（退出码 $RegistryCode）。其余任务已正常注册。"
    $Registry = @()
} else {
    # @() 包一层：只有一个 bot 时 ConvertFrom-Json 回的是单个对象而不是数组
    $Registry = @($RegistryJson | ConvertFrom-Json)
}
foreach ($b in $Registry) {
    $AccessJson   = Join-Path $env:USERPROFILE ".claude\channels\$($b.id)\access.json"
    $SelfInitChat = $null
    if (Test-Path $AccessJson) {
        try {
            $allowFrom = (Get-Content -Raw -Encoding UTF8 $AccessJson | ConvertFrom-Json).allowFrom
            # 过滤掉 YOUR_TELEGRAM_USER_ID 之类占位；JSON 里写成数字或字符串都能认
            $SelfInitChat = $allowFrom | Where-Object { "$_" -match '^\d+$' } | Select-Object -First 1
        } catch {
            # JSON 坏了/格式不对 → 当成没读到，下面 warn
        }
    }
    if ($SelfInitChat) {
        Register-Task "claude-tgbot-self-initiate-$($b.id)" `
            (PyAction "`"$RepoDir\scripts\self_initiate.py`" $($b.id) $SelfInitChat") `
            (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 10) -RepetitionDuration $Forever)
    } else {
        Write-Warning "跳过 claude-tgbot-self-initiate-$($b.id)：没能从 $AccessJson 的 allowFrom 读到数字 user_id。填好白名单后重跑本脚本即可补上（其余任务已正常注册）。"
    }
}

# 3) jiwen tick：每 5 分钟（情绪+关系数值）
Register-Task "claude-tgbot-jiwen-tick" `
    (PyAction "`"$RepoDir\jiwen\tick.py`"") `
    (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration $Forever)

# 4) moments web：登录时自启（朋友圈网页 :8765，可选）
Register-Task "claude-tgbot-moments-web" `
    (PyAction "-m moments.web") `
    (New-ScheduledTaskTrigger -AtLogOn)

# 5) memory compactor：每天 04:00（长期记忆压缩）
Register-Task "claude-tgbot-memory-compactor" `
    (PyAction "`"$RepoDir\memory\memory_compactor.py`"") `
    (New-ScheduledTaskTrigger -Daily -At "04:00")

# 6) cliproxy：登录时自启（provider 管理台的后端代理，可选）。
#    动作必须是 run-cliproxy.ps1 守护循环——计划任务没有 launchd 的 KeepAlive，
#    直接跑二进制的话崩一次就再也不起来（INTERFACE §8.2 B4）。
#    -WorkingDirectory 必须钉在 ~/.claude-tgbot/cliproxy：v7 启动会往进程 CWD
#    写 static\management.html（2.7 MB），不钉就拉进代码仓库根目录。
$CliproxyRoot = if ($env:CLAUDE_TGBOT_HOME) { $env:CLAUDE_TGBOT_HOME } else { Join-Path $env:USERPROFILE ".claude-tgbot" }
$CliproxyDir  = Join-Path $CliproxyRoot "cliproxy"
if (Test-Path (Join-Path $CliproxyDir "cli-proxy-api.exe")) {
    Register-Task "claude-tgbot-cliproxy" `
        (New-ScheduledTaskAction -Execute "powershell" -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSScriptRoot\run-cliproxy.ps1`"" -WorkingDirectory $CliproxyDir) `
        (New-ScheduledTaskTrigger -AtLogOn)
} else {
    Write-Warning "跳过 claude-tgbot-cliproxy：还没装 cliproxy（先跑 windows\install-cliproxy.ps1），装完重跑本脚本即可补上。"
}

Write-Host ""
Write-Host "全部注册完成。查看：taskschd.msc 或 Get-ScheduledTask -TaskName 'claude-tgbot-*'"
