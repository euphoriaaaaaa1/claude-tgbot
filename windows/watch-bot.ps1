# watch-bot.ps1 [bot] — 实时看某 bot 的对话流（替代 tmux attach）
param([string]$Bot = "chenlulu")
$log = Join-Path $env:USERPROFILE ".claude\channels\$Bot\logs\chat.log"
if (-not (Test-Path $log)) {
    Write-Host "还没有日志（worker 尚未说过话）：$log"
    Write-Host "等第一条消息后再运行本脚本。"
    exit 1
}
Write-Host "── 实时对话流 $Bot（Ctrl+C 退出）──"
Get-Content $log -Tail 50 -Wait
