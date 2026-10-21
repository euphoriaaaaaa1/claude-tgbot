#!/bin/bash
# 本项目专属 OpenAI 协议转换代理实例的安装/升级脚本。
#   用法：install_provider_proxy.sh            全新安装（目标 plist 已存在即退出 1）
#         install_provider_proxy.sh --upgrade  只换二进制（plist 必须已存在）
# 端口、运行目录、launchd label 与 plist 路径均从 provider_proxy 现读配置
# （CLAUDEBOT_PROXY_PORT / CLAUDEBOT_PROXY_DIR 可覆盖缺省值），不假设任何固定前缀目录。
# 二进制来源：优先 CLAUDEBOT_PROXY_SRC_BIN，否则在 PATH、~/.claude-tgbot、仓库根查找。
# 退出码：0 成功；1 任一检查/自检失败（零写入，或已回滚 plist，或 --upgrade 已换回旧二进制）。
# launchctl、lsof、shasum 一律经 PATH 查找（测试以桩替换）；端口占用与监听自检只看 lsof。
# config.yaml 的「可解析」只按面板产出的 YAML 子集做行级判定，不是通用 YAML 解析（不依赖 PyYAML）。
set -euo pipefail

PY="${PYTHON:-python3}"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TMPL="$PROJECT_DIR/plist-templates/provider-proxy.plist.tmpl"

die() { echo "❌ $*" >&2; exit 1; }

read_cfg() {  # $1 = python 表达式；失败即退出
  "$PY" -c "import provider_proxy, os; print($1)" 2>/dev/null \
    || die "无法从 provider_proxy 读取代理配置（$1）"
}

PROXY_PORT="$(read_cfg 'provider_proxy.proxy_port()')"
PROXY_DIR="$(read_cfg 'provider_proxy.proxy_dir()')"
LABEL="$(read_cfg 'provider_proxy.LAUNCHD_LABEL')"
TARGET_PLIST="$(read_cfg 'os.path.expanduser(provider_proxy.PLIST_PATH)')"
CONFIG="$PROXY_DIR/config.yaml"
STATE="$PROXY_DIR/state.json"
DEST_BIN="$PROXY_DIR/cli-proxy-api"
DOMAIN="gui/$(id -u)"
SRC_BIN="${CLAUDEBOT_PROXY_SRC_BIN:-}"
BACKUP=""

case "${1:-}" in
  "") UPGRADE=0 ;;
  --upgrade) UPGRADE=1 ;;
  *) die "用法: $0 [--upgrade]" ;;
esac

# ─── 0. 配置合法性（非法端口/目录一律拒绝，零写入）──────────────────────────
case "$PROXY_PORT" in
  ''|*[!0-9]*) die "代理端口非法：$PROXY_PORT" ;;
esac
[ "$PROXY_PORT" -gt 0 ] && [ "$PROXY_PORT" -le 65535 ] || die "代理端口越界：$PROXY_PORT"
[ -n "$PROXY_DIR" ] && [ "${PROXY_DIR#/}" != "$PROXY_DIR" ] || die "代理运行目录必须是绝对路径：$PROXY_DIR"
[ -f "$TMPL" ] || die "缺少 plist 模板：$TMPL"

# 二进制来源：显式环境变量优先，其次常见放置位置。
if [ -z "$SRC_BIN" ]; then
  on_path="$(command -v cli-proxy-api 2>/dev/null || true)"
  for candidate in "$on_path" "$HOME/.claude-tgbot/cli-proxy-api" "$PROJECT_DIR/cli-proxy-api"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
      SRC_BIN="$candidate"
      break
    fi
  done
fi
[ -n "$SRC_BIN" ] || die "找不到 cli-proxy-api；请用 CLAUDEBOT_PROXY_SRC_BIN 指定二进制路径"

# ─── 1. 平台与 Rosetta 探测（S1：darwin_amd64 二进制在 Apple Silicon 上需要 Rosetta）───
echo "探测平台与 Rosetta..."
ARCH="$(uname -m)"
echo "  当前 CPU 架构: $ARCH"
if [ "$ARCH" = "arm64" ]; then
  if ! /usr/bin/pgrep -q oahd 2>/dev/null && ! /usr/bin/arch -x86_64 /usr/bin/true 2>/dev/null; then
    echo "❌ arm64 机器上无法运行 x86_64 二进制：请先安装 Rosetta："
    echo "     softwareupdate --install-rosetta --agree-to-license"
    exit 1
  fi
  echo "  ✅ Rosetta 可用（x86_64 二进制可运行）"
elif [ "$ARCH" = "x86_64" ]; then
  echo "  ✅ 原生 x86_64，可直接运行"
else
  echo "❌ 未支持的架构: ${ARCH}（本二进制为 darwin_amd64）"
  exit 1
fi

# ─── 工具函数 ───────────────────────────────────────────────────────────────
sha_of() { shasum -a 256 "$1" | awk '{print $1}'; }
rand_key() { od -An -tx1 -N24 /dev/urandom | tr -d ' \n'; }   # 定长读取，避免管道 SIGPIPE
is_listening() { lsof -nP -iTCP:"$PROXY_PORT" -sTCP:LISTEN >/dev/null 2>&1; }

wait_listen() {  # 装载后 20 s 内进入监听即成功
  local i
  for i in $(seq 1 20); do
    is_listening && return 0
    sleep 1
  done
  return 1
}

check_flags() {  # 模板里每个 flag 都须在二进制 -h 输出（stdout+stderr，忽略退出码）中整词出现
  local help flag
  [ -x "$1" ] || die "二进制不存在或不可执行：$1"
  help="$("$1" -h 2>&1 || true)"
  for flag in $(grep -oE '<string>-[A-Za-z0-9_-]+</string>' "$TMPL" | sed -E 's#</?string>##g; s/^-//'); do
    printf '%s\n' "$help" | grep -qE "^[[:space:]]*-${flag}([^A-Za-z0-9_]|\$)" \
      || die "二进制 -h 中没有 flag -${flag}，拒绝安装"
  done
}

atomic_write() {  # $1 = 目标文件，内容取自 stdin：同目录临时文件 → sync 落盘 → mv 替换
  local tmp
  tmp="$(mktemp "$(dirname "$1")/.$(basename "$1").XXXXXX")"
  if ! cat >"$tmp"; then rm -f "$tmp"; return 1; fi
  chmod 600 "$tmp"
  sync
  mv -f "$tmp" "$1"
}

config_ok() {  # 面板产出子集：顶层 `key:`/`key: value`、顶格 `- ` 序列项或两空格子项；api-keys 块列表或行内列表至少一个非空值
  [ -s "$CONFIG" ] || return 1
  awk '
    /^[[:space:]]*$/ { next }
    /^[A-Za-z0-9_-]+:/ {
      inkeys = ($0 ~ /^api-keys:[[:space:]]*$/)
      if ($0 ~ /^api-keys:[[:space:]]*\[.*\][[:space:]]*$/) {
        s = $0; sub(/^api-keys:[[:space:]]*\[/, "", s); sub(/\][[:space:]]*$/, "", s)
        gsub(/["\047,[:space:]]/, "", s); if (s != "") ok = 1
      }
      next
    }
    /^-([[:space:]]|$)/ {  # PyYAML 顶格块序列项：属于上一个顶层键（面板 build_config 的真实产出形态）
      if (inkeys) { s = $0; sub(/^-/, "", s); gsub(/["\047[:space:]]/, "", s); if (s != "") ok = 1 }
      next
    }
    /^  / {
      if (inkeys && $0 ~ /^  -/) { s = $0; sub(/^  -/, "", s); gsub(/["\047[:space:]]/, "", s); if (s != "") ok = 1 }
      next
    }
    { bad = 1 }
    END { exit (bad || !ok) }' "$CONFIG"
}

write_skeleton() {  # 随机占位 key：实例在面板首次重建配置前拒绝一切请求（不落其它文件、不打印）
  atomic_write "$CONFIG" <<YAML
host: "127.0.0.1"
port: $PROXY_PORT
auth-dir: "$PROXY_DIR/auths"
api-keys:
  - "$(rand_key)"
remote-management:
  allow-remote: false
  disable-control-panel: true
YAML
}

fill_fixed_keys() {  # 只追加缺失的顶层固定键，其它键原样保留
  local add=""
  grep -q '^host:' "$CONFIG" || add+=$'host: "127.0.0.1"\n'
  grep -q '^port:' "$CONFIG" || add+="port: $PROXY_PORT"$'\n'
  grep -q '^auth-dir:' "$CONFIG" || add+="auth-dir: \"$PROXY_DIR/auths\""$'\n'
  grep -q '^remote-management:' "$CONFIG" || add+=$'remote-management:\n  allow-remote: false\n  disable-control-panel: true\n'
  [ -n "$add" ] || return 0
  { cat "$CONFIG"; [ -z "$(tail -c1 "$CONFIG")" ] || echo; printf '%s' "$add"; } | atomic_write "$CONFIG"
}

prepare_config() {
  if [ -e "$CONFIG" ] && config_ok; then fill_fixed_keys; else write_skeleton; fi
}

write_state() {  # 每次安装都改写 binary_sha256：合并写，其它键保留；文件不可解析才整份重写
  local merged rc=0
  if [ -f "$STATE" ]; then
    merged="$("$PY" -c '
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    assert isinstance(d, dict)
except Exception:
    sys.exit(3)
d["binary_sha256"] = sys.argv[2]
print(json.dumps(d, indent=2, ensure_ascii=False))' "$STATE" "$1")" || rc=$?
    if [ "$rc" = 0 ]; then printf '%s\n' "$merged" | atomic_write "$STATE"; return; fi
    [ "$rc" = 3 ] || die "无法用 $PY 合并写 state.json（退出码 $rc）"
  fi
  printf '{\n  "port": %s,\n  "binary_sha256": "%s"\n}\n' "$PROXY_PORT" "$1" | atomic_write "$STATE"
}

install_bin() { cp -f "$1" "$DEST_BIN"; chmod 755 "$DEST_BIN"; }

write_plist() {  # 端口/目录/label 都来自配置，模板只做占位符替换
  sed -e "s|{PROXY_LABEL}|$LABEL|g" -e "s|{PROXY_BIN}|$DEST_BIN|g" -e "s|{PROXY_DIR}|$PROXY_DIR|g" "$TMPL" > "$1"
}

# ─── --upgrade：只换二进制，失败换回旧的；plist 永不删除 ─────────────────────
if [ "$UPGRADE" = 1 ]; then
  [ -e "$TARGET_PLIST" ] || die "未安装（找不到 ${TARGET_PLIST}），不能 --upgrade"
  [ -f "$DEST_BIN" ] || die "运行目录里没有旧二进制，不能 --upgrade"
  check_flags "$SRC_BIN"
  BACKUP="$(mktemp -d "${HOME}/.claudebotlife-proxy-upgrade.XXXXXX")"
  trap '[ -n "$BACKUP" ] && rm -rf -- "$BACKUP"' EXIT   # 备份目录无论成败都删
  cp -p "$DEST_BIN" "$BACKUP/cli-proxy-api"
  OLD_SHA="$(sha_of "$BACKUP/cli-proxy-api")"
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  install_bin "$SRC_BIN"
  write_state "$(sha_of "$DEST_BIN")"
  prepare_config
  launchctl bootstrap "$DOMAIN" "$TARGET_PLIST" || true
  if wait_listen; then echo "✅ 升级完成"; exit 0; fi
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  install_bin "$BACKUP/cli-proxy-api"
  write_state "$OLD_SHA"
  launchctl bootstrap "$DOMAIN" "$TARGET_PLIST" || true
  die "新二进制装载后 20 s 内未监听 ${PROXY_PORT}，已换回旧二进制；请查看 $PROXY_DIR/logs/stderr.log"
fi

# ─── 全新安装：检查全部通过之前不写任何东西 ──────────────────────────────────
[ ! -e "$TARGET_PLIST" ] || die "目标 plist 已存在，拒绝覆盖：${TARGET_PLIST}（如需换二进制用 --upgrade）"
! is_listening || die "端口 $PROXY_PORT 已被占用"
check_flags "$SRC_BIN"

mkdir -p "$PROXY_DIR/logs"
chmod 700 "$PROXY_DIR"
install_bin "$SRC_BIN"
prepare_config
write_state "$(sha_of "$DEST_BIN")"
write_plist "$TARGET_PLIST"
launchctl bootstrap "$DOMAIN" "$TARGET_PLIST" || true
if ! wait_listen; then
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  rm -f "$TARGET_PLIST"
  die "装载后 20 s 内未监听 ${PROXY_PORT}，已卸载并删除 plist；请查看 $PROXY_DIR/logs/stderr.log"
fi
echo "✅ 安装完成：127.0.0.1:$PROXY_PORT 已监听"
echo "下一步：在面板里给 provider 开 proxy 并点「重新应用供应商代理」。"
