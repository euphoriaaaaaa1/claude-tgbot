# run-cliproxy.ps1 — cliproxy 守护循环。
# 计划任务没有 launchd 的 KeepAlive：直接跑二进制的话崩一次就再也不起来，
# 所以任务动作必须是本脚本（INTERFACE §8.2 B4）。
$ErrorActionPreference = "Continue"
$Root = if ($env:CLAUDE_TGBOT_HOME) { $env:CLAUDE_TGBOT_HOME } else { Join-Path $env:USERPROFILE ".claude-tgbot" }
$Dir = Join-Path $Root "cliproxy"
$Bin = Join-Path $Dir "cli-proxy-api.exe"
$Cfg = Join-Path $Dir "config.yaml"
if (-not (Test-Path $Bin)) { throw "还没装 cliproxy：先跑 windows\install-cliproxy.ps1" }
while ($true) {
    # -WorkingDirectory 必须钉在 $Dir：v7 启动会往进程 CWD 写 static\management.html（2.7 MB）
    Start-Process -FilePath $Bin -ArgumentList @("-config", $Cfg) -WorkingDirectory $Dir -Wait -NoNewWindow
    Start-Sleep -Seconds 10   # 崩了等 10 秒再拉起，等价 launchd 的 KeepAlive + ThrottleInterval
}
