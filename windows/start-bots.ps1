# start-bots.ps1 — 启动全部 bot 的 dispatcher（Windows）
# bot 名单/端口/命名空间来自注册表（configs\*.yml），加 bot 不用改本文件。
# 进程模型与 macOS/Linux 一致：每个 bot 一个后台 bun 进程，pid/日志落本机目录。
$ErrorActionPreference = "Stop"
# 保证任务计划/隐藏会话里能找到 bun（登录会话 PATH 未必含 ~/.bun/bin）
$env:PATH = "$env:USERPROFILE\.bun\bin;$env:PATH"
$RepoDir  = Split-Path -Parent $PSScriptRoot
$Channels = if ($env:CLAUDEBOT_CHANNELS_ROOT) { $env:CLAUDEBOT_CHANNELS_ROOT } elseif ($env:HUB_CHANNELS_DIR) { $env:HUB_CHANNELS_DIR } else { Join-Path $env:USERPROFILE ".claude\channels" }
$LogDir   = if ($env:CLAUDEBOT_LOG_DIR) { $env:CLAUDEBOT_LOG_DIR } else { Join-Path $env:TEMP "claude-tgbot-logs" }
$RunDir   = if ($env:CLAUDEBOT_RUN_DIR) { $env:CLAUDEBOT_RUN_DIR } else { Join-Path $env:USERPROFILE ".claude-tgbot\run" }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path $RunDir | Out-Null

$Py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Py) { $Py = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
if (-not $Py) { Write-Error "找不到 python/python3，无法读取注册表"; exit 1 }

$Bun = (Get-Command bun -ErrorAction SilentlyContinue).Source
if (-not $Bun) {
    $Candidate = Join-Path $env:USERPROFILE ".bun\bin\bun.exe"
    if (Test-Path $Candidate) { $Bun = $Candidate }
}
if (-not $Bun) { Write-Error "找不到 bun（先装 Bun，见 bun.sh）"; exit 1 }

# PowerShell 按 [Console]::OutputEncoding 解码子进程 stdout；中文 Windows 上默认 cp936，
# 注册表里的 emoji display_name 会被解成乱码 → ConvertFrom-Json 直接失败。
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$env:PYTHONUTF8 = '1'  # bot 子进程与注册表 CLI 的 open/stdio 全部默认 UTF-8，防 cp936 乱码
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

# 只探 /health（免口令）；/status 需要 X-Api-Token，不能拿来判活。
function Test-DispatcherAlive([int]$Port) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -Uri ("http://127.0.0.1:{0}/health" -f $Port) -TimeoutSec 2
        return ($r.StatusCode -eq 200)
    } catch {
        return $false
    }
}

$Failed = $false
foreach ($b in $Registry) {
    $chan = Join-Path $Channels $b.id
    $envFile = Join-Path $chan ".env"
    if (-not (Test-Path $envFile)) {
        [Console]::Error.WriteLine("跳过 $($b.id)：$envFile 不存在（先复制 .env.example 填 token）")
        $Failed = $true
        continue
    }
    # 读 .env 里的 TELEGRAM_BOT_TOKEN（允许 export 前缀与成对引号）
    $token = ""
    foreach ($line in Get-Content -Encoding UTF8 $envFile) {
        if ($line -match '^\s*(?:export\s+)?TELEGRAM_BOT_TOKEN\s*=\s*(.+?)\s*$') {
            $token = $Matches[1].Trim()
            if ($token.Length -ge 2 -and (($token.StartsWith('"') -and $token.EndsWith('"')) -or
                                          ($token.StartsWith("'") -and $token.EndsWith("'")))) {
                $token = $token.Substring(1, $token.Length - 2)
            }
        }
    }
    if (-not $token) {
        [Console]::Error.WriteLine("跳过 $($b.id)：.env 里没有 TELEGRAM_BOT_TOKEN")
        $Failed = $true
        continue
    }

    # 已在跑就跳过（/health 免口令，避免误判后重复起进程抢同一 token）
    if (Test-DispatcherAlive ([int]$b.port)) {
        Write-Host "$($b.id) 已在运行 (port $($b.port))，跳过"
        continue
    }

    $out = Join-Path $LogDir "$($b.id)-dispatcher.log"
    $err = Join-Path $LogDir "$($b.id)-dispatcher.err.log"
    $psi = @{
        FilePath               = $Bun
        ArgumentList           = @((Join-Path $RepoDir "dispatcher\dispatcher.ts"))
        WorkingDirectory       = (Join-Path $RepoDir "dispatcher")
        RedirectStandardOutput = $out
        RedirectStandardError  = $err
        WindowStyle            = "Hidden"
        PassThru               = $true
    }
    $env:CHANNEL_DIR        = $chan
    $env:TELEGRAM_BOT_TOKEN = $token
    $env:BOT_NAME           = $b.id
    $env:DISPATCHER_PORT    = "$($b.port)"
    # BOT_NAMESPACE 必须注进去：worker-manager.ts 是 env 优先，注了它新 bot 才认得
    # 自己的记忆命名空间（TS 侧那张表退化为 legacy 兜底，源码零改动）。
    $env:BOT_NAMESPACE      = "$($b.namespace_uuid)"

    try {
        $p = Start-Process @psi
    } catch {
        [Console]::Error.WriteLine("启动 $($b.id) 失败：$($_.Exception.Message)；日志 $out")
        $Failed = $true
        continue
    }
    Set-Content -Path (Join-Path $RunDir "$($b.id).pid") -Value "$($p.Id)" -Encoding ASCII
    Start-Sleep -Milliseconds 700
    $p.Refresh()
    if ($p.HasExited) {
        [Console]::Error.WriteLine("启动失败 $($b.id)（进程已退出，见日志 $out / $err）")
        Remove-Item (Join-Path $RunDir "$($b.id).pid") -ErrorAction SilentlyContinue
        $Failed = $true
    } else {
        Write-Host "已启动 $($b.id) (pid $($p.Id), port $($b.port), 日志 $out)"
    }
}
# 别把最后一个 bot 的 token 留在环境里
Remove-Item Env:TELEGRAM_BOT_TOKEN -ErrorAction SilentlyContinue
if ($Failed) { exit 1 }
exit 0
