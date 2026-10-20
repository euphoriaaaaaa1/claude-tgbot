#!/usr/bin/env bash
# claude-tgbot 一键安装（Mac / Linux）。
# 幂等：重复运行安全，绝不覆盖你已填好的配置。
# 自动完成 README 部署 ②~⑥ 里一切能自动的事；密钥没法替你填，
# 跑完会列出「还差哪几个要手填」的清单，全填完再跑一次本脚本即可看到全绿。
set -e
cd "$(dirname "$0")"

# 不接受位置参数（INTERFACE §1.1）：误传参数一律 2，避免把拼错的选项当成静默忽略。
if [ $# -gt 0 ]; then
  echo "用法：bash install.sh（不接受参数；用环境变量 DRY_RUN=1 等控制行为）" >&2
  exit 2
fi

say()  { printf '\033[1;36m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[1;32m✅ %s\033[0m\n' "$*"; }
todo() { printf '  \033[1;33m⚠️  %s\033[0m\n' "$*"; }

# 还差几项要人手补。从 ① 起就可能被置 1，结尾据它决定报不报"全部就绪"。
left=0

PYTHON_BIN="${PYTHON_BIN:-python3}"
DRY_RUN="${DRY_RUN:-0}"

# install.sh 只覆盖 macOS / Linux；Windows 走 windows\register-tasks.ps1。
case "$(uname -s)" in
  Darwin|Linux) ;;
  *) echo "错误：install.sh 只支持 macOS/Linux。Windows 请用 powershell -File windows\\register-tasks.ps1" >&2; exit 2 ;;
esac

# ── ① 前置依赖检查 ─────────────────────────────────────────
say "① 检查前置依赖"
missing=0
command -v python3 >/dev/null || { todo "缺 Python 3 → 去 python.org 的 Downloads 页装"; missing=1; }
command -v bun     >/dev/null || { todo "缺 Bun → 照 bun.sh 官网首页给的那行安装命令装"; missing=1; }
command -v claude  >/dev/null || { todo "缺 Claude Code CLI → npm install -g @anthropic-ai/claude-code，装完跑一次 claude 登录"; missing=1; }
[ "$missing" = 1 ] && { echo; echo "先装齐上面缺的，再重新跑 bash install.sh"; exit 1; }
ok "python3 / bun / claude 都在"
# dispatcher 由 restart-bots.sh 用 nohup 后台拉起，不依赖 tmux/launchd。

if [ "$DRY_RUN" = 1 ]; then
  say "DRY_RUN=1：只检查依赖并打印计划，不写盘、不装依赖、不注册服务"
  echo "  ② $PYTHON_BIN -m pip install -r requirements.txt；cd dispatcher && bun install"
  echo "  ③ 铺 configs/_global.yml、configs/providers.json（从 example 复制，0600）、restart-bots.sh（仅缺失时复制）"
  echo "  ④ 密钥体检（不写入；实际运行时可当场粘贴）"
  echo "  ⑤ $PYTHON_BIN platform_services.py register autostart --repo $PWD"
  echo "  ⑤ $PYTHON_BIN platform_services.py register provider-web --repo $PWD"
  echo "  只预览服务命令：$PYTHON_BIN platform_services.py register autostart --repo $PWD --dry-run"
  echo "                  $PYTHON_BIN platform_services.py register provider-web --repo $PWD --dry-run"
  echo "  设置/修改面板口令：$PYTHON_BIN -m moments.panel_auth --set-password"
  echo "  管理台地址：http://127.0.0.1:8767/login"
  exit 0
fi

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

# 供应商清单：管理台（8767）和 provider_config 都读它。缺失时给一份空白清单，
# 否则管理台供应商页首次保存会失败。原子写（同目录 tmp + mv）、0600；绝不覆盖已有文件。
if [ ! -e configs/providers.json ]; then
  if [ -f configs/providers.example.json ]; then
    providers_tmp="$(mktemp configs/.providers.json.XXXXXX)"
    cp configs/providers.example.json "$providers_tmp"
    chmod 600 "$providers_tmp"
    mv -f "$providers_tmp" configs/providers.json
    ok "已创建 configs/providers.json（空白清单，0600）"
  else
    todo "缺 configs/providers.example.json，无法创建 configs/providers.json → 重新 clone 仓库或手写 {\"providers\": [], \"bindings\": {}}"
    left=1
  fi
fi

# ── 手填项体检 ────────────────────────────────────────────
say "④ 三把必填密钥（脚本没法替你申请，但会告诉你去哪拿、填哪，能当场填）"
# 逐项：缺什么 → 怎么拿 → 填哪 → 终端里直接粘贴就写进文件（非交互时只打说明）。
# 写文件交给 scripts/setup_keys.py 按 YAML/.env/JSON 各自格式写，不用 sed 猜行。
bash scripts/setup_keys.sh || true
if [ "$(python3 scripts/setup_keys.py status | python3 -c 'import json,sys; d=json.load(sys.stdin); print(int(all(d.values())))')" != 1 ]; then
  left=1
fi

# ── ⑤ 注册开机自启 ────────────────────────────────────────
# 统一走 platform_services.py：macOS=launchd、Linux=systemd --user。
# 幂等：注册前先卸后装；任何一步失败只 warn 不中断安装（比如 SSH 里没有图形会话）。
say "⑤ 注册开机自启"
autostart_on=0
# 老版 install.sh / README 用过两个历史标签；先卸掉，免得开机起两次。
# platform_services 里对这两个标签做了精确白名单，不碰别家服务。
for LEGACY_AUTOSTART_LABEL in com.claudebotlife.autostart com.example.claudebotlife.autostart; do
  if [ -f "$HOME/Library/LaunchAgents/$LEGACY_AUTOSTART_LABEL.plist" ]; then
    todo "检测到旧版自启标签 $LEGACY_AUTOSTART_LABEL，先卸载"
    "$PYTHON_BIN" platform_services.py unregister --raw-label "$LEGACY_AUTOSTART_LABEL" >/dev/null 2>&1 || true
  fi
done
if "$PYTHON_BIN" platform_services.py register autostart --repo "$PWD"; then
  ok "开机自启已注册（$(uname -s)，platform_services）"
  autostart_on=1
else
  rc=$?
  left=1
  todo "开机自启注册失败（退出码 $rc）。手动重跑：$PYTHON_BIN platform_services.py register autostart --repo \"$PWD\""
fi

# 8767 管理台单独注册：只装 autostart 时它不会起，用户 clone 后打不开 8767。
# 同样幂等（先卸后装），失败不中断安装，但必须置 left，不能静默成功。
say "⑤ 注册 8767 管理台（provider-web）"
if "$PYTHON_BIN" platform_services.py register provider-web --repo "$PWD"; then
  ok "管理台已注册（http://127.0.0.1:8767/login）"
else
  rc=$?
  left=1
  todo "管理台注册失败（退出码 $rc）。手动重跑：$PYTHON_BIN platform_services.py register provider-web --repo \"$PWD\""
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
  echo "  $PYTHON_BIN platform_services.py unregister autostart"
fi

echo
say "面板口令与访问地址"
echo "  设置/修改面板口令：$PYTHON_BIN -m moments.panel_auth --set-password"
echo "  访问地址：http://127.0.0.1:8767/login"
echo "  首次用 claude 新建 bot 前，需信任 ~/.claude/channels（按 README §5 的说明；管理台和启动脚本也会提示）"
