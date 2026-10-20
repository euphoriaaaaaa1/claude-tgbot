#!/bin/bash
# 手动启动 8767 管理台的 shell wrapper（旧 launchd 模板仍指向本文件）。
# 推荐优先用：python3 platform_services.py register provider-web --repo "$PWD"
set -e
export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin
export MOMENTS_WEB_MODE=provider
export MOMENTS_WEB_PORT="${MOMENTS_WEB_PORT:-8767}"
# 仓库位置按本脚本自身定位。写死 ~/claudebotlife 的话，仓库克隆在别处 = 服务起不来，
# 而 plist 的 WorkingDirectory 已经指对了也白搭（这里的 cd 会把它顶掉）。
cd "$(cd "$(dirname "$0")/.." && pwd)"
exec python3 -u -m moments.web
