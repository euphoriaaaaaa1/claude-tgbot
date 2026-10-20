#!/usr/bin/env bash
# watch-bot.sh [bot] — 实时看某 bot 的对话流（替代 tmux attach）。mac/linux 用。
# 不传 bot 时取注册表里的第一只；没有任何 bot 就报错退出。
set -u
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CHANNELS_ROOT="${CLAUDEBOT_CHANNELS_ROOT:-${HUB_CHANNELS_DIR:-$HOME/.claude/channels}}"

bot="${1:-}"
if [ -z "$bot" ]; then
  registry="$(PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}" python3 -m bots_registry --format=sh 2>/dev/null)" || registry=""
  bot="$(printf '%s\n' "$registry" | sed -n '1p' | cut -f1)"
  if [ -z "$bot" ]; then
    echo "没有可用 bot：先在 configs/ 新建 <bot>.yml（不要写 enabled: false）。" >&2
    exit 2
  fi
fi

# bot id 会拼进路径：拒绝带路径分隔符/.. 的输入
case "$bot" in
  ''|.|..|*/*|*\\*) echo "非法 bot id：$bot" >&2; exit 2 ;;
esac

log="$CHANNELS_ROOT/$bot/logs/chat.log"
if [ ! -f "$log" ]; then
  echo "还没有日志（worker 尚未说过话）：$log"
  echo "等第一条消息后再运行本脚本。"
  exit 1
fi
echo "── 实时对话流 $bot（Ctrl-C 退出）──"
# 👤用户 🤖回复 ⚙工具 加点颜色，其余原样
tail -n 40 -F "$log" | sed \
  -e $'s/\\(👤.*\\)/\033[36m\\1\033[0m/' \
  -e $'s/\\(🤖.*\\)/\033[32m\\1\033[0m/' \
  -e $'s/\\(⚙.*\\)/\033[90m\\1\033[0m/'
