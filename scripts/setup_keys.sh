#!/usr/bin/env bash
# 三把必填密钥的"填哪、去哪拿、现在就填"向导。install.sh 第④步调用；也可以单独跑：
#   bash scripts/setup_keys.sh            # 交互：逐项说明 + 当场输入 + 写进文件
#   bash scripts/setup_keys.sh --check    # 只检查、不提问（CI / 非终端）
# 终端不是交互式（stdin 不是 TTY，比如被管道/脚本调用）时自动退化成只打说明。
# 写文件交给 scripts/setup_keys.py（YAML/.env/JSON 各按格式写，不用 sed 猜）。
set -e
cd "$(dirname "$0")/.."

BOT="${BOT:-chenlulu}"
FS="python3 scripts/setup_keys.py"
check_only=0; [ "${1:-}" = "--check" ] && check_only=1
interactive=1; { [ "$check_only" = 1 ] || [ ! -t 0 ]; } && interactive=0

ok()   { printf '  \033[1;32m✅ %s\033[0m\n' "$*"; }
todo() { printf '  \033[1;33m⚠️  %s\033[0m\n' "$*"; }
info() { printf '     %s\n' "$*"; }

status_json="$($FS status --bot "$BOT")"
has() { printf '%s' "$status_json" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('$1') else 1)"; }

# ask <提示> <校验正则> <格式说明> → 读一行到 REPLY；空回车 = 跳过（返回 1）
ask() {
  local prompt="$1" re="$2" fmt="$3"
  while :; do
    printf '     👉 %s（回车跳过，稍后再填）：' "$prompt"
    IFS= read -r REPLY || return 1
    REPLY="${REPLY//[$'\r\n']/}"; REPLY="${REPLY## }"; REPLY="${REPLY%% }"
    [ -z "$REPLY" ] && return 1
    if printf '%s' "$REPLY" | grep -qE "$re"; then return 0; fi
    todo "看起来不对：$fmt。再试一次或直接回车跳过"
  done
}

left=0

# ── ① Telegram bot token ───────────────────────────────────────────
if has token; then ok "bot token 已填（~/.claude/channels/$BOT/.env）"; else
  todo "缺 Telegram bot token —— 没有它 bot 收发不了消息"
  info "怎么拿：Telegram 里搜 @BotFather → 发 /newbot → 起个名字、再起个以 bot 结尾的用户名 → 它回你一串"
  info "        形如 123456789:AAxxxxxxxx 的东西，整串就是 token"
  info "填哪：~/.claude/channels/$BOT/.env 里的 TELEGRAM_BOT_TOKEN=（或下面直接输入）"
  if [ "$interactive" = 1 ] && ask "粘贴 bot token" '^[0-9]{6,}:[A-Za-z0-9_-]{25,}$' '形如 123456789:AAxxxxxxxx，冒号前是数字'; then
    $FS set-token "$REPLY" --bot "$BOT" && ok "已写入 ~/.claude/channels/$BOT/.env"
  else left=1; fi
fi

# ── ② 你自己的 Telegram user_id ─────────────────────────────────────
if has userid; then ok "允许私聊的 user_id 已填（~/.claude/channels/$BOT/access.json）"; else
  todo "缺你的 Telegram user_id —— 白名单空着，bot 收到消息也不理"
  info "怎么拿：Telegram 里搜 @userinfobot，随便发它一句，它回你的 Id: 一串数字（不是 @用户名）"
  info "填哪：~/.claude/channels/$BOT/access.json 的 allowFrom 数组（或下面直接输入）"
  if [ "$interactive" = 1 ] && ask "输入你的数字 user_id" '^[0-9]{5,}$' '纯数字，通常 8–10 位'; then
    $FS set-userid "$REPLY" --bot "$BOT" && ok "已写入 access.json 的 allowFrom"
  else left=1; fi
fi

# ── ③ DeepSeek API key ──────────────────────────────────────────────
if has deepseek; then ok "DeepSeek key 已填（configs/_global.yml）"; else
  todo "缺 DeepSeek key —— 私聊能跑，但情绪分/关系数值/群聊导演/主动消息不工作"
  info "怎么拿：浏览器开 platform.deepseek.com → 注册/登录 → 左侧 API keys → 创建 → 复制形如 sk-xxxx 的 key"
  info "填哪：configs/_global.yml 的 jiwen.delta_llm.api_key（或下面直接输入）"
  if [ "$interactive" = 1 ] && ask "粘贴 DeepSeek key" '^sk-[A-Za-z0-9_-]{16,}$' '以 sk- 开头的一串字母数字'; then
    $FS set-deepseek "$REPLY" && ok "已写入 configs/_global.yml"
  else left=1; fi
fi

if [ "$left" = 0 ]; then
  ok "三把密钥都齐了"
else
  echo
  info "还有 ⚠️ 没填的，随时补：bash scripts/setup_keys.sh（会再问一遍）；或者按上面「填哪」手动改文件"
fi
exit 0
