# register-tasks.ps1 — 注册任务计划（统一走 platform_services.py）
# 平台任务名、触发器、命令与环境变量都在 Python 的 platform_services.py 里定义；
# 本脚本只负责从注册表读 bot 名单，然后逐个调用它。
# 需管理员或当前用户任务权限。卸载：unregister-tasks.ps1
$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $PSScriptRoot
$Py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $Py) { $Py = (Get-Command python3).Source }
if (-not $Py) { Write-Error "找不到 python/python3，无法注册计划任务"; exit 1 }
$ServiceScript = Join-Path $RepoDir "platform_services.py"
# 频道目录口径与 start-bots.ps1 / restart-bots.example.sh 一致：HUB_CHANNELS_DIR 优先。
$Channels = if ($env:HUB_CHANNELS_DIR) { $env:HUB_CHANNELS_DIR } else { Join-Path $env:USERPROFILE ".claude\channels" }
# PowerShell 按 [Console]::OutputEncoding 解码子进程 stdout；中文 Windows 上默认 cp936，
# 注册表里的 emoji display_name 会被解成乱码 → ConvertFrom-Json 直接失败。
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

$script:RegisterFailed = $false

# 调 platform_services.py register <name>；失败时打印任务名与手动命令，继续注册其余任务。
function Register-Service([string]$Name, [string[]]$Extra = @()) {
    & $Py $ServiceScript register $Name --repo $RepoDir @Extra
    if ($LASTEXITCODE -ne 0) {
        [Console]::Error.WriteLine(
            "注册 $Name 失败（退出码 $LASTEXITCODE）。手动重跑：`"$Py`" `"$ServiceScript`" register $Name --repo `"$RepoDir`" " + ($Extra -join " ")
        )
        $script:RegisterFailed = $true
    }
}

# 1) dispatcher：登录时自启（等价 macOS 的 autostart）
Register-Service "dispatchers"

# 2) self-initiate：每 10 分钟 tick（脚本内部随机 30min-24h 自节流），**每个 bot 一个任务**。
#    bot 名单来自注册表（configs\*.yml），加 bot 后重跑本脚本即可带上，不用手改本文件。
#    chat_id 自动从该 bot 的 access.json → allowFrom 取第一个纯数字项。
#    先判 $LASTEXITCODE 再 ConvertFrom-Json：读不出名单就整段跳过并 warn，其余任务照常注册。
Push-Location $RepoDir
$RegistryJson = & $Py -m bots_registry --format=json
$RegistryCode = $LASTEXITCODE
Pop-Location
if ($RegistryCode -ne 0 -or -not $RegistryJson) {
    Write-Warning "跳过全部 self-initiate：bots_registry 读不出 bot 名单（退出码 $RegistryCode）。其余任务已正常注册。"
    $Registry = @()
} else {
    # @() 包一层：只有一个 bot 时 ConvertFrom-Json 回的是单个对象而不是数组
    $Registry = @($RegistryJson | ConvertFrom-Json)
}
foreach ($b in $Registry) {
    $AccessJson   = Join-Path $Channels "$($b.id)\access.json"
    $SelfInitChat = $null
    if (Test-Path $AccessJson) {
        try {
            $allowFrom = (Get-Content -Raw -Encoding UTF8 $AccessJson | ConvertFrom-Json).allowFrom
            # 过滤掉 YOUR_TELEGRAM_USER_ID 之类占位；JSON 里写成数字或字符串都能认
            $SelfInitChat = $allowFrom | Where-Object { "$_" -match '^\d+$' } | Select-Object -First 1
        } catch {
            # JSON 坏了/格式不对 → 当成没读到，下面 warn
        }
    }
    if ($SelfInitChat) {
        Register-Service "self-initiate-$($b.id)" @("--bot", "$($b.id)", "--chat-id", "$SelfInitChat")
    } else {
        Write-Warning "跳过 claude-tgbot-self-initiate-$($b.id)：没能从 $AccessJson 的 allowFrom 读到数字 user_id。填好白名单后重跑本脚本即可补上（其余任务已正常注册）。"
    }
}

# 3) jiwen tick：每 5 分钟（情绪+关系数值）
Register-Service "jiwen-tick"

# 4) memory compactor：每天 04:00（长期记忆压缩）
Register-Service "memory-compactor"

# 8767 管理台与 8770 供应商代理的服务名已在 platform_services.py 里定义
# （provider-web / provider-proxy）。T06/T09 的进程入口就位后再启用下面两行，
# 现在不默认注册，免得创建指向尚不存在入口的任务：
#   Register-Service "provider-web"
#   Register-Service "provider-proxy"

Write-Host ""
if ($script:RegisterFailed) {
    Write-Host "有任务注册失败，见上面的错误与手动命令。" -ForegroundColor Red
    exit 1
}
Write-Host "全部注册完成。查看：taskschd.msc 或 Get-ScheduledTask -TaskName 'claude-tgbot-*'"
