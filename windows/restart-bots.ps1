# restart-bots.ps1 — 杀掉本仓库所有 dispatcher（连带 worker 子进程）后重新启动。
# worker 会在下条消息 --resume 复活，记忆不丢。
# -Only <bot>：只重启这一只（管理台「主程序没响应」时的单只入口，与 POSIX 的 dispatcher/restart-one.sh 同一套
# 退出码）：0 已拉起；1 起不来；2 bot 编号不对、不在注册表或已停用；3 旧进程停不掉或停完端口仍被占（不启动）。
param([string]$Only = "")
$ErrorActionPreference = "Stop"
$PSExe = (Get-Process -Id $PID).Path
$StopScript = Join-Path $PSScriptRoot "stop-bots.ps1"
$StartScript = Join-Path $PSScriptRoot "start-bots.ps1"

# 判「传没传 -Only」而不是判真假：显式传空串也算传了，交给下面的编号校验退出 2，不能落到整批重启。
if ($PSBoundParameters.ContainsKey('Only')) {
    # 与 adapters/base.py 的 valid_bot_id 同一条规则（stop-bots.ps1 同此）：1 到 64 位字母数字与 . _ -，不能全是点
    if ($Only -cnotmatch '^(?!\.+\z)[A-Za-z0-9._-]{1,64}\z') {
        [Console]::Error.WriteLine("非法 bot 编号（只能是 1 到 64 位字母数字与 . _ -，且不能全是点），没有重启")
        exit 2
    }
    $RepoDir = Split-Path -Parent $PSScriptRoot
    $Py = (Get-Command python -ErrorAction SilentlyContinue).Source
    if (-not $Py) { $Py = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
    if (-not $Py) { [Console]::Error.WriteLine("找不到 python/python3，无法读取注册表"); exit 1 }
    [Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
    $env:PYTHONUTF8 = '1'
    Push-Location $RepoDir
    $RegistryJson = & $Py -m bots_registry --format=json
    $RegistryCode = $LASTEXITCODE
    Pop-Location
    # 名单为空（例如唯一一只也停用了）时 bots_registry 退出 1 并输出 []：按不在注册表处理，退出 2（同 restart-one.sh）
    if ($RegistryCode -eq 1 -and ("$RegistryJson".Trim() -eq '[]')) {
        [Console]::Error.WriteLine("$Only 不在注册表里或已停用，没有重启")
        exit 2
    }
    if ($RegistryCode -ne 0 -or -not $RegistryJson) {
        [Console]::Error.WriteLine("bots_registry 读不出 bot 名单（退出码 $RegistryCode），没有重启")
        exit 1
    }
    $Entry = @($RegistryJson | ConvertFrom-Json) | Where-Object { $_.id -ceq $Only } | Select-Object -First 1
    $Port = 0
    if ($Entry) { $Port = [int]$Entry.port }
    if ($Port -le 0) {
        [Console]::Error.WriteLine("$Only 不在注册表里或已停用，没有重启")
        exit 2
    }

    & $PSExe -NoProfile -ExecutionPolicy Bypass -File $StopScript -Only $Only
    if ($LASTEXITCODE -ne 0) {
        [Console]::Error.WriteLine("停止 $Only 失败（退出码 $LASTEXITCODE），没有启动")
        exit 3
    }

    # 连上 = 仍被占；拒连或 1 秒内没连上都算已释放（不依赖拒连快慢）
    function Test-PortInUse([int]$P) {
        $client = New-Object System.Net.Sockets.TcpClient
        try { return $client.ConnectAsync("127.0.0.1", $P).Wait(1000) } catch { return $false } finally { $client.Dispose() }
    }
    $Deadline = (Get-Date).AddSeconds(5)
    $InUse = Test-PortInUse $Port
    while ($InUse -and (Get-Date) -lt $Deadline) {
        Start-Sleep -Milliseconds 500
        $InUse = Test-PortInUse $Port
    }
    if ($InUse) {
        [Console]::Error.WriteLine("$Only 的端口 $Port 仍被占用，没有启动")
        exit 3
    }

    & $PSExe -NoProfile -ExecutionPolicy Bypass -File $StartScript -Only $Only
    exit $LASTEXITCODE
}

& $PSExe -NoProfile -ExecutionPolicy Bypass -File $StopScript
$stopRc = $LASTEXITCODE
if ($stopRc -ne 0) {
    [Console]::Error.WriteLine("停止失败（退出码 $stopRc），未执行启动。手动：powershell -File `"$StopScript`"")
    exit $stopRc
}

Start-Sleep -Seconds 2
& $PSExe -NoProfile -ExecutionPolicy Bypass -File $StartScript
exit $LASTEXITCODE
