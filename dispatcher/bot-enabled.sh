# shellcheck shell=bash
# bot-enabled.sh — 供 start/stop/restart 脚本 source 的公共 shell 运行时。
#
# 只做四件事，全部跨平台（macOS / Linux）：
#   1. 注册表读取（唯一名单来源：configs/*.yml 经 bots_registry）
#   2. 通道目录 / 运行目录 / 日志目录的默认值（全部可用环境变量覆盖）
#   3. dispatcher 进程树的停止（pid 文件 + 命令行扫描，不依赖 tmux/launchd）
#   4. 日志打码（Telegram 令牌形状绝不落盘）
#
# 本文件被 source 时不打印任何东西，也不改调用方的 shell 选项。

# 仓库根目录：调用方可以显式设置 CLAUDEBOT_REPO_DIR；否则由本文件位置
# （<repo>/dispatcher/bot-enabled.sh）推导。
if [ -z "${CLAUDEBOT_REPO_DIR:-}" ]; then
  # 不带数组下标的 BASH_SOURCE 在 bash 里就是本文件路径；dash 里未定义时退回 $0。
  _be_self="${BASH_SOURCE:-$0}"
  CLAUDEBOT_REPO_DIR="$(cd "$(dirname "$_be_self")/.." && pwd)"
fi
REPO_DIR="$CLAUDEBOT_REPO_DIR"

# 通道根目录：CLAUDEBOT_CHANNELS_ROOT 优先，兼容公开版既有的 HUB_CHANNELS_DIR。
CHANNELS_ROOT="${CLAUDEBOT_CHANNELS_ROOT:-${HUB_CHANNELS_DIR:-$HOME/.claude/channels}}"
# 运行期文件（pid）与日志都放 ~/.claude-tgbot/ 下，和安装契约一致。
RUN_DIR="${CLAUDEBOT_RUN_DIR:-$HOME/.claude-tgbot/run}"
LOG_DIR="${CLAUDEBOT_LOG_DIR:-$HOME/.claude-tgbot/logs}"

# 测试/运维接缝：默认命令都能被整体替换，脚本不写死绝对路径。
PYTHON_BIN="${CLAUDEBOT_PYTHON:-python3}"
PS_CMD="${CLAUDEBOT_PS:-ps}"
if [ -z "${CLAUDEBOT_BUN:-}" ]; then
  if command -v bun >/dev/null 2>&1; then
    BUN_BIN="$(command -v bun)"
  elif [ -x "$HOME/.bun/bin/bun" ]; then
    # launchd/systemd 的 PATH 往往不含 ~/.bun/bin
    BUN_BIN="$HOME/.bun/bin/bun"
  else
    BUN_BIN="bun"
  fi
else
  BUN_BIN="$CLAUDEBOT_BUN"
fi

# 让 `python3 -m bots_registry` 在任何 cwd 下都能 import 到仓库模块。
_be_python_env() {
  PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}"
  export PYTHONPATH
}

# 注册表原始行（<id>\t<port>\t<namespace_uuid>）。退出码原样返回；
# 调用方必须自己判 0 与空输出，绝不把空名单当成功。
registry_lines() {
  _be_python_env
  "$PYTHON_BIN" -m bots_registry --format=sh
}

registry_json() {
  _be_python_env
  "$PYTHON_BIN" -m bots_registry --format=json
}

# bot 是否被停用（enabled:false 或旧 .disabled 标记）。只读、fail-open。
# 返回 0 = 停用；1 = 启用/判断不了。
bot_stopped() {
  _be_python_env
  "$PYTHON_BIN" - "$1" <<'PY'
import sys
import bots_registry
sys.exit(0 if sys.argv[1] in bots_registry.disabled_ids_safe() else 1)
PY
}

channel_dir_for() {
  printf '%s/%s' "$CHANNELS_ROOT" "$1"
}

# 令牌形状打码。日志落盘前必须过这一层。
scrub_token_stream() {
  sed -E 's/[0-9]{5,12}:[A-Za-z0-9_-]{30,}/<telegram-token-hidden>/g'
}

# 子进程 PID（macOS/Linux 都有 pgrep；没有时退化为只杀父进程）。
_children_of() {
  local pid="$1"
  pgrep -P "$pid" 2>/dev/null || true
}

# 递归杀进程树：先子后父，避免 dispatcher 退出后 worker 变成孤儿。
_kill_tree() {
  local pid="$1" sig="${2:-TERM}" child
  for child in $(_children_of "$pid"); do
    _kill_tree "$child" "$sig"
  done
  kill -s "$sig" "$pid" 2>/dev/null || true
}

# pid 是否确实是本仓库的 dispatcher（防重启后 pid 复用误杀无关进程）。
_pid_is_dispatcher() {
  local cmd
  cmd="$("$PS_CMD" -ww -o command= -p "$1" 2>/dev/null || true)"
  case "$cmd" in
    *"$REPO_DIR/dispatcher/dispatcher.ts"*) return 0 ;;
  esac
  return 1
}

# 本仓库 dispatcher 的 PID：
#   a) start 脚本写下的 <RUN_DIR>/<bot>.pid（校验命令行后再信）
#   b) 命令行里带本仓库 dispatcher.ts 的 bun 进程（覆盖手工启动/旧 pid 丢失）
# 输出每行一个 PID，可能重复，调用方用 sort -u 去重。
dispatcher_pids() {
  local f pid cmd
  if [ -d "$RUN_DIR" ]; then
    for f in "$RUN_DIR"/*.pid; do
      [ -f "$f" ] || continue
      pid="$(cat "$f" 2>/dev/null || true)"
      case "$pid" in
        ''|*[!0-9]*) continue ;;
      esac
      if kill -0 "$pid" 2>/dev/null && _pid_is_dispatcher "$pid"; then
        printf '%s\n' "$pid"
      fi
    done
  fi
  "$PS_CMD" -ww -eo pid=,command= 2>/dev/null | while read -r pid cmd; do
    case "$cmd" in
      *"$REPO_DIR/dispatcher/dispatcher.ts"*) ;;
      *) continue ;;
    esac
    case "$cmd" in
      *bun*) printf '%s\n' "$pid" ;;
    esac
  done
}

# 停掉本仓库全部 dispatcher（包含已停用 bot 的旧进程：它们不在注册表里，
# 只按注册表循环永远轮不到）。返回 0 = 全部已停；1 = 仍有进程杀不掉。
stop_all_dispatchers() {
  local pids pid f bot left=0 alive=0 i=0 mapping="" entry
  pids="$(dispatcher_pids | sort -u)"
  if [ -z "$pids" ]; then
    rm -f "$RUN_DIR"/*.pid 2>/dev/null || true
    echo "没有正在运行的 dispatcher。"
    return 0
  fi

  # 先记下 pid → bot，停完再输出，避免把变量改乱。
  if [ -d "$RUN_DIR" ]; then
    for f in "$RUN_DIR"/*.pid; do
      [ -f "$f" ] || continue
      pid="$(cat "$f" 2>/dev/null || true)"
      case "$pid" in
        ''|*[!0-9]*) continue ;;
      esac
      mapping="$mapping $pid:$(basename "$f" .pid)"
    done
  fi

  for pid in $pids; do
    _kill_tree "$pid" TERM
  done

  # 最多等 5 秒优雅退出；还在的补 SIGKILL。
  while [ "$i" -lt 25 ]; do
    alive=0
    for pid in $pids; do
      if kill -0 "$pid" 2>/dev/null; then alive=1; fi
    done
    [ "$alive" = 0 ] && break
    sleep 0.2
    i=$((i + 1))
  done
  for pid in $pids; do
    if kill -0 "$pid" 2>/dev/null; then
      echo "dispatcher pid=$pid 未响应 SIGTERM，强制结束" >&2
      _kill_tree "$pid" KILL
    fi
  done

  # 再确认一次；仍活着的进程如实报错，不静默成功。
  for pid in $pids; do
    if kill -0 "$pid" 2>/dev/null; then
      echo "错误：dispatcher pid=$pid 无法停止" >&2
      left=1
    else
      bot=""
      for entry in $mapping; do
        case "$entry" in
          "$pid":*) bot="${entry#*:}" ;;
        esac
      done
      if [ -n "$bot" ]; then
        echo "已停 dispatcher $bot (pid $pid)"
      else
        echo "已停 dispatcher pid=$pid"
      fi
    fi
  done
  rm -f "$RUN_DIR"/*.pid 2>/dev/null || true
  return "$left"
}
