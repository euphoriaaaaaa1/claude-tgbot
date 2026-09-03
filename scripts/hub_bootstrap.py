#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cliproxy 引导：资产名/下载地址/校验和 + config.yaml 骨架 + hub.env（三平台共用）。

[RA] M6：**架构映射、config 骨架、hub.env 键名这三套知识的唯一定义点**。
install_cliproxy.sh / install-cliproxy.ps1 只保留下载、解压、注册常驻，
需要这三套知识时一律 `hub_bootstrap.py get <name>` 来问，绝不各写一遍——
各写一遍的后果是改一个字段要改三处，漏掉 ps1 那处 = Windows 用户静默拿到错配置。

用法：
  hub_bootstrap.py                生成/补齐 ~/.claude-tgbot/{hub.env, cliproxy/config.yaml}
  hub_bootstrap.py get <name>     打印单个值（version|asset|url|sha256|home|dir|bin|config|env|port）

出错时 stderr 首词是错误码，脚本据此判因，退出码 2：
  bad_version / sha256_version_mismatch / checksum_entry_missing /
  unsupported_platform / bad_download_host / config_unparsable
"""
import os
import platform
import re
import secrets
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RELEASE_REPO = "router-for-me/CLIProxyAPI"
# [R4D] I6-3：下载源写死，构造出别的 host 直接失败（排障时最容易被人改成随手一个镜像）
ALLOWED_HOSTS = ("github.com", "objects.githubusercontent.com")
DEFAULT_PORT = "8317"
# v7 把 arm64 改成了 aarch64（[CPX] Q6 引 release.yaml）；Windows 上 platform.machine() 给大写
ARCH_MAP = {"x86_64": "amd64", "amd64": "amd64", "arm64": "aarch64", "aarch64": "aarch64"}
OS_MAP = {"darwin": "darwin", "linux": "linux", "windows": "windows"}


class BootstrapError(Exception):
    def __init__(self, code, msg):
        super().__init__("%s: %s" % (code, msg))
        self.code = code


# ── 资产名 / 下载地址 / 校验和 ────────────────────────────────

def arch_tag(machine=None):
    m = (machine or platform.machine() or "").strip().lower()
    if m not in ARCH_MAP:
        raise BootstrapError("unsupported_platform", "认不出 CPU 架构 %r" % m)
    return ARCH_MAP[m]


def os_tag(system=None):
    s = (system or platform.system() or "").strip().lower()
    if s not in OS_MAP:
        raise BootstrapError("unsupported_platform", "认不出操作系统 %r" % s)
    return OS_MAP[s]


def asset_name(version, system=None, machine=None):
    o = os_tag(system)
    ext = "zip" if o == "windows" else "tar.gz"
    return "CLIProxyAPI_%s_%s_%s.%s" % (version, o, arch_tag(machine), ext)


def read_version():
    raw = (REPO / "configs" / "cliproxy.version").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", raw):
        raise BootstrapError("bad_version", "cliproxy.version 应形如 7.2.148，实得 %r" % raw)
    return raw


def read_checksums(version):
    """返回 {资产名: sha256}。首行 `# <版本号>` 必须与 cliproxy.version 一致（[RA] 建议5）。"""
    lines = [l for l in (REPO / "configs" / "cliproxy.sha256").read_text(
        encoding="utf-8").splitlines() if l.strip()]
    head = lines[0].lstrip("#").strip() if lines else ""
    if head != version:
        raise BootstrapError("sha256_version_mismatch",
                             "cliproxy.sha256 首行 %r ≠ cliproxy.version %r"
                             " —— 改版本号时忘了换校验和" % (head, version))
    out = {}
    for l in lines[1:]:
        m = re.fullmatch(r"([0-9a-f]{64})\s+(\S+)", l.strip())
        if not m:
            raise BootstrapError("sha256_version_mismatch", "校验和行格式不对：%r" % l)
        out[m.group(2)] = m.group(1)
    return out


def expected_sha256(version, asset):
    sums = read_checksums(version)
    if asset not in sums:
        # [R4D] I6：条目缺失必须失败，绝不走"找不到就跳过校验"——上游无 GPG 签名，sha256 是唯一防线
        raise BootstrapError("checksum_entry_missing",
                             "cliproxy.sha256 里没有 %s 的条目" % asset)
    return sums[asset]


def download_url(version, asset):
    url = "https://github.com/%s/releases/download/v%s/%s" % (RELEASE_REPO, version, asset)
    host = re.match(r"https://([^/]+)/", url).group(1)
    if host not in ALLOWED_HOSTS:
        raise BootstrapError("bad_download_host", "下载源 %s 不在白名单" % host)
    return url


# ── 路径 ──────────────────────────────────────────────────

def tgbot_home():
    env = os.environ.get("CLAUDE_TGBOT_HOME")
    return Path(env).expanduser() if env else Path.home() / ".claude-tgbot"


def cliproxy_dir():
    return tgbot_home() / "cliproxy"


def _mkdir700(p):
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(p, 0o700)  # 已存在的目录 mkdir 不改权限，显式补一刀


def _write600(path, text):
    """一次建成 0600（不是先建再 chmod，避免明文 key 有一瞬间是 644）。"""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    if os.name != "nt":
        os.chmod(path, 0o600)


# ── hub.env（§0.3 的五个键；凭据只在这里，plist/任务 XML 一律不带，见 §8.1 A10b）──

HUB_ENV_HEADER = [
    "# claude-tgbot 管理台凭据。权限 600，绝不入 git，绝不打进日志。",
    "# 常驻单元不带凭据，由包装脚本 `set -a; . 本文件; set +a` 注入进程环境。",
    "# 想再加一道「管理口令」（浏览/管理分锁），自己补一行 HUB_ADMIN_PASSWORD 后重启门户。",
]


def parse_env_file(path):
    """KEY=VALUE 逐行解析，忽略 # 开头与空行（§0.3 的回落解析口径）。"""
    out = {}
    if not Path(path).exists():
        return out
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip().strip('"')
    return out


def ensure_hub_env(path):
    """补齐缺的键，**绝不动已有的值**——重生成口令 = 用户被自己的门户锁在外面（A5）。"""
    cur = parse_env_file(path)
    lines = Path(path).read_text(encoding="utf-8").splitlines() if Path(path).exists() \
        else list(HUB_ENV_HEADER)
    changed = not Path(path).exists()
    wanted = [
        ("HUB_ACCESS_PASSWORD", lambda: secrets.token_urlsafe(24)),   # ≥16 字符（[R4D] I2-c）
        ("CLIPROXY_PORT", lambda: os.environ.get("CLIPROXY_PORT") or DEFAULT_PORT),
        ("CLIPROXY_MGMT_KEY", lambda: secrets.token_urlsafe(24)),
        ("CLIPROXY_API_KEY", lambda: "sk-tgbot-" + secrets.token_urlsafe(24)),
    ]
    for key, gen in wanted:
        if not cur.get(key):
            cur[key] = gen()
            lines.append("%s=%s" % (key, cur[key]))
            changed = True
    # A9b：S0-B 已实证 claude-api-key + base-url 能承载第三方 Anthropic 端点，探测逻辑已删，恒为 1
    if cur.get("CLIPROXY_ANTHROPIC_COMPAT") != "1":
        lines = [l for l in lines if not l.strip().startswith("CLIPROXY_ANTHROPIC_COMPAT=")]
        lines.append("CLIPROXY_ANTHROPIC_COMPAT=1")
        cur["CLIPROXY_ANTHROPIC_COMPAT"] = "1"
        changed = True
    if changed:
        _write600(path, "\n".join(lines) + "\n")
    elif os.name != "nt":
        os.chmod(path, 0o600)  # A7：老文件可能是 644，权限每次都收紧
    return cur, changed


# ── config.yaml：首次生成骨架，重跑只做**字段级补齐**（[RA] F1：整文件重写会抹掉用户的 provider）──

_EMPTY = ("", '""', "''", "~", "null", "[]")


def _scalar(rest):
    """取行内标量值：` 8317  # 端口` → `8317`（YAML 的行内注释要求 # 前有空白）。"""
    s = rest.strip()
    m = re.match(r"^(.*?)\s+#", s)
    return (m.group(1) if m else s).strip()


def _find_top(lines, key):
    pat = re.compile(r"^%s:(.*)$" % re.escape(key))
    for i, l in enumerate(lines):
        m = pat.match(l)
        if m:
            return i, m.group(1)
    return -1, ""


def _block_end(lines, i):
    j = i + 1
    while j < len(lines) and (not lines[j].strip() or lines[j][:1] in (" ", "\t")):
        j += 1
    return j


def set_top_scalar(lines, key, value, force=True):
    i, rest = _find_top(lines, key)
    if i < 0:
        lines.append("%s: %s" % (key, value))
        return True
    cur = _scalar(rest)
    if (not force and cur not in _EMPTY) or cur == str(value):
        return False
    lines[i] = "%s: %s" % (key, value)
    return True


def set_nested_scalar(lines, parent, key, value, force=True):
    i, rest = _find_top(lines, parent)
    if i < 0:
        lines.extend(["%s:" % parent, "  %s: %s" % (key, value)])
        return True
    if _scalar(rest):
        raise BootstrapError("config_unparsable",
                             "%s 是行内形式，本脚本只认块形式；手工改成块形式后重跑" % parent)
    pat = re.compile(r"^(\s+)%s:(.*)$" % re.escape(key))
    for j in range(i + 1, _block_end(lines, i)):
        m = pat.match(lines[j])
        if not m:
            continue
        cur = _scalar(m.group(2))
        if (not force and cur not in _EMPTY) or cur == str(value):
            return False
        lines[j] = "%s%s: %s" % (m.group(1), key, value)
        return True
    lines.insert(i + 1, "  %s: %s" % (key, value))
    return True


def ensure_list_item(lines, key, value):
    """保证 `key:` 的列表里有 value（保留用户自己加的其它项）。"""
    quoted = '"%s"' % value
    i, rest = _find_top(lines, key)
    if i < 0:
        lines.extend(["%s:" % key, "  - %s" % quoted])
        return True
    cur = _scalar(rest)
    if cur and cur not in _EMPTY:
        raise BootstrapError("config_unparsable",
                             "%s 是行内形式，本脚本只认块形式；手工改成块形式后重跑" % key)
    if cur:                       # `api-keys: []` → 换成块形式
        lines[i] = "%s:" % key
        lines.insert(i + 1, "  - %s" % quoted)
        return True
    for j in range(i + 1, _block_end(lines, i)):
        m = re.match(r"^\s*-\s*(.*)$", lines[j])
        if m and _scalar(m.group(1)).strip('"\'') == value:
            return False
    lines.insert(i + 1, "  - %s" % quoted)
    return True


SKELETON = """\
# claude-tgbot 生成的 cliproxy 配置骨架。
# provider 条目由管理台经管理 API 写入（也只该由它写），手改容易被下一次管理 API 落盘覆盖。
# 重跑安装脚本只补齐下面这些骨架字段，绝不整文件重写。
# ⚠️ 出错的请求会把完整 prompt 明文写进 logs/，别把本目录放进 iCloud/Dropbox/Time Machine。
host: "127.0.0.1"
port: {port}
auth-dir: "{auth_dir}"
debug: false
logging-to-file: false
request-log: false
error-logs-max-files: 0
api-keys:
  - "{api_key}"
remote-management:
  allow-remote: false
  secret-key: "{mgmt_key}"
"""


def ensure_config(path, env, auth_dir):
    """返回 (是否改动, 改了哪些字段)。首次生成骨架，之后只按字段补齐/复位。"""
    if not Path(path).exists():
        _write600(path, SKELETON.format(port=env["CLIPROXY_PORT"], auth_dir=auth_dir,
                                        api_key=env["CLIPROXY_API_KEY"],
                                        mgmt_key=env["CLIPROXY_MGMT_KEY"]))
        return True, ["<新建骨架>"]
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    touched = []
    # 强制复位：安全面（host/allow-remote 决定要不要上局域网）＋隐私面（debug 会打半掩码上游 key，
    # 日志三件套决定 prompt 明文落不落盘）。这两类字段用户改错的代价是泄漏，所以每次装都拉回来。
    forced = [("host", '"127.0.0.1"'), ("port", env["CLIPROXY_PORT"]), ("debug", "false"),
              ("logging-to-file", "false"), ("request-log", "false"),
              ("error-logs-max-files", "0")]
    for k, v in forced:
        if set_top_scalar(lines, k, v):
            touched.append(k)
    if set_nested_scalar(lines, "remote-management", "allow-remote", "false"):
        touched.append("remote-management.allow-remote")
    # 凭据类只在缺失/为空时补：cliproxy 启动会把明文 secret-key 换成哈希，强制回写等于每次都白改一遍
    if set_top_scalar(lines, "auth-dir", '"%s"' % auth_dir, force=False):
        touched.append("auth-dir")
    if set_nested_scalar(lines, "remote-management", "secret-key",
                         '"%s"' % env["CLIPROXY_MGMT_KEY"], force=False):
        touched.append("remote-management.secret-key")
    if ensure_list_item(lines, "api-keys", env["CLIPROXY_API_KEY"]):
        touched.append("api-keys")
    if touched:
        _write600(path, "\n".join(lines) + "\n")
    elif os.name != "nt":
        os.chmod(path, 0o600)
    return bool(touched), touched


# ── CLI ───────────────────────────────────────────────────

def bin_path():
    exe = "cli-proxy-api.exe" if os_tag() == "windows" else "cli-proxy-api"
    return cliproxy_dir() / exe


def get(name):
    ver = read_version()
    if name == "version":
        return ver
    if name == "asset":
        return asset_name(ver)
    if name == "url":
        return download_url(ver, asset_name(ver))
    if name == "sha256":
        return expected_sha256(ver, asset_name(ver))
    if name == "home":
        return str(tgbot_home())
    if name == "dir":
        return str(cliproxy_dir())
    if name == "bin":
        return str(bin_path())
    if name == "config":
        return str(cliproxy_dir() / "config.yaml")
    if name == "env":
        return str(tgbot_home() / "hub.env")
    if name == "port":
        return (parse_env_file(tgbot_home() / "hub.env").get("CLIPROXY_PORT")
                or os.environ.get("CLIPROXY_PORT") or DEFAULT_PORT)
    raise BootstrapError("unknown_key", "没有这个值：%s" % name)


def bootstrap():
    home, cdir = tgbot_home(), cliproxy_dir()
    for d in (home, cdir, cdir / "logs", cdir / "auths"):
        _mkdir700(d)          # 700：出错请求会把完整 prompt 明文写进 logs/（[CPX] Q7）
    env, env_changed = ensure_hub_env(home / "hub.env")
    print("hub.env %s（口令只写进文件，不打印、不进日志）"
          % ("已生成" if env_changed else "已存在，原样保留"))
    changed, touched = ensure_config(cdir / "config.yaml", env, cdir / "auths")
    print("config.yaml %s" % ("已补齐骨架字段：" + "、".join(touched) if changed else "骨架字段已就位"))
    print("目录：%s（700）" % cdir)


def main(argv):
    try:
        if argv and argv[0] == "get":
            if len(argv) != 2:
                raise BootstrapError("usage", "用法：hub_bootstrap.py get <name>")
            sys.stdout.write(get(argv[1]) + "\n")
        elif argv:
            raise BootstrapError("usage", "用法：hub_bootstrap.py [get <name>]")
        else:
            bootstrap()
    except BootstrapError as e:
        sys.stderr.write(str(e) + "\n")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
