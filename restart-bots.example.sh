#!/usr/bin/env bash
# 复制为 restart-bots.sh 后按需改。逐个 bot 重启 dispatcher（长轮询 + HTTP 服务）。
# bot 名单/端口/命名空间全部来自注册表（configs/*.yml），**加 bot 不用再改本文件**。
set -eu

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
CHANNELS="${HUB_CHANNELS_DIR:-$HOME/.claude/channels}"
TAB="$(printf '\t')"

# 注册表读不出来就整体失败：`cmd | while read` 的退出状态是 while 的，
# CLI 的非零码会被管道吞掉，于是脚本"成功"跑完 0 个 bot —— 全部 bot 悄悄起不来。
out="$(python3 -m bots_registry --format=sh)" || {
  echo "bots_registry 执行失败，未启动任何 bot" >&2; exit 1; }
[ -n "$out" ] || { echo "bots_registry 没给出任何 bot，未启动任何 bot" >&2; exit 1; }

# 先把**全部** tg-* 会话停掉，再按注册表拉起（口径同 stop-bots.example.sh）。
# 只在下面的循环里逐个 kill 不够：`enabled: false` 的 bot 已经不在注册表里了，
# 循环压根轮不到它，它那个旧 dispatcher 会一直活着 —— 「停止」就等于没停。
# 放在注册表读成功之后：读不出名单时本脚本原样非零退出，不会白停一遍用户的 bot。
for s in $(tmux ls 2>/dev/null | grep -oE '^tg-[a-z0-9_]+' | sort -u || true); do
  tmux kill-session -t "$s" 2>/dev/null && echo "已停 $s" || true
done

# 用 here-doc 喂循环（不是管道）：循环体留在当前 shell，计数与 exit 才有效
while IFS="$TAB" read -r bot port ns; do
  [ -n "$bot" ] || continue
  chan="$CHANNELS/$bot"
  session="tg-$bot"

  # 读该 bot 的 token（.env 里 TELEGRAM_BOT_TOKEN=...）
  if [ ! -f "$chan/.env" ]; then
    echo "跳过 $bot：$chan/.env 不存在（先 cp .env.example .env 填 token）" >&2
    continue
  fi
  # 先 unset 再 source：`.env` 里没写 token 时，上一轮 bot 的值会留在环境里被这一轮拿走
  # → 两个 dispatcher 用同一个 token 长轮询，Telegram 只发给其中一个（409），消息随机丢。
  unset TELEGRAM_BOT_TOKEN
  # shellcheck disable=SC1091
  set -a; . "$chan/.env"; set +a
  # `set -u` 下直接引用未定义变量会整脚本崩掉，所以带 `:-` 判空后跳过这一个 bot
  [ -n "${TELEGRAM_BOT_TOKEN:-}" ] || {
    echo "跳过 $bot：$chan/.env 里缺 TELEGRAM_BOT_TOKEN" >&2; continue; }

  # 会话已在上面统一停过了，这里直接起
  # BOT_NAMESPACE 必须注进去：worker-manager.ts 是 env 优先，注了它新 bot 才认得自己的
  # 记忆命名空间（TS 侧那张表退化为 legacy 兜底，源码零改动）。空值 = TS 自动回落该表。
  tmux new-session -d -s "$session" \
    -e "CHANNEL_DIR=$chan" \
    -e "TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN" \
    -e "BOT_NAME=$bot" \
    -e "DISPATCHER_PORT=$port" \
    -e "BOT_NAMESPACE=$ns" \
    "bun $REPO_DIR/dispatcher/dispatcher.ts"
  echo "已启动 $bot（session=$session port=$port）"
done <<EOF
$out
EOF
