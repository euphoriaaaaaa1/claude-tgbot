#!/usr/bin/env bash
# 复制为 stop-bots.sh。停掉本仓库所有 bot 的 dispatcher（连带它的 worker 子进程一起走）。
# 不需要注册表：全部 bot 停用时也要能停掉遗留进程（停用 bot 不在名单里）。
set -u

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDEBOT_REPO_DIR="$REPO_DIR"
# shellcheck source=dispatcher/bot-enabled.sh
. "$REPO_DIR/dispatcher/bot-enabled.sh"

stop_all_dispatchers
rc=$?
if [ "$rc" = 0 ]; then
  echo "全部 bot 已停。"
else
  echo "有 dispatcher 未能停止，见上面的错误。" >&2
fi
exit "$rc"
