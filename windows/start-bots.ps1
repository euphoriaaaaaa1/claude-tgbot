# start-bots.ps1 — 启动全部 bot 的 dispatcher（Windows）
# 每加一个 bot：往 $Bots 加一行 @{ Name="botname"; Port=17802 }
$ErrorActionPreference = "Stop"
# 保证任务计划/隐藏会话里能找到 bun（登录会话 PATH 未必含 ~/.bun/bin）
$env:PATH = "$env:USERPROFILE\.bun\bin;$env:PATH"
$RepoDir  = Split-Path -Parent $PSScriptRoot
$Channels = Join-Path $env:USERPROFILE ".claude\channels"
$LogDir   = Join-Path $env:TEMP "claude-tgbot-logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Bots = @(
    @{ Name = "chenlulu"; Port = 17801 }
)

foreach ($b in $Bots) {
    $chan = Join-Path $Channels $b.Name
    $envFile = Join-Path $chan ".env"
    if (-not (Test-Path $envFile)) {
        Write-Warning "跳过 $($b.Name)：$envFile 不存在（先复制 .env.example 填 token）"
        continue
    }
    # 读 .env 里的 TELEGRAM_BOT_TOKEN
    $token = ""
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*TELEGRAM_BOT_TOKEN\s*=\s*(.+)$') { $token = $Matches[1].Trim() }
    }
    if (-not $token) { Write-Warning "跳过 $($b.Name)：.env 里没有 TELEGRAM_BOT_TOKEN"; continue }

    # 已在跑就跳过（问 dispatcher /status）
    try {
        Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$($b.Port)/status" -TimeoutSec 2 | Out-Null
        Write-Host "$($b.Name) 已在运行 (port $($b.Port))，跳过"
        continue
    } catch {}

    $out = Join-Path $LogDir "$($b.Name)-dispatcher.log"
    $psi = @{
        FilePath               = "bun"
        ArgumentList           = @((Join-Path $RepoDir "dispatcher\dispatcher.ts"))
        WorkingDirectory       = (Join-Path $RepoDir "dispatcher")
        RedirectStandardOutput = $out
        RedirectStandardError  = ($out -replace '\.log$', '.err.log')
        WindowStyle            = "Hidden"
        PassThru               = $true
    }
    $env:CHANNEL_DIR        = $chan
    $env:TELEGRAM_BOT_TOKEN = $token
    $env:BOT_NAME           = $b.Name
    $env:DISPATCHER_PORT    = "$($b.Port)"
    $p = Start-Process @psi
    Write-Host "已启动 $($b.Name) (pid $($p.Id), port $($b.Port), 日志 $out)"
}
