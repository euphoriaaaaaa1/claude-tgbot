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
# 本项目十几处入口（launchd plist、restart-bots.sh、moments web 等）都直接调 `python3`、
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
[ -f restart-bots.sh ] || cp restart-bots.example.sh restart-bots.sh
# 上面那行不覆盖已存在的文件，所以老机器上可能留着旧版硬编码 BOTS 名单的 restart-bots.sh：
# 它不读注册表 → 新加的 bot 永远起不来，而重启脚本本身照样"成功"退出。
grep -q bots_registry restart-bots.sh 2>/dev/null || {
  todo "restart-bots.sh 是旧版（硬编码 bot 名单，加 bot 不生效）→ cp restart-bots.example.sh restart-bots.sh"; left=1; }
ok "configs/_global.yml、restart-bots.sh 就位"

# ── 手填项体检 ────────────────────────────────────────────
say "④ 三把必填密钥（脚本没法替你申请，但会告诉你去哪拿、填哪，能当场填）"
# 逐项：缺什么 → 怎么拿 → 填哪 → 终端里直接粘贴就写进文件（非交互时只打说明）。
# 写文件交给 scripts/setup_keys.py 按 YAML/.env/JSON 各自格式写，不用 sed 猜行。
bash scripts/setup_keys.sh || true
if [ "$(python3 scripts/setup_keys.py status | python3 -c 'import json,sys; d=json.load(sys.stdin); print(int(all(d.values())))')" != 1 ]; then
  left=1
fi

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
