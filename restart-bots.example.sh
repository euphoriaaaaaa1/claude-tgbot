#!/usr/bin/env bash
# 复制为 restart-bots.sh 后按需改。逐个 bot 重启 dispatcher（长轮询 + HTTP 服务）。
# 每加一个 bot，往下面 BOTS 数组加一行 "bot名:端口"。
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHANNELS="$HOME/.claude/channels"

# bot名:dispatcher端口（端口要和 configs/_global.yml 的 moments 及各 bot 一致）
BOTS=(
  "chenlulu:17801"
)

for entry in "${BOTS[@]}"; do
  bot="${entry%%:*}"
  port="${entry##*:}"
  chan="$CHANNELS/$bot"
  session="tg-$bot"

  # 读该 bot 的 token（.env 里 TELEGRAM_BOT_TOKEN=...）
  if [ ! -f "$chan/.env" ]; then
    echo "跳过 $bot：$chan/.env 不存在（先 cp .env.example .env 填 token）" >&2
    continue
  fi
  # shellcheck disable=SC1091
  set -a; . "$chan/.env"; set +a

  tmux kill-session -t "$session" 2>/dev/null || true
  tmux new-session -d -s "$session" \
    -e "CHANNEL_DIR=$chan" \
    -e "TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN" \
    -e "BOT_NAME=$bot" \
    -e "DISPATCHER_PORT=$port" \
    "bun $REPO_DIR/dispatcher/dispatcher.ts"
  echo "已启动 $bot（session=$session port=$port）"
done
