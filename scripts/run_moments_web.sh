#!/bin/bash
# launchd wrapper: 启动 moments web 服务
# 用 bash wrapper 包一层，避免 launchd 直接 exec pyenv python 时 dyld 卡住
set -e
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
export MOMENTS_WEB_PORT="${MOMENTS_WEB_PORT:-8765}"
cd $HOME/claudebotlife
exec python3 -u -m moments.web
