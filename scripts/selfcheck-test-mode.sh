#!/bin/bash
# 测试模式自检（INTERFACE-desync §11.3，公开版）：每次跑测试前先跑，红即停，后续测试不许跑。
# 用法：CLAUDEBOTLIFE_TEST=1 CLAUDEBOTLIFE_TEST_ROOT=<已存在的目录> bash scripts/selfcheck-test-mode.sh
# 私有版的 (a) bot-enabled.sh、(b) bot_stop.py 两项在公开仓没有对应文件，这里只做 (c)：
#   dispatcher/chat_guard.ts 的 testModeCheck 对 http://127.0.0.1:17802 → ok=false, reason="forbidden_port"（只解析字符串，不连端口）
# 生产会话数的跑前/跑后核对要调真实外部命令，不放在本脚本里，由操作者在自己终端做。
# 定位注入：CHAT_GUARD_TS（默认本仓 dispatcher/chat_guard.ts）。
set -u
here=$(cd "$(dirname "$0")" && pwd)
: "${CHAT_GUARD_TS:=$here/../dispatcher/chat_guard.ts}"
root="${CLAUDEBOTLIFE_TEST_ROOT:-}"
if [ "${CLAUDEBOTLIFE_TEST:-}" != 1 ] || [ ! -d "$root" ]; then
  echo "selfcheck FAILED: 需要 CLAUDEBOTLIFE_TEST=1 且 CLAUDEBOTLIFE_TEST_ROOT 为已存在目录" >&2
  exit 1
fi

out=$(CG="$CHAT_GUARD_TS" ROOT="$root" bun -e '
const { testModeCheck } = await import(process.env.CG)
const r = testModeCheck({ CLAUDEBOTLIFE_TEST: "1", CLAUDEBOTLIFE_TEST_ROOT: process.env.ROOT,
  HOME: process.env.ROOT + "/selfcheck-home", CHANNEL_DIR: process.env.ROOT + "/selfcheck-home/channel",
  TELEGRAM_DISPATCHER_URL: "http://127.0.0.1:17802" })
console.log(JSON.stringify(r))
process.exit(r && r.ok === false && r.reason === "forbidden_port" ? 0 : 1)
' 2>&1)
if [ $? = 0 ]; then
  echo "ok (c) chat_guard: $out"
  echo "selfcheck OK"
  exit 0
fi
echo "selfcheck FAILED: (c) 期望 forbidden_port，实际: $(printf '%s\n' "$out" | tail -1)" >&2
exit 1
