#!/usr/bin/env bash
# watch-bot.sh [bot] — 实时看某 bot 的对话流（替代 tmux attach）。mac/linux 用。
bot="${1:-chenlulu}"
log="$HOME/.claude/channels/$bot/logs/chat.log"
if [ ! -f "$log" ]; then
  echo "还没有日志（worker 尚未说过话）：$log"
  echo "等第一条消息后再运行。"
  exit 1
fi
echo "── 实时对话流 $bot（Ctrl-C 退出）──"
# 👤用户 🤖回复 ⚙工具 加点颜色，其余原样
tail -n 40 -F "$log" | sed \
  -e $'s/\\(👤.*\\)/\033[36m\\1\033[0m/' \
  -e $'s/\\(🤖.*\\)/\033[32m\\1\033[0m/' \
  -e $'s/\\(⚙.*\\)/\033[90m\\1\033[0m/'
