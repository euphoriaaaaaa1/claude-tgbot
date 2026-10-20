# restart-bots.ps1 — 杀掉所有 dispatcher(连带 worker 子进程) 后重新启动
$ErrorActionPreference = "SilentlyContinue"
# 按命令行特征找 dispatcher 的 bun 进程，连子进程树一起杀（worker 会在下条消息 --resume 复活，记忆不丢）
Get-CimInstance Win32_Process -Filter "Name = 'bun.exe'" |
    Where-Object { $_.CommandLine -match 'dispatcher\.ts' } |
    ForEach-Object {
        Write-Host "杀 dispatcher pid $($_.ProcessId)"
        & taskkill /PID $_.ProcessId /T /F | Out-Null
    }
Start-Sleep -Seconds 2
& (Join-Path $PSScriptRoot "start-bots.ps1")
