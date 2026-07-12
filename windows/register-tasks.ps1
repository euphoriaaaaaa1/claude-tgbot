# register-tasks.ps1 — 注册任务计划（等价 macOS 的 launchd plist 五件套）
# 需管理员或当前用户任务权限。卸载：unregister-tasks.ps1
$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $PSScriptRoot
$Py      = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Py) { $Py = (Get-Command python3).Source }
$User    = "$env:USERDOMAIN\$env:USERNAME"

function Register-Task($Name, $Action, $Trigger) {
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger -User $User `
        -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable) | Out-Null
    Write-Host "已注册 $Name"
}

# 1) dispatcher：登录时自启（等价 bots-autostart）
Register-Task "claude-tgbot-dispatchers" `
    (New-ScheduledTaskAction -Execute "powershell" -Argument "-NoProfile -WindowStyle Hidden -File `"$PSScriptRoot\start-bots.ps1`"") `
    (New-ScheduledTaskTrigger -AtLogOn)

# 2) self-initiate：每 10 分钟 tick（脚本内部随机 30min-24h 自节流）
#    多 bot 时复制本段改 bot 名/chat_id。chat_id = 你自己的 Telegram user_id。
$SelfInitBot  = "chenlulu"
$SelfInitChat = "YOUR_TELEGRAM_USER_ID"
Register-Task "claude-tgbot-self-initiate-$SelfInitBot" `
    (New-ScheduledTaskAction -Execute $Py -Argument "`"$RepoDir\scripts\self_initiate.py`" $SelfInitBot $SelfInitChat" -WorkingDirectory $RepoDir) `
    (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 10))

# 3) jiwen tick：每 5 分钟（情绪+关系数值）
Register-Task "claude-tgbot-jiwen-tick" `
    (New-ScheduledTaskAction -Execute $Py -Argument "`"$RepoDir\jiwen\tick.py`"" -WorkingDirectory $RepoDir) `
    (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5))

# 4) moments web：登录时自启（朋友圈网页 :8765，可选）
Register-Task "claude-tgbot-moments-web" `
    (New-ScheduledTaskAction -Execute $Py -Argument "-m moments.web" -WorkingDirectory $RepoDir) `
    (New-ScheduledTaskTrigger -AtLogOn)

# 5) memory compactor：每天 04:00（长期记忆压缩）
Register-Task "claude-tgbot-memory-compactor" `
    (New-ScheduledTaskAction -Execute $Py -Argument "`"$RepoDir\memory\memory_compactor.py`"" -WorkingDirectory $RepoDir) `
    (New-ScheduledTaskTrigger -Daily -At "04:00")

Write-Host ""
Write-Host "全部注册完成。查看：taskschd.msc 或 Get-ScheduledTask -TaskName 'claude-tgbot-*'"
