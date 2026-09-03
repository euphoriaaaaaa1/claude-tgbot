#!/usr/bin/env bash
# 装 cliproxy（Mac / Linux）：下载 → 校验 → 解压 → 生成配置 → 注册常驻。
# 幂等：版本对得上就跳过下载；配置只做字段级补齐，绝不抹掉你已配好的 provider。
# 资产名/下载地址/校验和/配置骨架**一律问 scripts/hub_bootstrap.py**，本脚本不重复这些知识。
#
# 接缝（自测与离线安装用，生产不设）：
#   HUB_CLIPROXY_LOCAL_PKG=<包路径>  用本地压缩包代替下载，校验流程照跑
#   HUB_SKIP_AUTOSTART=1            只装不注册常驻
set -euo pipefail
cd "$(dirname "$0")/.."

BS="scripts/hub_bootstrap.py"
say()  { printf '\033[1;36m%s\033[0m\n' "$*"; }
ok()   { printf '  \033[1;32m✅ %s\033[0m\n' "$*"; }
todo() { printf '  \033[1;33m⚠️  %s\033[0m\n' "$*"; }
die()  { printf '  \033[1;31m❌ %s\033[0m\n' "$*" >&2; exit 1; }

command -v python3 >/dev/null || die "缺 python3，装不了 cliproxy"
VER="$(python3 "$BS" get version)"
ASSET="$(python3 "$BS" get asset)"
URL="$(python3 "$BS" get url)"
WANT="$(python3 "$BS" get sha256)"
CLIPROXY_DIR="$(python3 "$BS" get dir)"   # ~/.claude-tgbot/cliproxy
BIN="$(python3 "$BS" get bin)"
HUB_ENV="$(python3 "$BS" get env)"
STAMP="$CLIPROXY_DIR/.installed-version"

# [R4D] I6-3：下载源白名单。排障时最容易被人随手换成某个镜像，这里再挡一道
case "$URL" in
  https://github.com/*) ;;
  *) die "bad_download_host: $URL" ;;
esac

# ── 下载 + 校验 + 解压 ────────────────────────────────────
say "① 装 cli-proxy-api $VER"
if [ -x "$BIN" ] && [ "$(cat "$STAMP" 2>/dev/null || true)" = "$VER" ]; then
  ok "已是 ${VER}，跳过下载"
else
  mkdir -p "$CLIPROXY_DIR"
  TMP="$(mktemp -d "${TMPDIR:-/tmp}/cliproxy.XXXXXX")"
  trap 'rm -rf "$TMP"' EXIT
  PKG="$TMP/$ASSET"
  if [ -n "${HUB_CLIPROXY_LOCAL_PKG:-}" ]; then
    cp "$HUB_CLIPROXY_LOCAL_PKG" "$PKG"
    ok "用本地包 ${HUB_CLIPROXY_LOCAL_PKG}（校验照跑）"
  else
    echo "  下载 ${ASSET}（约 20 MB，解压后约 57 MB）…"
    curl -fL --proto '=https' --tlsv1.2 -o "$PKG" "$URL" || die "下载失败：$URL"
  fi

  # 上游没有 GPG 签名，sha256 是唯一防线：**校验先于解压**，不匹配就什么都不留
  if command -v shasum >/dev/null; then GOT="$(shasum -a 256 "$PKG" | awk '{print $1}')"
  else GOT="$(sha256sum "$PKG" | awk '{print $1}')"; fi
  [ "$GOT" = "$WANT" ] || die "checksum_mismatch: $ASSET 校验和对不上，已丢弃（期望 ${WANT}）"
  ok "sha256 校验通过"

  # 防归档路径穿越：绝对路径或 .. 一律拒（[R4D] B5）。
  # 先把清单落成文件再 grep：`tar | grep -q` 里 grep 提前退出会让 tar 吃到 SIGPIPE，
  # 叠加 pipefail 后整条管道恒非零，反而把"发现越界路径"这个分支绕过去了。
  tar -tzf "$PKG" > "$TMP/members.txt"
  if grep -qE '^/|(^|/)\.\.(/|$)' "$TMP/members.txt"; then
    die "unsafe_archive_path: 包里有越界路径，已丢弃"
  fi
  mkdir -p "$TMP/stage"
  tar -xzf "$PKG" -C "$TMP/stage"
  SRC="$(find "$TMP/stage" -type f -name cli-proxy-api | head -1 || true)"
  [ -n "$SRC" ] || die "包里没找到 cli-proxy-api"
  install -m 0700 "$SRC" "$BIN"
  printf '%s\n' "$VER" > "$STAMP"
  ok "已装到 $BIN"
fi

# ── 配置骨架 + hub.env（三平台同一份逻辑）────────────────
say "② 生成配置与凭据"
python3 "$BS" || die "hub_bootstrap 失败"

# ── 注册常驻 ──────────────────────────────────────────────
if [ "${HUB_SKIP_AUTOSTART:-0}" = "1" ]; then
  say "③ 跳过常驻注册（HUB_SKIP_AUTOSTART=1）"
  exit 0
fi
say "③ 注册常驻（崩了自动拉起）"
if [ "$(uname -s)" = "Darwin" ]; then
  LABEL="com.claudebotlife.cliproxy"
  PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
  tmp_plist="$(mktemp "${TMPDIR:-/tmp}/claudebotlife-cliproxy.XXXXXX")"
  sed -e "s#{CLIPROXY_BIN}#$BIN#g" -e "s#{CLIPROXY_DIR}#$CLIPROXY_DIR#g" \
      plist-templates/cliproxy.plist.tmpl > "$tmp_plist"
  # 🔴 A11b：v7 启动会往**进程 CWD** 下载 static/management.html（2.7 MB）。
  # 模板一旦漂了就是往用户仓库里拉 2.7 MB，所以渲染完必须验一遍 WorkingDirectory 钉住了没。
  if ! grep -A1 '<key>WorkingDirectory</key>' "$tmp_plist" | grep -qF "$CLIPROXY_DIR"; then
    rm -f "$tmp_plist"; die "plist 的 WorkingDirectory 没钉在 ${CLIPROXY_DIR}，拒绝注册"
  fi
  if plutil -lint "$tmp_plist" >/dev/null 2>&1; then
    mkdir -p "$HOME/Library/LaunchAgents"
    mv -f "$tmp_plist" "$PLIST"
    launchctl bootout "gui/$UID/$LABEL" >/dev/null 2>&1 || true
    if launchctl bootstrap "gui/$UID" "$PLIST" >/dev/null 2>&1; then
      ok "已常驻（${LABEL}），崩溃会自动拉起"
    else
      todo "plist 写好了但没加载上（常见于 SSH 无图形会话）。图形登录后跑：launchctl bootstrap gui/\$UID $PLIST"
    fi
  else
    rm -f "$tmp_plist"; die "plist 渲染后格式校验没过"
  fi
elif command -v systemctl >/dev/null && systemctl --user show-environment >/dev/null 2>&1; then
  UNIT_DIR="$HOME/.config/systemd/user"
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/claudebotlife-cliproxy.service" <<UNITEOF
[Unit]
Description=claude-tgbot cliproxy (CLIProxyAPI $VER)

[Service]
Type=simple
# 与 plist 的 WorkingDirectory 同义：不钉死就会往当时的 CWD 拉 2.7 MB static/
WorkingDirectory=$CLIPROXY_DIR
EnvironmentFile=-$HUB_ENV
ExecStart="$BIN" -config "$CLIPROXY_DIR/config.yaml"
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
UNITEOF
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  if systemctl --user enable --now claudebotlife-cliproxy >/dev/null 2>&1; then
    ok "已常驻（systemd user unit: claudebotlife-cliproxy）"
  else
    todo "unit 写好了但 enable 失败，手动跑：systemctl --user enable --now claudebotlife-cliproxy"
  fi
else
  todo "TODO: 这台机器没有 launchd/systemd user，开机自启请自己挂（cliproxy 命令：$BIN -config $CLIPROXY_DIR/config.yaml，工作目录必须是 ${CLIPROXY_DIR}）"
fi

echo
say "cliproxy 就绪：http://127.0.0.1:$(python3 "$BS" get port)"
echo "  配置：$CLIPROXY_DIR/config.yaml（provider 请在管理台加，别手改）"
echo "  升级：改 configs/cliproxy.version + 换 configs/cliproxy.sha256，重跑本脚本"
