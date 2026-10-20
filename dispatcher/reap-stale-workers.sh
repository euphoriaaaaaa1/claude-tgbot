#!/usr/bin/env bash
# reap-stale-workers.sh [--dry-run]
#
# 清扫孤儿 worker-plugin 进程：worker-plugin.ts 正常时的父进程是它的 claude worker；
# worker 异常死亡后插件不一定退出，会空转占满 CPU。被收养到 init/launchd（ppid=1）
# 就说明父 worker 已死，安全杀。
#
# 只认 ppid=1，绝不误杀父进程还活着的正常插件。只杀命令行里带本仓库
# dispatcher/worker-plugin.ts 的进程。
#
# 退出码：0 = 扫描完成（包括没有孤儿）；1 = ps 不可用（无法判断，绝不假装成功）。
# 环境变量：REAP_PS_CMD（默认 ps）、REAP_KILL_CMD（默认 kill）、
#           REAP_PATTERN（默认本仓库 worker-plugin.ts 路径）、REAP_DRY_RUN=1。
set -u

REPO_DIR="${CLAUDEBOT_REPO_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
PS_CMD="${REAP_PS_CMD:-ps}"
KILL_CMD="${REAP_KILL_CMD:-kill}"
PATTERN="${REAP_PATTERN:-$REPO_DIR/dispatcher/worker-plugin.ts}"
DRY_RUN="${REAP_DRY_RUN:-0}"

case "${1:-}" in
  --dry-run) DRY_RUN=1 ;;
  "") ;;
  *) echo "用法：reap-stale-workers.sh [--dry-run]" >&2; exit 1 ;;
esac

if ! "$PS_CMD" -ww -eo pid=,ppid=,command= >/dev/null 2>&1; then
  echo "reap-stale-workers: ps 不可用，无法判断孤儿 worker-plugin" >&2
  exit 1
fi

# 一次 ps 同时拿 pid/ppid/命令行；只在管道里过滤，命令行不落日志。
"$PS_CMD" -ww -eo pid=,ppid=,command= 2>/dev/null |
while read -r pid ppid cmd; do
  [ "$ppid" = "1" ] || continue
  case "$cmd" in
    *"$PATTERN"*) ;;
    *) continue ;;
  esac
  if [ "$DRY_RUN" = 1 ]; then
    echo "would reap worker-plugin pid=$pid (ppid=1)"
  else
    echo "reap worker-plugin pid=$pid (ppid=1，父 worker 已死)"
    "$KILL_CMD" -9 "$pid" 2>/dev/null || true
  fi
done

exit 0
