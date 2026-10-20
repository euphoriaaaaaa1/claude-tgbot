# unregister-tasks.ps1 — 卸载所有 claude-tgbot 任务计划（统一走 platform_services.py）
# 通配列出任务名后按完整任务名调用 Python 层，避免 PowerShell 再碰 schtasks 参数。
$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $PSScriptRoot
$Py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Py) { $Py = (Get-Command python3).Source }
if (-not $Py) { Write-Error "找不到 python/python3，无法卸载计划任务"; exit 1 }
$ServiceScript = Join-Path $RepoDir "platform_services.py"

$script:UnregisterFailed = $false
$Tasks = @(Get-ScheduledTask -TaskName "claude-tgbot-*" -ErrorAction SilentlyContinue)
if ($Tasks.Count -eq 0) {
    Write-Host "没有找到 claude-tgbot-* 任务。"
    exit 0
}
foreach ($Task in $Tasks) {
    & $Py $ServiceScript unregister --raw-label $Task.TaskName
    if ($LASTEXITCODE -ne 0) {
        [Console]::Error.WriteLine(
            "卸载 $($Task.TaskName) 失败（退出码 $LASTEXITCODE）。手动重跑：`"$Py`" `"$ServiceScript`" unregister --raw-label $($Task.TaskName)"
        )
        $script:UnregisterFailed = $true
    } else {
        Write-Host "已卸载 $($Task.TaskName)"
    }
}
if ($script:UnregisterFailed) { exit 1 }
