# start-bots.ps1 — 启动全部 bot 的 dispatcher（Windows）
# bot 名单/端口/命名空间来自注册表（configs\*.yml），加 bot 不用改本文件。
$ErrorActionPreference = "Stop"
# 保证任务计划/隐藏会话里能找到 bun（登录会话 PATH 未必含 ~/.bun/bin）
$env:PATH = "$env:USERPROFILE\.bun\bin;$env:PATH"
$RepoDir  = Split-Path -Parent $PSScriptRoot
$Channels = if ($env:HUB_CHANNELS_DIR) { $env:HUB_CHANNELS_DIR } else { Join-Path $env:USERPROFILE ".claude\channels" }
$LogDir   = Join-Path $env:TEMP "claude-tgbot-logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$Py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Py) { $Py = (Get-Command python3).Source }
# PowerShell 按 [Console]::OutputEncoding 解码子进程 stdout；中文 Windows 上默认 cp936，
# 注册表里的 emoji display_name 会被解成乱码 → ConvertFrom-Json 直接失败。
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
# 先判 $LASTEXITCODE 再 ConvertFrom-Json：注册表读不出来必须整体失败，
# 而不是把空名单当"本来就没有 bot"，让所有 bot 悄悄起不来。
Push-Location $RepoDir
$RegistryJson = & $Py -m bots_registry --format=json
$RegistryCode = $LASTEXITCODE
Pop-Location
if ($RegistryCode -ne 0 -or -not $RegistryJson) {
    Write-Error "bots_registry 读不出 bot 名单（退出码 $RegistryCode），未启动任何 bot"
    exit 1
}
# @() 包一层：只有一个 bot 时 ConvertFrom-Json 回的是单个对象而不是数组
$Registry = @($RegistryJson | ConvertFrom-Json)

foreach ($b in $Registry) {
    $chan = Join-Path $Channels $b.id
    $envFile = Join-Path $chan ".env"
    if (-not (Test-Path $envFile)) {
        Write-Warning "跳过 $($b.id)：$envFile 不存在（先复制 .env.example 填 token）"
        continue
    }
    # 读 .env 里的 TELEGRAM_BOT_TOKEN
    $token = ""
    foreach ($line in Get-Content $envFile) {
        if ($line -match '^\s*TELEGRAM_BOT_TOKEN\s*=\s*(.+)$') { $token = $Matches[1].Trim() }
    }
    if (-not $token) { Write-Warning "跳过 $($b.id)：.env 里没有 TELEGRAM_BOT_TOKEN"; continue }

    # 已在跑就跳过（问 dispatcher /status）
    try {
        Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$($b.port)/status" -TimeoutSec 2 | Out-Null
        Write-Host "$($b.id) 已在运行 (port $($b.port))，跳过"
        continue
    } catch {}

    $out = Join-Path $LogDir "$($b.id)-dispatcher.log"
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
    $env:BOT_NAME           = $b.id
    $env:DISPATCHER_PORT    = "$($b.port)"
    # BOT_NAMESPACE 必须注进去：worker-manager.ts 是 env 优先，注了它新 bot 才认得自己的
    # 记忆命名空间（TS 侧那张表退化为 legacy 兜底，源码零改动）。空值 = TS 自动回落该表。
    $env:BOT_NAMESPACE      = "$($b.namespace_uuid)"
    $p = Start-Process @psi
    Write-Host "已启动 $($b.id) (pid $($p.Id), port $($b.port), 日志 $out)"
}
