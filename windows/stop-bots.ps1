# stop-bots.ps1 — 停掉本仓库所有 dispatcher（连子进程 worker 一起）
# 只匹配本仓库 dispatcher\dispatcher.ts 的命令行，不误杀别的项目。
# 不需要注册表：全部 bot 停用时也要能停掉遗留进程（停用 bot 不在名单里）。
# -Only <bot>：只停 <RunDir>\<bot>.pid 记的、命令行匹配本仓库 dispatcher.ts 的那一个，只删这一个 pid 文件
# （管理台单只重启经 restart-bots.ps1 -Only 调用）。退出码：0 已停或本来就没在跑；1 停不掉；2 bot 编号不对。
param([string]$Only = "")
$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $PSScriptRoot
$RunDir = if ($env:CLAUDEBOT_RUN_DIR) { $env:CLAUDEBOT_RUN_DIR } else { Join-Path $env:USERPROFILE ".claude-tgbot\run" }

$DispatcherPath = Join-Path $RepoDir "dispatcher\dispatcher.ts"
# 命令行里可能是反斜杠也可能是正斜杠；[regex]::Escape 后再把转义的反斜杠放宽
$Pattern = [regex]::Escape($DispatcherPath) -replace '\\\\', '[\\/]'

if ($Only) {
    if ($Only -notmatch '^[A-Za-z0-9][A-Za-z0-9_-]*$') {
        [Console]::Error.WriteLine("非法 bot id：$Only")
        exit 2
    }
    $PidFile = Join-Path $RunDir "$Only.pid"
    $RecordedPid = 0
    if (Test-Path -LiteralPath $PidFile -PathType Leaf) {
        $raw = (Get-Content -LiteralPath $PidFile -TotalCount 1 -ErrorAction SilentlyContinue)
        if ($raw -match '^\s*(\d+)\s*$') { $RecordedPid = [int]$Matches[1] }
    }
    $proc = $null
    if ($RecordedPid -gt 0) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId = $RecordedPid" -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -eq 'bun.exe' -and $_.CommandLine -match $Pattern } | Select-Object -First 1
    }
    if (-not $proc) {
        Remove-Item -LiteralPath $PidFile -ErrorAction SilentlyContinue
        Write-Host "$Only 的 dispatcher 没在运行。"
        exit 0
    }
    & taskkill /PID $proc.ProcessId /T /F | Out-Null
    if ($LASTEXITCODE -ne 0) {
        [Console]::Error.WriteLine("停止 $Only 的 dispatcher pid $($proc.ProcessId) 失败（taskkill 退出码 $LASTEXITCODE）")
        exit 1
    }
    Remove-Item -LiteralPath $PidFile -ErrorAction SilentlyContinue
    Write-Host "已停 dispatcher $Only (pid $($proc.ProcessId))"
    exit 0
}

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
