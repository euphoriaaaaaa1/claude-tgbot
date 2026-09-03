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
command -v python3 >/dev/null || { todo "缺 Python 3 → 去 python.org 的 Downloads 页装"; missing=1; }
command -v bun     >/dev/null || { todo "缺 Bun → 照 bun.sh 官网首页给的那行安装命令装"; missing=1; }
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

# ── ⑤ 注册开机自启 ────────────────────────────────────────
# 幂等：每次都重新渲染 plist、先 bootout 再 bootstrap，重复跑结果一致。
# 任何一步失败只 warn 不中断安装（比如 SSH 里没有图形会话，bootstrap 会拒）。
say "⑤ 注册开机自启"
AUTOSTART_LABEL="com.claudebotlife.autostart"
AUTOSTART_PLIST="$HOME/Library/LaunchAgents/$AUTOSTART_LABEL.plist"
autostart_on=0
if [ "$(uname -s)" != "Darwin" ]; then
  todo "非 macOS：没有 launchd，想开机自启请自己挂 cron（@reboot bash $PWD/restart-bots.sh）或 systemd user service"
else
  tmp_plist="$(mktemp "${TMPDIR:-/tmp}/claudebotlife-autostart.XXXXXX")"
  # launchd 不展开 ~，模板里的 ~/.bun/bin 必须换成真实家目录，否则非 homebrew 装的 bun 找不到
  sed -e "s#{REPO_DIR}#$PWD#g" -e "s#~/.bun/bin#$HOME/.bun/bin#g" \
      plist-templates/autostart.plist.tmpl > "$tmp_plist"
  if plutil -lint "$tmp_plist" >/dev/null 2>&1; then
    mkdir -p "$HOME/Library/LaunchAgents"
    mv -f "$tmp_plist" "$AUTOSTART_PLIST"
    launchctl bootout "gui/$UID/$AUTOSTART_LABEL" >/dev/null 2>&1 || true
    if launchctl bootstrap "gui/$UID" "$AUTOSTART_PLIST" >/dev/null 2>&1; then
      ok "开机自启已注册（${AUTOSTART_LABEL}）"
      autostart_on=1
    else
      todo "plist 已写好但没加载上（常见于 SSH，没有图形会话）。图形登录后手动跑：launchctl bootstrap gui/\$UID $AUTOSTART_PLIST"
    fi
  else
    rm -f "$tmp_plist"
    todo "自启 plist 渲染后格式校验没过，已跳过（不影响手动 bash restart-bots.sh）"
  fi
  # 老版 README 手动挂过的自启，标签不同不会被上面覆盖 → 会起两次，提醒删掉
  old_plist="$HOME/Library/LaunchAgents/com.example.claudebotlife-autostart.plist"
  if [ -f "$old_plist" ]; then
    todo "检测到旧版自启，删掉免得开机起两次：launchctl bootout gui/\$UID/com.example.claudebotlife-autostart; rm $old_plist"
  fi
fi

# ── ⑥ provider 管理台的后端代理 cliproxy（可选组件）────────
# 装不上不阻断整体安装：bot 本体不依赖它，只是管理台的 provider 页会显示"未配置"。
say "⑥ 装 provider 管理台后端（cliproxy，约 20 MB 下载）"
if [ "${SKIP_CLIPROXY:-0}" = "1" ]; then
  todo "SKIP_CLIPROXY=1，已跳过。想装：bash scripts/install_cliproxy.sh"
elif bash scripts/install_cliproxy.sh; then
  # 体检：常驻单元的 WorkingDirectory 必须钉在 ~/.claude-tgbot/cliproxy。
  # cliproxy v7 启动会往**进程 CWD** 下载 2.7 MB 的 static/management.html，
  # 没钉住就落进这个代码仓库（还会跟着 git status 一起碍眼）。
  cliproxy_dir="$(python3 scripts/hub_bootstrap.py get dir 2>/dev/null || true)"
  unit="$HOME/Library/LaunchAgents/com.claudebotlife.cliproxy.plist"
  [ -f "$unit" ] || unit="$HOME/.config/systemd/user/claudebotlife-cliproxy.service"
  if [ -n "$cliproxy_dir" ] && [ -f "$unit" ] && ! grep -q "$cliproxy_dir" "$unit"; then
    todo "常驻单元 $unit 的 WorkingDirectory 没指向 ${cliproxy_dir}，重跑：bash scripts/install_cliproxy.sh"
  else
    ok "cliproxy 就绪，工作目录钉在 $cliproxy_dir"
  fi
  if [ -d static ]; then
    todo "仓库根多了个 static/：那是 cliproxy 拉的管理面板，说明有进程没钉住工作目录，可直接删"
  fi
else
  todo "cliproxy 没装上（不影响 bot 本体）。重试：bash scripts/install_cliproxy.sh"
fi

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
if [ "$autostart_on" = 1 ]; then
  echo "以后开机自动起 bot，不想要就卸掉："
  echo "  launchctl bootout gui/\$UID/$AUTOSTART_LABEL; rm $AUTOSTART_PLIST"
fi
