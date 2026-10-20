#!/usr/bin/env bash
# worker-idle-wait.sh <bot> [max_sec=120]
#
# 重启/清理 worker 之前先等它把当前这一轮回复做完的"只读探针"：读
# <CHANNELS_ROOT>/<bot>/.worker-state.json（worker-plugin.ts 维护），忙则轮询到空闲
# 或等满上限。
#
# 退出码：0 = 可以杀（不忙 / 状态陈旧 / 无状态文件 / 读不出 / bot 目录不存在）；
#         1 = 等满 max_sec 仍忙（调用方照旧杀，stderr 那行 idle_wait_timeout 即证据）。
# 只读：绝不 kill、绝不写任何文件。
#
# 环境变量：CLAUDEBOT_CHANNELS_ROOT / HUB_CHANNELS_DIR（默认 ~/.claude/channels）；
#           IDLE_WAIT_POLL_MS（默认 2000，供测试加速）；IDLE_WAIT_PY（默认 python3）。
set -u

BOT="${1:-}"
MAX_SEC="${2:-120}"
CHANNELS_ROOT="${CLAUDEBOT_CHANNELS_ROOT:-${HUB_CHANNELS_DIR:-$HOME/.claude/channels}}"
POLL_MS="${IDLE_WAIT_POLL_MS:-2000}"
STALE_MS=$((15 * 60 * 1000))     # 状态超过 15 分钟没更新 → 视为陈旧，不阻塞
PY="${IDLE_WAIT_PY:-python3}"

# 数值兜底：非纯数字 → 回默认；轮询间隔必须 ≥1ms（绝不空转/卡死）
case "$MAX_SEC" in ''|*[!0-9]*) MAX_SEC=120 ;; esac
case "$POLL_MS" in ''|*[!0-9]*) POLL_MS=2000 ;; esac
[ "$POLL_MS" -ge 1 ] || POLL_MS=2000

# bot 目录解析：<CHANNELS_ROOT>/<bot>。解析不出目录 → 可杀（退 0）。
BOT_DIR="$CHANNELS_ROOT/$BOT"
STATE_FILE="$BOT_DIR/.worker-state.json"

now_ms() { "$PY" -c 'import time; print(int(time.time()*1000))'; }

# 只读解析状态：打印 "<busy:0|1> <at_ms>"（唯一读取点）。
# 读不出 / 非对象 / v!=1 / busy 非布尔 / at 非数字 → 一律 "0 0"（按不忙，不得阻塞重启）。
read_busy() {
  "$PY" - "$STATE_FILE" <<'PY'
import json, sys
try:
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        d = json.load(fh)
except Exception:
    print("0 0"); sys.exit(0)
if not isinstance(d, dict) or d.get("v") != 1:
    print("0 0"); sys.exit(0)
busy, at = d.get("busy"), d.get("at")
if not isinstance(busy, bool) or isinstance(at, bool) or not isinstance(at, (int, float)):
    print("0 0"); sys.exit(0)
print(("1" if busy else "0") + " " + str(int(at)))
PY
}

# 返回 0 = 可以杀（不忙 / 陈旧 / 读不出 / 时钟回拨按刚到）；返回 1 = 忙且新鲜。
is_ready() {
  local busy at now diff
  read -r busy at < <(read_busy)
  [ "$busy" = 1 ] || return 0
  now=$(now_ms)
  diff=$((now - at))
  [ "$diff" -lt 0 ] && diff=0                  # 时钟回拨：当作刚到，不误判陈旧
  [ "$diff" -gt "$STALE_MS" ] && return 0
  return 1
}

[ -d "$BOT_DIR" ] || exit 0                     # bot 目录解析不出 → 可杀
is_ready && exit 0

# 忙：轮询等待，循环受 MAX_SEC 硬约束（自身有界，不依赖调用方 timeout）
deadline=$(( $(now_ms) + MAX_SEC * 1000 ))
n=0
while :; do
  now=$(now_ms)
  if [ "$now" -ge "$deadline" ]; then
    echo "idle_wait_timeout bot=$BOT" >&2
    exit 1
  fi
  n=$((n + 1))
  echo "idle_wait_poll n=$n" >&2
  sleep "$(printf '%d.%03d' $((POLL_MS / 1000)) $((POLL_MS % 1000)))"
  is_ready && exit 0
done
