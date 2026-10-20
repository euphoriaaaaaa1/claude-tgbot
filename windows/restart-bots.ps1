# restart-bots.ps1 — 杀掉本仓库所有 dispatcher（连带 worker 子进程）后重新启动。
# worker 会在下条消息 --resume 复活，记忆不丢。
$ErrorActionPreference = "Stop"
$PSExe = (Get-Process -Id $PID).Path
$StopScript = Join-Path $PSScriptRoot "stop-bots.ps1"
$StartScript = Join-Path $PSScriptRoot "start-bots.ps1"

& $PSExe -NoProfile -ExecutionPolicy Bypass -File $StopScript
$stopRc = $LASTEXITCODE
if ($stopRc -ne 0) {
    [Console]::Error.WriteLine("停止失败（退出码 $stopRc），未执行启动。手动：powershell -File `"$StopScript`"")
    exit $stopRc
}

Start-Sleep -Seconds 2
& $PSExe -NoProfile -ExecutionPolicy Bypass -File $StartScript
exit $LASTEXITCODE
