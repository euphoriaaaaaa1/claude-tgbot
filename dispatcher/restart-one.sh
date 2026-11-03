#!/usr/bin/env bash
# restart-one.sh <bot> — 只重启这一只 bot 的 dispatcher（连同它的 worker），别的 bot 不动。
# 管理台「重启」在主程序没响应时调用（adapters/claude_proc.run_restart_one），也可以手动跑。
#
# 拉起段与仓库根的 restart-bots.example.sh 同一写法（source 该 bot 的 .env、nohup bun、写 pid 文件），
# 改其中一边记得同步另一边。环境取调用方（管理台进程）的环境；用户在自己复制的 restart-bots.sh 里
# 额外加的变量不会带上，要带就写进该 bot 的 .env。
#
# 退出码：
#   0  已拉起
#   1  起不来：注册表读不出、缺 .env 或令牌、拉起后 0.7 秒内就退出
#   2  参数不对，或这只 bot 不在注册表里 / 已停用
#   3  旧进程停不掉，或停完端口仍有人在听（找不到合格 pid 时不盲杀、不重复起进程）
#
# 归属标记：拉起时在命令行末尾带 --bot=<bot>（restart-bots.example.sh、windows/start-bots.ps1 同样带），
# 停旧进程只认带本 bot 标记的本仓库 dispatcher。pid 文件指向别的 bot（过期 pid 被复用）或没有标记的旧进程时
# 一律不停，按「没在跑」处理，再由端口检查兜底：旧进程仍占着端口就退出 3。
set -u
set -o pipefail

CLAUDEBOT_REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=dispatcher/bot-enabled.sh
. "$CLAUDEBOT_REPO_DIR/dispatcher/bot-enabled.sh"
# voice-bridge 共享令牌，与 restart-bots.example.sh 相同
export VOICE_BRIDGE_TOKEN_FILE="${VOICE_BRIDGE_TOKEN_FILE:-$HOME/.claude/secrets/voice-bridge.token}"

TAB="$(printf '\t')"

# 端口上还有没有人在听（1 秒超时）。0 = 连得上（仍被占）；1 = 连不上。
_port_in_use() {
  _be_python_env
  "$PYTHON_BIN" - "$1" <<'PY'
import socket
import sys
s = socket.socket()
s.settimeout(1)
try:
    s.connect(("127.0.0.1", int(sys.argv[1])))
except (OSError, ValueError):
    sys.exit(1)
finally:
    s.close()
sys.exit(0)
PY
}

# bot 编号与适配器、路由同一条规则（adapters/base.py 的 valid_bot_id）：1 到 64 位，只含字母数字和 . _ -，
# 不能全是点。字符逐个列出、不用区间写法：区间在部分 locale 下会放进别的字符。
_valid_bot_id() {
  case "$1" in
    ''|*[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-]*) return 1 ;;
  esac
  [ "${#1}" -le 64 ] || return 1
  case "$1" in
    *[!.]*) return 0 ;;
  esac
  return 1
}

# pid 是否是本仓库、这一只 bot 的 dispatcher：命令行要以 "<仓库>/dispatcher/dispatcher.ts --bot=<bot>" 结尾，
# 或其后紧跟空格。编号已过 _valid_bot_id，不含通配符和空白。
_pid_is_bot_dispatcher() {
  local cmd
  cmd="$("$PS_CMD" -ww -o command= -p "$1" 2>/dev/null || true)"
  case "$cmd" in
    *"$REPO_DIR/dispatcher/dispatcher.ts --bot=$2"|*"$REPO_DIR/dispatcher/dispatcher.ts --bot=$2 "*) return 0 ;;
  esac
  return 1
}

restart_one() {
  local bot="$1" out rc line port ns chan log pidfile old pid i

  out="$(registry_lines)"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    # 名单为空（例如唯一一只也停用了）时 bots_registry 同样退出 1；JSON 输出恰好是 [] 才算空名单
    if [ "$rc" -eq 1 ] && [ -z "$out" ] && [ "$(registry_json 2>/dev/null)" = "[]" ]; then
      echo "$bot 不在注册表里或已停用，没有重启" >&2
      return 2
    fi
    echo "bots_registry 执行失败，没有重启" >&2
    return 1
  fi
  line="$(printf '%s\n' "$out" | awk -F'\t' -v b="$bot" '$1 == b { print; exit }')"
  if [ -z "$line" ]; then
    echo "$bot 不在注册表里或已停用，没有重启" >&2
    return 2
  fi
  IFS="$TAB" read -r _ port ns <<EOF
$line
EOF
  case "$port" in
    ''|*[!0-9]*) echo "$bot 没有可用的端口，没有重启" >&2; return 2 ;;
  esac

  chan="$(channel_dir_for "$bot")"
  log="$LOG_DIR/$bot-dispatcher.log"
  pidfile="$RUN_DIR/$bot.pid"

  # 先确认拉得起来再停旧进程：缺 .env 或令牌时不白停一遍
  if [ ! -f "$chan/.env" ]; then
    echo "$bot：$chan/.env 不存在，没有重启" >&2
    return 1
  fi
  unset TELEGRAM_BOT_TOKEN
  # shellcheck disable=SC1091
  set -a
  . "$chan/.env"
  set +a
  if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
    echo "$bot：$chan/.env 里缺 TELEGRAM_BOT_TOKEN，没有重启" >&2
    return 1
  fi

  # 只停 pid 文件记的、命令行确实是本仓库这一只 bot 的 dispatcher 的那一个进程树
  old=""
  if [ -f "$pidfile" ]; then
    old="$(cat "$pidfile" 2>/dev/null || true)"
    case "$old" in
      ''|*[!0-9]*) old="" ;;
    esac
    if [ -n "$old" ] && ! kill -0 "$old" 2>/dev/null; then
      old=""
    fi
    if [ -n "$old" ] && ! _pid_is_bot_dispatcher "$old" "$bot"; then
      if _pid_is_dispatcher "$old"; then
        echo "$bot 的 pid 文件记的 pid $old 是本仓库的 dispatcher，但不是 $bot 的（别的 bot 复用了这个 pid，或是没有归属标记的旧进程），不停它；旧进程若仍占着端口，跑一次 restart-bots.sh 整体重启" >&2
      fi
      old=""
    fi
  fi
  if [ -n "$old" ]; then
    _kill_tree "$old" TERM
    i=0
    while [ "$i" -lt 25 ] && kill -0 "$old" 2>/dev/null; do
      sleep 0.2
      i=$((i + 1))
    done
    if kill -0 "$old" 2>/dev/null; then
      echo "$bot 的 dispatcher pid=$old 未响应 SIGTERM，强制结束" >&2
      _kill_tree "$old" KILL
      sleep 0.5
    fi
    if kill -0 "$old" 2>/dev/null; then
      echo "错误：$bot 的 dispatcher pid=$old 无法停止" >&2
      return 3
    fi
    echo "已停 dispatcher $bot (pid $old)"
  fi
  rm -f "$pidfile"

  # 停完端口仍有人在听：可能是 pid 文件丢了的旧进程或别的程序，不盲杀，也不再起一个抢同一个令牌
  if _port_in_use "$port"; then
    echo "$bot 的端口 $port 仍被占用，没有拉起" >&2
    return 3
  fi

  mkdir -p "$RUN_DIR" "$LOG_DIR"
  # 子 shell + exec：$! 就是最终 bun 进程的 PID；nohup 让它脱离终端（同 restart-bots.example.sh）
  (
    cd "$REPO_DIR/dispatcher" || exit 1
    exec env \
      CHANNEL_DIR="$chan" \
      TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" \
      BOT_NAME="$bot" \
      DISPATCHER_PORT="$port" \
      BOT_NAMESPACE="$ns" \
      nohup "$BUN_BIN" "$REPO_DIR/dispatcher/dispatcher.ts" "--bot=$bot" >>"$log" 2>&1
  ) &
  pid=$!
  printf '%s\n' "$pid" > "$pidfile"

  sleep 0.7
  if kill -0 "$pid" 2>/dev/null; then
    echo "已启动 $bot (pid $pid, port $port, 日志 $log)"
    return 0
  fi
  echo "启动失败 $bot（见日志 $log）" >&2
  rm -f "$pidfile"
  return 1
}

if [ "$#" -ne 1 ]; then
  echo "用法：restart-one.sh <bot>" >&2
  exit 2
fi
# bot 编号会拼进路径和进程匹配：不合规则一律拒绝，报错里不回显原文
if ! _valid_bot_id "$1"; then
  echo "非法 bot 编号（只能是 1 到 64 位字母数字与 . _ -，且不能全是点），没有重启" >&2
  exit 2
fi

restart_one "$1" 2>&1 | scrub_token_stream
exit "${PIPESTATUS[0]}"
