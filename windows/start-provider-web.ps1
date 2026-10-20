# start-provider-web.ps1 — 启动 8767 管理台（Windows）
# 与 macOS/Linux 的 `MOMENTS_WEB_MODE=provider python3 -m moments.web` 同入口。
# 启动失败非零退出；已在运行时直接跳过。
$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $PSScriptRoot
$LogDir = if ($env:CLAUDEBOT_LOG_DIR) { $env:CLAUDEBOT_LOG_DIR } else { Join-Path $env:TEMP "claude-tgbot-logs" }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Py) { $Py = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
if (-not $Py) { Write-Error "找不到 python/python3，无法启动 8767"; exit 1 }

$Port = if ($env:MOMENTS_WEB_PORT) { [int]$env:MOMENTS_WEB_PORT } else { 8767 }

function Test-PortOpen([int]$P) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect("127.0.0.1", $P, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(500)) { return $false }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

if (Test-PortOpen $Port) {
    Write-Host "8767 已在运行 (port $Port)，跳过"
    exit 0
}

$env:MOMENTS_WEB_MODE = "provider"
$env:MOMENTS_WEB_PORT = "$Port"
$out = Join-Path $LogDir "provider-web.log"
$err = Join-Path $LogDir "provider-web.err.log"
$p = Start-Process -FilePath $Py `
    -ArgumentList @("-m", "moments.web") `
    -WorkingDirectory $RepoDir `
    -RedirectStandardOutput $out `
    -RedirectStandardError $err `
    -WindowStyle Hidden -PassThru

Start-Sleep -Seconds 2
$p.Refresh()
if ($p.HasExited) {
    [Console]::Error.WriteLine("8767 启动失败（进程已退出，见日志 $out / $err）")
    exit 1
}
Write-Host "已启动 8767 (pid $($p.Id), port $Port, 日志 $out)"
exit 0
