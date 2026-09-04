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

# 还差几项要人手补。从 ① 起就可能被置 1，结尾据它决定报不报"全部就绪"。
left=0

# 渲染好的 plist 装进 ~/Library/LaunchAgents 并加载。
# 返回 0=已加载 / 1=写好了但没加载上（典型：SSH 里没有图形会话）/ 2=格式校验没过（已丢弃）
load_agent() {
  _label="$1"; _tmp="$2"; _dest="$HOME/Library/LaunchAgents/$_label.plist"
  plutil -lint "$_tmp" >/dev/null 2>&1 || { rm -f "$_tmp"; return 2; }
  mkdir -p "$HOME/Library/LaunchAgents"
  mv -f "$_tmp" "$_dest"
  launchctl bootout "gui/$UID/$_label" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$UID" "$_dest" >/dev/null 2>&1 || return 1
  return 0
}

# ── ① 前置依赖检查 ─────────────────────────────────────────
say "① 检查前置依赖"
missing=0
command -v python3 >/dev/null || { todo "缺 Python 3 → 去 python.org 的 Downloads 页装"; missing=1; }
command -v bun     >/dev/null || { todo "缺 Bun → 照 bun.sh 官网首页给的那行安装命令装"; missing=1; }
command -v claude  >/dev/null || { todo "缺 Claude Code CLI → npm install -g @anthropic-ai/claude-code，装完跑一次 claude 登录"; missing=1; }
[ "$missing" = 1 ] && { echo; echo "先装齐上面缺的，再重新跑 bash install.sh"; exit 1; }
ok "python3 / bun / claude 都在"
# tmux 不阻断安装，但 restart-bots.sh 是靠它常驻 dispatcher 的：没有它一个 bot 都起不来
command -v tmux >/dev/null || {
  todo "缺 tmux（restart-bots.sh 用它常驻每个 bot 的 dispatcher，没它起不来）→ brew install tmux（Linux：apt install tmux）"; left=1; }

# ── ② 装依赖 ──────────────────────────────────────────────
say "② 安装 Python + JS 依赖"
# PEP 668：Homebrew / Debian 等发行版的 python3 带 EXTERNALLY-MANAGED 标记，禁止往系统
# site-packages 装包，`pip install` 直接报 externally-managed-environment（另一台 Mac 实测）。
# 本项目十几处入口（launchd plist、restart-bots.sh、moments web、hub 脚本）都直接调 `python3`、
# 不走 venv，所以装到**用户 site**（~/Library/Python/3.x 或 ~/.local）：同一个 python3 照样
# import 得到，也不碰发行版自己的目录。判据用标记文件本身，不靠捕获报错文案（各版本文案不同）。
pip_flags="-q"
if python3 -c 'import os,sys,sysconfig; sys.exit(0 if os.path.exists(os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED")) else 1)'; then
  pip_flags="$pip_flags --user --break-system-packages --no-warn-script-location"
fi
python3 -m pip --version >/dev/null 2>&1 || python3 -m ensurepip --user >/dev/null 2>&1 || true
python3 -m pip install $pip_flags -r requirements.txt
# 装完立刻验 import：装进了另一个解释器的 site（pyenv/conda 混装机器）会在这里露馅，而不是 bot 起不来时。
python3 -c 'import yaml, feedparser, requests, flask' 2>/dev/null || {
  todo "依赖装了但当前 python3 import 不到（多半是 PATH 上的 python3 与 pip 用的不是同一个解释器）→ 看 which -a python3，把要用的那个排到前面再跑一次"; exit 1; }
( cd dispatcher && bun install --silent )
ok "依赖装好"

# ── ③~⑥ 铺配置文件（只在不存在时复制，已有的绝不动）────────
say "③ 铺配置文件"
[ -f configs/_global.yml ] || cp configs/_global.example.yml configs/_global.yml
mkdir -p "$HOME/.claude/channels"
[ -d "$HOME/.claude/channels/chenlulu" ] || cp -r channels/chenlulu "$HOME/.claude/channels/"
[ -f "$HOME/.claude/channels/chenlulu/.env" ] || cp "$HOME/.claude/channels/chenlulu/.env.example" "$HOME/.claude/channels/chenlulu/.env"
[ -f restart-bots.sh ] || cp restart-bots.example.sh restart-bots.sh
# 上面那行不覆盖已存在的文件，所以老机器上可能留着旧版硬编码 BOTS 名单的 restart-bots.sh：
# 它不读注册表 → 新加的 bot 永远起不来，而重启脚本本身照样"成功"退出。
grep -q bots_registry restart-bots.sh 2>/dev/null || {
  todo "restart-bots.sh 是旧版（硬编码 bot 名单，加 bot 不生效）→ cp restart-bots.example.sh restart-bots.sh"; left=1; }
ok "configs/_global.yml、~/.claude/channels/chenlulu/、restart-bots.sh 就位"

# 管理台凭据 ~/.claude-tgbot/hub.env。**必须独立于 ⑥ 的 cliproxy 安装**：
# 它以前寄生在 install_cliproxy.sh 里，于是下载失败或 SKIP_CLIPROXY=1 时就没有 hub.env，
# 而鉴权门读不到口令 = 整站放行 → 管理台 /hub 对能访问到 :8765 的人零鉴权。
# 幂等：已有的键一个都不会被重写（见 hub_bootstrap.ensure_hub_env）。
if python3 scripts/hub_bootstrap.py >/dev/null; then
  ok "管理台口令已就位（~/.claude-tgbot/hub.env，600 权限，不进 git）"
else
  todo "hub.env 没生成 → 管理台 /hub 会零鉴权，先别把 :8765 暴露到公网；重试：python3 scripts/hub_bootstrap.py"; left=1
fi

# ── 手填项体检 ────────────────────────────────────────────
say "④ 还需要你手填的密钥（脚本没法替你申请）"
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
  rc=0; load_agent "$AUTOSTART_LABEL" "$tmp_plist" || rc=$?
  if [ "$rc" = 0 ]; then
    ok "开机自启已注册（${AUTOSTART_LABEL}）"
    autostart_on=1
  elif [ "$rc" = 1 ]; then
    todo "plist 已写好但没加载上（常见于 SSH，没有图形会话）。图形登录后手动跑：launchctl bootstrap gui/\$UID $AUTOSTART_PLIST"
  else
    todo "自启 plist 渲染后格式校验没过，已跳过（不影响手动 bash restart-bots.sh）"
  fi
  # 老版 README 手动挂过的自启，标签不同不会被上面覆盖 → 会起两次，提醒删掉
  old_plist="$HOME/Library/LaunchAgents/com.example.claudebotlife-autostart.plist"
  if [ -f "$old_plist" ]; then
    todo "检测到旧版自启，删掉免得开机起两次：launchctl bootout gui/\$UID/com.example.claudebotlife-autostart; rm $old_plist"
  fi

  # 朋友圈网页 :8765（管理台 /hub 跑在同一个进程里）——也是开机自启，跟着一起挂。
  # 标签沿用模板里的老值：改名的话，照旧 README 手动挂过的用户会开机起两份。
  MOMENTS_LABEL="com.example.claudebotlife-moments-web"
  tmp_plist="$(mktemp "${TMPDIR:-/tmp}/claudebotlife-moments-web.XXXXXX")"
  sed -e "s#{PROJECT_DIR}#$PWD#g" plist-templates/moments-web.plist.tmpl > "$tmp_plist"
  rc=0; load_agent "$MOMENTS_LABEL" "$tmp_plist" || rc=$?
  if [ "$rc" = 0 ]; then
    ok "朋友圈网页 + 管理台已常驻：http://127.0.0.1:8765（管理台在 /hub）"
  elif [ "$rc" = 1 ]; then
    todo "moments-web 的 plist 写好了但没加载上（常见于 SSH）。图形登录后跑：launchctl bootstrap gui/\$UID $HOME/Library/LaunchAgents/$MOMENTS_LABEL.plist"
  else
    todo "moments-web plist 渲染后格式校验没过，已跳过（不影响手动 python3 -m moments.web）"
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
  plist="$HOME/Library/LaunchAgents/com.claudebotlife.cliproxy.plist"
  svc="$HOME/.config/systemd/user/claudebotlife-cliproxy.service"
  # 只认那一行工作目录配置。模糊 grep 这个路径没用：二进制路径、日志路径里都带它，
  # WorkingDirectory 整个丢了也照样命中。
  unit=""; wd_ok=0
  if [ -f "$plist" ]; then
    unit="$plist"
    if grep -A1 '<key>WorkingDirectory</key>' "$plist" | grep -qF "<string>$cliproxy_dir</string>"; then wd_ok=1; fi
  elif [ -f "$svc" ]; then
    unit="$svc"
    # -x 整行匹配：子串匹配会被 WorkingDirectory=$dir/logs、=$dir-old、
    # 以及 `#WorkingDirectory=$dir` 这种注释掉的行骗过去。
    # 两种写法都认：install_cliproxy.sh 现在写带引号的那种（路径含空格才不会被
    # systemd 切开），老机器上留着的是不带引号的旧单元 —— 只认一种会把它误报成没钉住。
    if grep -qxF "WorkingDirectory=$cliproxy_dir" "$svc" \
       || grep -qxF "WorkingDirectory=\"$cliproxy_dir\"" "$svc"; then wd_ok=1; fi
  fi
  if [ -z "$cliproxy_dir" ]; then
    todo "读不到 cliproxy 的安装目录，重跑：bash scripts/install_cliproxy.sh"
  elif [ -z "$unit" ]; then
    todo "cliproxy 装好了但没有常驻单元（SSH 无图形会话、或没有 systemd 都会这样），开机不会自己起；手动起：${cliproxy_dir}/cli-proxy-api -config ${cliproxy_dir}/config.yaml（工作目录必须是 ${cliproxy_dir}）"
  elif [ "$wd_ok" = 0 ]; then
    todo "常驻单元 $unit 的 WorkingDirectory 没钉在 ${cliproxy_dir}，重跑：bash scripts/install_cliproxy.sh"
  else
    ok "cliproxy 已常驻，工作目录钉在 $cliproxy_dir"
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
