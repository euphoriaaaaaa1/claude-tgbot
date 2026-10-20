# watch-bot.ps1 [bot] — 实时看某 bot 的对话流（替代 tmux attach）
# 不传 bot 时取注册表里的第一只；没有任何 bot 就报错退出。
param([string]$Bot = "")
# 让控制台按 UTF-8 显示中文/emoji（日志文件是 UTF-8）。老 cmd 窗口 emoji 可能显示成方块，
# 中文能正常看；想完美显示 emoji 请用 Windows Terminal。
try { chcp 65001 > $null } catch {}
$OutputEncoding = [System.Text.Encoding]::UTF8
$RepoDir = Split-Path -Parent $PSScriptRoot
$Channels = if ($env:CLAUDEBOT_CHANNELS_ROOT) { $env:CLAUDEBOT_CHANNELS_ROOT } elseif ($env:HUB_CHANNELS_DIR) { $env:HUB_CHANNELS_DIR } else { Join-Path $env:USERPROFILE ".claude\channels" }

if (-not $Bot) {
    $Py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $Py) { $Py = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
    if (-not $Py) { Write-Error "找不到 python/python3，无法读取注册表"; exit 1 }
    Push-Location $RepoDir
    $RegistryJson = & $Py -m bots_registry --format=json
    $RegistryCode = $LASTEXITCODE
    Pop-Location
    if ($RegistryCode -ne 0 -or -not $RegistryJson) {
        Write-Error "bots_registry 读不出 bot 名单（退出码 $RegistryCode）；请用 watch-bot.ps1 <bot> 指定"
        exit 1
    }
    $Registry = @($RegistryJson | ConvertFrom-Json)
    $Bot = $Registry[0].id
}

# bot id 会拼进路径：拒绝带路径分隔符/.. 的输入
if ($Bot -match '[\\/]' -or $Bot -eq "." -or $Bot -eq "..") {
    Write-Error "非法 bot id：$Bot"
    exit 1
}

$log = Join-Path $Channels "$Bot\logs\chat.log"
if (-not (Test-Path $log)) {
    Write-Host "还没有日志（worker 尚未说过话）：$log"
    Write-Host "等第一条消息后再运行本脚本。"
    exit 1
}
Write-Host "── 实时对话流 $Bot（Ctrl+C 退出）──"
Get-Content $log -Tail 50 -Wait -Encoding UTF8
