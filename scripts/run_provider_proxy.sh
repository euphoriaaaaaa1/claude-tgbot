#!/bin/bash
# 8770 供应商代理启动器：先用现有清单生成 config.yaml，再 exec 外部 cli-proxy-api。
# 公开版不随附该二进制；未安装时明确报错退出，不影响其他功能。
set -eu

REPO_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$REPO_DIR"

# 1) 生成/更新 config.yaml。失败不阻塞启动：实例可能已有上一代配置；
#    真缺二进制的情况由下面统一给出固定错误。
"${PYTHON:-python3}" provider_proxy.py --sync || true

# 2) 依次查找二进制：环境变量 → 默认运行目录 → 旧目录。
BIN=""
for candidate in \
  "${CLAUDEBOT_CLI_PROXY_BIN:-}" \
  "${HOME:-}/Library/Application Support/claudebotlife-provider-proxy/cli-proxy-api" \
  "${HOME:-}/.claude-tgbot/cli-proxy-api"
do
  if [ -n "$candidate" ] && [ -x "$candidate" ]; then
    BIN=$candidate
    break
  fi
done

if [ -z "$BIN" ]; then
  echo "未安装 cli-proxy-api，8770 代理不可用；请按 README 放置二进制" >&2
  exit 1
fi

exec "$BIN"
