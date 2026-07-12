# stop-bots.ps1 — 停掉所有 dispatcher（连子进程 worker 一起）
$ErrorActionPreference = "SilentlyContinue"
Get-CimInstance Win32_Process -Filter "Name = 'bun.exe'" |
    Where-Object { $_.CommandLine -match 'dispatcher\.ts' } |
    ForEach-Object {
        & taskkill /PID $_.ProcessId /T /F | Out-Null
        Write-Host "已停 dispatcher pid $($_.ProcessId)"
    }
Write-Host "全部 bot 已停。"
