# unregister-tasks.ps1 — 卸载所有 claude-tgbot 任务计划
Get-ScheduledTask -TaskName "claude-tgbot-*" -ErrorAction SilentlyContinue |
    ForEach-Object {
        Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false
        Write-Host "已卸载 $($_.TaskName)"
    }
