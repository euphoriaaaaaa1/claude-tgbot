# watch-bot.ps1 [bot] — 实时看某 bot 的对话流（替代 tmux attach）
param([string]$Bot = "chenlulu")
# 让控制台按 UTF-8 显示中文/emoji（日志文件是 UTF-8）。老 cmd 窗口 emoji 可能显示成方块，
# 中文能正常看；想完美显示 emoji 请用 Windows Terminal。
try { chcp 65001 > $null } catch {}
$OutputEncoding = [System.Text.Encoding]::UTF8
$log = Join-Path $env:USERPROFILE ".claude\channels\$Bot\logs\chat.log"
if (-not (Test-Path $log)) {
    Write-Host "还没有日志（worker 尚未说过话）：$log"
    Write-Host "等第一条消息后再运行本脚本。"
    exit 1
}
Write-Host "── 实时对话流 $Bot（Ctrl+C 退出）──"
Get-Content $log -Tail 50 -Wait -Encoding UTF8
