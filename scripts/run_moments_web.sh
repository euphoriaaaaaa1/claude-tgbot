#!/bin/bash
# launchd wrapper: 启动 moments web 服务
# 用 bash wrapper 包一层，避免 launchd 直接 exec pyenv python 时 dyld 卡住
set -e
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
export MOMENTS_WEB_PORT="${MOMENTS_WEB_PORT:-8765}"
# 仓库位置按本脚本自身定位。写死 ~/claudebotlife 的话，仓库克隆在别处 = 服务起不来，
# 而 plist 的 WorkingDirectory 已经指对了也白搭（这里的 cd 会把它顶掉）。
cd "$(cd "$(dirname "$0")/.." && pwd)"
exec python3 -u -m moments.web
