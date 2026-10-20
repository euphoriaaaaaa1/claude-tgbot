# stop-bots.ps1 — 停掉本仓库所有 dispatcher（连子进程 worker 一起）
# 只匹配本仓库 dispatcher\dispatcher.ts 的命令行，不误杀别的项目。
# 不需要注册表：全部 bot 停用时也要能停掉遗留进程（停用 bot 不在名单里）。
$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $PSScriptRoot
$RunDir = if ($env:CLAUDEBOT_RUN_DIR) { $env:CLAUDEBOT_RUN_DIR } else { Join-Path $env:USERPROFILE ".claude-tgbot\run" }

$DispatcherPath = Join-Path $RepoDir "dispatcher\dispatcher.ts"
# 命令行里可能是反斜杠也可能是正斜杠；[regex]::Escape 后再把转义的反斜杠放宽
$Pattern = [regex]::Escape($DispatcherPath) -replace '\\\\', '[\\/]'
$procs = @(Get-CimInstance Win32_Process -Filter "Name = 'bun.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match $Pattern })

if ($procs.Count -eq 0) {
    Remove-Item (Join-Path $RunDir "*.pid") -ErrorAction SilentlyContinue
    Write-Host "没有正在运行的 dispatcher。"
    exit 0
}

$Failed = $false
foreach ($proc in $procs) {
    & taskkill /PID $proc.ProcessId /T /F | Out-Null
    if ($LASTEXITCODE -ne 0) {
        [Console]::Error.WriteLine("停止 dispatcher pid $($proc.ProcessId) 失败（taskkill 退出码 $LASTEXITCODE）")
        $Failed = $true
    } else {
        Write-Host "已停 dispatcher pid $($proc.ProcessId)"
    }
}
Remove-Item (Join-Path $RunDir "*.pid") -ErrorAction SilentlyContinue
Write-Host "全部 bot 已停。"
if ($Failed) { exit 1 }
exit 0
