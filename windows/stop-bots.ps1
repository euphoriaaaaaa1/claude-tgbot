# stop-bots.ps1 — 停掉本仓库所有 dispatcher（连子进程 worker 一起）
# 只匹配本仓库 dispatcher\dispatcher.ts 的命令行，不误杀别的项目。
# 不需要注册表：全部 bot 停用时也要能停掉遗留进程（停用 bot 不在名单里）。
# -Only <bot>：只停 <RunDir>\<bot>.pid 记的、命令行匹配本仓库 dispatcher.ts 且带本 bot 归属标记 --bot=<bot>
# （start-bots.ps1 拉起时加）的那一个，只删这一个 pid 文件（管理台单只重启经 restart-bots.ps1 -Only 调用）。
# pid 指向别的 bot（过期 pid 被复用）或没有标记的旧进程时不停，按「没在跑」处理，由 restart-bots.ps1 的端口检查兜底。
# 退出码：0 已停或本来就没在跑；1 停不掉；2 bot 编号不对。
param([string]$Only = "")
$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $PSScriptRoot
$RunDir = if ($env:CLAUDEBOT_RUN_DIR) { $env:CLAUDEBOT_RUN_DIR } else { Join-Path $env:USERPROFILE ".claude-tgbot\run" }

$DispatcherPath = Join-Path $RepoDir "dispatcher\dispatcher.ts"
# 命令行里可能是反斜杠也可能是正斜杠；[regex]::Escape 后再把转义的反斜杠放宽
$Pattern = [regex]::Escape($DispatcherPath) -replace '\\\\', '[\\/]'

if ($Only) {
    # 与 adapters/base.py 的 valid_bot_id 同一条规则：1 到 64 位字母数字与 . _ -，不能全是点。
    # 用 -cnotmatch（区分大小写）和 \z：忽略大小写时开尔文符号等非 ASCII 字符会被当成字母放行，$ 会放过结尾换行。
    if ($Only -cnotmatch '^(?!\.+\z)[A-Za-z0-9._-]{1,64}\z') {
        [Console]::Error.WriteLine("非法 bot 编号（只能是 1 到 64 位字母数字与 . _ -，且不能全是点）")
        exit 2
    }
    # 归属标记紧跟在 dispatcher.ts 之后（路径可能带引号），其后是空白、引号或结尾。
    # 路径部分不分大小写（Windows 路径如此），编号部分区分大小写（注册表按 -ceq 比对）。
    $OwnerPattern = '(?i:' + $Pattern + ')"?\s+--bot=' + [regex]::Escape($Only) + '(?=\s|"|$)'
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
    if ($proc -and $proc.CommandLine -cnotmatch $OwnerPattern) {
        # 走 stdout：这条是"按没在跑处理"的说明，不是失败，免得调用方把 stderr 当错误
        Write-Host "$Only 的 pid 文件记的 pid $($proc.ProcessId) 是本仓库的 dispatcher，但不是 $Only 的（别的 bot 复用了这个 pid，或是没有归属标记的旧进程），不停它；旧进程若仍占着端口，跑一次 restart-bots.ps1 整体重启"
        $proc = $null
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
