#!/usr/bin/env bash
# 复制为 restart-bots.sh。逐个 bot 重启 dispatcher（长轮询 + HTTP 服务）。
# bot 名单/端口/命名空间全部来自注册表（configs/*.yml），**加 bot 不用再改本文件**。
#
# 进程模型：每个 bot 一个后台 bun 进程，pid 写 <RUN_DIR>/<bot>.pid，日志写
# <LOG_DIR>/<bot>-dispatcher.log。不使用 tmux/launchd；停止时按 pid + 命令行
# 扫描本仓库的 dispatcher.ts（已停用 bot 的旧进程也会被停掉）。
set -u

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDEBOT_REPO_DIR="$REPO_DIR"
# voice-bridge 共享令牌（install.sh 生成）。dispatcher 调 /send_voice、/transcribe_* 时带上。
export VOICE_BRIDGE_TOKEN_FILE="${VOICE_BRIDGE_TOKEN_FILE:-$HOME/.claude/secrets/voice-bridge.token}"
# shellcheck source=dispatcher/bot-enabled.sh
. "$REPO_DIR/dispatcher/bot-enabled.sh"

TAB="$(printf '\t')"

# 注册表读不出来就整体失败：`cmd | while read` 的退出状态是 while 的，
# CLI 的非零码会被管道吞掉，于是脚本"成功"跑完 0 个 bot —— 全部 bot 悄悄起不来。
PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
out="$(python3 -m bots_registry --format=sh)" || {
  echo "bots_registry 执行失败，未启动任何 bot" >&2
  exit 1
}
[ -n "$out" ] || {
  echo "bots_registry 没给出任何 bot（全部停用时也会这样），未启动任何 bot" >&2
  exit 1
}

# 先停全部旧 dispatcher，再按注册表拉起。停旧进程必须排在"注册表读成功"之后：
# 读不出名单时本脚本原样非零退出，不会白停一遍用户的 bot。
# 只按注册表循环 kill 不够：enabled:false 的 bot 已经不在名单里，旧进程会一直活着。
if ! stop_all_dispatchers; then
  echo "停止旧 dispatcher 失败，未启动任何 bot" >&2
  exit 1
fi

mkdir -p "$RUN_DIR" "$LOG_DIR"
failed=0

# 用 here-doc 喂循环（不是管道）：循环体留在当前 shell，计数与 exit 才有效。
while IFS="$TAB" read -r bot port ns; do
  [ -n "$bot" ] || continue
  chan="$(channel_dir_for "$bot")"
  log="$LOG_DIR/$bot-dispatcher.log"

  if [ ! -f "$chan/.env" ]; then
    echo "跳过 $bot：$chan/.env 不存在（先 cp .env.example .env 填 token）" >&2
    failed=1
    continue
  fi

  # 先 unset 再 source：`.env` 里没写 token 时，上一轮 bot 的值会留在环境里被这一轮拿走
  # → 两个 dispatcher 用同一个 token 长轮询（409），消息随机丢。
  unset TELEGRAM_BOT_TOKEN
  # shellcheck disable=SC1091
  set -a
  . "$chan/.env"
  set +a
  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
    echo "跳过 $bot：$chan/.env 里缺 TELEGRAM_BOT_TOKEN" >&2
    failed=1
    continue
  fi

  # 子 shell + exec：$! 就是最终 bun 进程的 PID；nohup 让它脱离终端。
  # BOT_NAMESPACE 必须注进去：worker-manager.ts 是 env 优先，注了它新 bot 才认得
  # 自己的记忆命名空间（TS 侧那张表退化为 legacy 兜底，源码零改动）。
  (
    cd "$REPO_DIR/dispatcher" || exit 1
    exec env \
      CHANNEL_DIR="$chan" \
      TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" \
      BOT_NAME="$bot" \
      DISPATCHER_PORT="$port" \
      BOT_NAMESPACE="$ns" \
      nohup "$BUN_BIN" "$REPO_DIR/dispatcher/dispatcher.ts" >>"$log" 2>&1
  ) &
  pid=$!
  printf '%s\n' "$pid" > "$RUN_DIR/$bot.pid"

  # 启动失败（缺 bun、token 格式错、端口被占等）不要报"已启动"。
  sleep 0.7
  if kill -0 "$pid" 2>/dev/null; then
    echo "已启动 $bot (pid $pid, port $port, 日志 $log)"
  else
    echo "启动失败 $bot（见日志 $log）" >&2
    rm -f "$RUN_DIR/$bot.pid"
    failed=1
  fi
done <<EOF
$out
EOF

exit "$failed"
