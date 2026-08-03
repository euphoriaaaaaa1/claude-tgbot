#!/usr/bin/env bash
# claude-tgbot 一键安装（Mac / Linux）。
# 幂等：重复运行安全，绝不覆盖你已填好的配置。
# 自动完成 README 部署 ②~⑥ 里一切能自动的事；密钥没法替你填，
# 跑完会列出「还差哪几个要手填」的清单，全填完再跑一次本脚本即可看到全绿。
set -e
cd "$(dirname "$0")"

say()  { printf '\033[1;36m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[1;32m✅ %s\033[0m\n' "$*"; }
todo() { printf '  \033[1;33m⚠️  %s\033[0m\n' "$*"; }

# ── ① 前置依赖检查 ─────────────────────────────────────────
say "① 检查前置依赖"
missing=0
command -v python3 >/dev/null || { todo "缺 Python 3 → https://www.python.org/downloads/"; missing=1; }
command -v bun     >/dev/null || { todo "缺 Bun → https://bun.sh （curl -fsSL https://bun.sh/install | bash）"; missing=1; }
command -v claude  >/dev/null || { todo "缺 Claude Code CLI → npm install -g @anthropic-ai/claude-code，装完跑一次 claude 登录"; missing=1; }
[ "$missing" = 1 ] && { echo; echo "先装齐上面缺的，再重新跑 bash install.sh"; exit 1; }
ok "python3 / bun / claude 都在"

# ── ② 装依赖 ──────────────────────────────────────────────
say "② 安装 Python + JS 依赖"
python3 -m pip install -q -r requirements.txt
( cd dispatcher && bun install --silent )
ok "依赖装好"

# ── ③~⑥ 铺配置文件（只在不存在时复制，已有的绝不动）────────
say "③ 铺配置文件"
[ -f configs/_global.yml ] || cp configs/_global.example.yml configs/_global.yml
mkdir -p "$HOME/.claude/channels"
[ -d "$HOME/.claude/channels/chenlulu" ] || cp -r channels/chenlulu "$HOME/.claude/channels/"
[ -f "$HOME/.claude/channels/chenlulu/.env" ] || cp "$HOME/.claude/channels/chenlulu/.env.example" "$HOME/.claude/channels/chenlulu/.env"
[ -f restart-bots.sh ] || cp restart-bots.example.sh restart-bots.sh
ok "configs/_global.yml、~/.claude/channels/chenlulu/、restart-bots.sh 就位"

# ── 手填项体检 ────────────────────────────────────────────
say "④ 还需要你手填的密钥（脚本没法替你申请）"
left=0
if grep -q "YOUR_DEEPSEEK_API_KEY" configs/_global.yml 2>/dev/null; then
  todo "configs/_global.yml → jiwen.delta_llm.api_key 换成你的 DeepSeek key"; left=1
else ok "_global.yml 的 api_key 已填"; fi
if grep -qE "^TELEGRAM_BOT_TOKEN=\s*$|your-telegram-bot-token|YOUR" "$HOME/.claude/channels/chenlulu/.env" 2>/dev/null; then
  todo "~/.claude/channels/chenlulu/.env → TELEGRAM_BOT_TOKEN 填 @BotFather 给你的 token"; left=1
else ok "chenlulu/.env 的 bot token 已填"; fi
if grep -q "YOUR_TELEGRAM_USER_ID" "$HOME/.claude/channels/chenlulu/access.json" 2>/dev/null; then
  todo "~/.claude/channels/chenlulu/access.json → allowFrom 填你的数字 user_id（@userinfobot 可查）"; left=1
else ok "access.json 的白名单已填"; fi

echo
if [ "$left" = 0 ]; then
  say "全部就绪！启动："
  echo "  bash restart-bots.sh"
  echo "然后在 Telegram 给你的 bot 发条消息试试。强烈建议改成自己的人设（README「换成你自己的人设」）。"
else
  say "把上面 ⚠️ 的几项填完，然后："
  echo "  bash restart-bots.sh"
  echo "（想复查可以再跑一次 bash install.sh，全 ✅ 即就绪。可选的网页打电话模块见 voicecall/README.md）"
fi
