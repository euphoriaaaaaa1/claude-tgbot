# -*- coding: utf-8 -*-
"""bot 注册表：扫 ``configs/*.yml`` 派生（id / display_name / port / namespace_uuid）。

**唯一读取入口**（PLAN D1）：重启脚本、Windows 启动与注册脚本、``moments.bots_client``、
``chat_history`` 全部经本模块取值 —— 加 bot 只落一个 yml，零源码改动。
不新建 `bots.yml`：那是第二真相源，加/删 bot 要同时改两处，漂移是必然的。

CLI 契约（INTERFACE §6，契约即测试点）::

    python3 -m bots_registry --format=sh     # 每行 `<id>\\t<port>\\t<namespace_uuid>`
    python3 -m bots_registry --format=json   # [{"id","display_name","port","namespace_uuid","port_derived"}]

**没有任何可用 bot 时退出码 1** 是刻意的：启动脚本据此报错并非零退出，
而不是把空名单当"本来就没有 bot"静默成功 —— 那会让所有 bot 悄悄起不来。

**停用标记**：写侧只有 ``configs/<bot>.yml`` 顶层 ``enabled: false``。读取时还兼容旧版
``~/.claude/dispatcher/.disabled/<bot>`` 标记 —— **只读**，本模块绝不创建/删除/改写它；
两种标记任一成立即视为停用。本表不吐停用 bot，于是所有消费方（重启脚本 / Windows
启动与注册 / 门户名单）都不认它 —— "停了不被任何路径拉回来"就是这一处判定负责的，
不另设第二张开关表。

**令牌**：``<channelDir>/.env`` 的 ``TELEGRAM_BOT_TOKEN`` 与 ``<channelDir>/.api-token``
的读法也只在本模块实现一份。任何函数都只返回令牌值或 ``None``，绝不打印/记录它。
"""
import json
import os
import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent
BASE_PORT = 17801

# legacy 兜底表：**只读**，加 bot 不往这里加。
# namespace 绝不派生（uuid5(项目ns, bot_id) 会给 chenlulu 算出另一个值 →
# unifiedSessionUuid 变 → worker 丢整段会话历史）；端口同理，派生会把既有 bot 挤走。
LEGACY_PORTS = {"chenlulu": 17801}
LEGACY_NAMESPACES = {"chenlulu": "550e8400-e29b-41d4-a716-446655440001"}


# ---------------------------------------------------------------- 本机 dispatcher HTTP 口令（H-15）
# dispatcher 启动时把 32 字节随机口令写到 <channelDir>/.api-token（0600）。所有向
# 127.0.0.1:<dispatcher_port> 发请求的调用方都要读它并带上 X-Api-Token（或
# Authorization: Bearer）。读不到 → 记一行明确日志（含 api_token_missing），暴露问题，
# 绝不静默成功。
API_TOKEN_FILENAME = ".api-token"
TELEGRAM_TOKEN_KEY = "TELEGRAM_BOT_TOKEN"
LEGACY_DISABLED_DIR = "~/.claude/dispatcher/.disabled"   # 旧停用标记，只读兼容
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def disabled_dir():
    """旧停用标记目录。``DIRECTOR_DISABLED_DIR`` 是既有接缝（CBL bot_stop.py 同名）。"""
    return os.path.expanduser(os.environ.get("DIRECTOR_DISABLED_DIR") or LEGACY_DISABLED_DIR)


def safe_bot_id(bot_id):
    """bot id 会拼进文件路径：拒绝空/点/路径分隔符/NUL，防 ``../`` 越界。

    中文 bot id 合法（既有旧标记里出现过），所以不限制字符集，只拒绝路径语义。
    """
    return (isinstance(bot_id, str) and bot_id not in ("", ".", "..")
            and "/" not in bot_id and "\\" not in bot_id and "\x00" not in bot_id)


def default_channel_dir(bot_id):
    """bot 的通道目录，与 worker 插件同一套约定：``~/.claude/channels/<bot>``。

    不安全 bot id → ``None``（调用方据此拒绝读令牌，不拼路径）。
    """
    if not safe_bot_id(bot_id):
        return None
    return os.path.expanduser(os.path.join("~", ".claude", "channels", bot_id))


def legacy_disabled(bot_id):
    """旧版 ``.disabled/<bot>`` 标记是否命中。**只读**；不可读按未停用（fail-open）。"""
    if not safe_bot_id(bot_id):
        return False
    try:
        os.stat(os.path.join(disabled_dir(), bot_id))
        return True
    except (OSError, ValueError):
        return False


def read_dispatcher_token(bot_id, channel_dir=None):
    """读 ``<channelDir>/.api-token``；缺失/为空/路径越界 → ``None``。绝不返回值到日志。"""
    d = os.path.expanduser(channel_dir) if channel_dir else default_channel_dir(bot_id)
    if not d:
        return None
    try:
        with open(os.path.join(d, API_TOKEN_FILENAME), "r", encoding="utf-8") as f:
            return f.read().strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def read_telegram_token(bot_id, channel_dir=None):
    """读 ``<channelDir>/.env`` 的 ``TELEGRAM_BOT_TOKEN``；缺失/为空/越界 → ``None``。

    解析口径对齐 shell ``source``：忽略空行与 ``#`` 注释、允许 ``export`` 前缀、
    去掉成对引号；同名键以后出现的为准。**绝不打印令牌**。
    """
    d = os.path.expanduser(channel_dir) if channel_dir else default_channel_dir(bot_id)
    if not d:
        return None
    try:
        with open(os.path.join(d, ".env"), "r", encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return None
    value = None
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("export "):
            s = s[len("export "):].lstrip()
        if "=" not in s:
            continue
        key, raw = s.split("=", 1)
        if key.strip() != TELEGRAM_TOKEN_KEY:
            continue
        raw = raw.strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
            raw = raw[1:-1]
        value = raw or None
    return value


def dispatcher_auth_headers(bot_id, channel_dir=None):
    """口令头 ``{"X-Api-Token": <口令>}``；读不到回 ``{}``。日志里绝不出现口令值。"""
    tok = read_dispatcher_token(bot_id, channel_dir)
    return {"X-Api-Token": tok} if tok else {}


def configs_dir() -> Path:
    """配置根。测试接缝 ``HUB_CONFIGS_DIR``（INTERFACE §7），未设才走仓库 ``configs/``。

    本模块任何路径都经它取，绝不 expanduser 出真路径 —— 否则用例会去动生产配置。
    """
    return Path(os.environ.get("HUB_CONFIGS_DIR") or (REPO_ROOT / "configs"))


def _valid_port(raw):
    """端口值清洗。空串/非数字/越界一律当"没给"——宁可派生也不要拿垃圾端口去连别的服务。"""
    try:
        port = int(raw)
    except (TypeError, ValueError):
        return None
    return port if 0 < port < 65536 else None


def _env_port(bot_id):
    """``DISPATCHER_PORT_<BOT大写>``：口径与 moments.bots_client / scripts 一致。"""
    return _valid_port(os.environ.get("DISPATCHER_PORT_%s" % bot_id.upper()))


def _scan(warn=None):
    """读 ``configs/*.yml``（跳过 ``_`` 开头的 `_global.yml` 等），返回 ``[(bot_id, cfg)]``。

    id 取文件名（与 ``config_loader.list_enabled_bots`` 同规则）。单个 yml 坏掉不影响其余：
    记一行 ``skip <文件名>: <异常类名>`` 交给 ``warn``，**不带路径、不带异常正文**。
    """
    d = configs_dir()
    rows = []
    if not d.is_dir():
        return rows
    for path in sorted(d.glob("*.yml")):
        if path.name.startswith("_"):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            if not isinstance(cfg, dict):
                raise TypeError("yml 顶层不是映射")
        except Exception as e:
            if warn is not None:
                warn("skip %s: %s" % (path.name, type(e).__name__))
            continue
        rows.append((path.stem, cfg))
    return rows


def is_enabled(cfg, bot_id=None):
    """顶层 ``enabled`` 的判定：**没写字段 = 启用**（存量 bot 一个都不许被顺手停掉）。

    只有显式写了假值（``false`` / ``no`` / ``0`` / 空串）才算停用 —— 手写 ``enabled: 0``
    的人要的就是停，按 ``is False`` 判会把它当启用，正是"看着改了其实没生效"那类坑。
    给出 ``bot_id`` 时叠加旧版 ``.disabled/<bot>`` 只读标记；不传则只看 yml（旧调用口径）。
    """
    v = cfg.get("enabled")
    if v is not None and not bool(v):
        return False
    return not (bot_id is not None and legacy_disabled(bot_id))


def _resolve_ports(rows):
    """端口解析（F3，与 namespace 对称）：env → yml → legacy 表 → 派生。

    派生规则定死为「取 ≥17801 且**未被任何显式/legacy 端口占用**的最小空闲号，按 id 排序依次分配」。
    原稿的 ``17801 + index`` 会让新 bot 把 chenlulu 从 17801 挤到 17802，
    而消费方仍按注册表去连 —— **既有生产 bot 静默失联**。
    返回 ``(确定端口, 派生端口)`` 两张表，``port_derived`` 据后者判定。
    """
    fixed, need = {}, []
    for bot_id, cfg in rows:
        port = (_env_port(bot_id) or _valid_port(cfg.get("dispatcher_port"))
                or LEGACY_PORTS.get(bot_id))
        if port:
            fixed[bot_id] = port
        else:
            need.append(bot_id)
    taken = set(fixed.values())
    derived, nxt = {}, BASE_PORT
    for bot_id in need:
        while nxt in taken:
            nxt += 1
        if nxt > 65535:            # 现实里到不了；真到了也不发垃圾端口出去
            break
        derived[bot_id] = nxt
        taken.add(nxt)
    return fixed, derived


def _namespace(bot_id, cfg):
    """``namespace_uuid``：yml 字段 → legacy 兜底 → None（新 bot 建号时才生成 uuid4 写进 yml）。

    ``env BOT_NAMESPACE`` 的优先级由调用方保持：它是**单值** env（起 dispatcher 时逐 bot 注入），
    而本表是多 bot 表，在这里读它会把一个值套到所有 bot 头上。
    """
    ns = cfg.get("namespace_uuid")
    if isinstance(ns, str) and ns.strip():
        return ns.strip()
    return LEGACY_NAMESPACES.get(bot_id)


def bots(warn=None, include_disabled=False):
    """可用 bot，按 id 排序。CLI 与全部 Python 消费方共用这一个出口。

    ``enabled: false`` / 旧 ``.disabled`` 标记的 bot **默认不出现在这里** —— 这张表就是
    "该起哪些 bot"的名单，重启脚本、Windows 启动/注册脚本全按它拉起，不吐出去就等于停用生效。
    ``include_disabled=True`` 给"通讯录"类用途（端口/命名空间查询、管理台列表）用。

    过滤放在这里而不是 ``_scan``：端口按**全表**解析，停用的 bot 仍占着自己的号，
    停一个 bot 不会让别人的派生端口漂移，重新启用时端口也还是原来那个
    （端口一漂，消费方就照注册表去连了另一个服务 —— 与 F3 同一条理由）。
    """
    rows = _scan(warn)
    fixed, derived = _resolve_ports(rows)
    out = []
    for bot_id, cfg in rows:
        if not include_disabled and not is_enabled(cfg, bot_id):
            continue
        port = fixed.get(bot_id) or derived.get(bot_id)
        if port is None:                       # 端口耗尽的极端情况：宁可不列
            continue
        name = cfg.get("display_name")
        ok = isinstance(name, str) and name.strip()
        out.append({"id": bot_id,
                    "display_name": name.strip() if ok else bot_id,
                    "port": port,
                    "namespace_uuid": _namespace(bot_id, cfg),
                    "port_derived": bot_id in derived})
    return out


def ports():
    """``{bot_id: port}``。moments 侧端口表的唯一来源。

    **含停用的 bot**：这是"谁在哪个端口"的通讯录，不是"该起哪些"的名单。
    漏掉停用 bot 会让管理台把它显示成 unknown（查不到端口），而它真实状态是 offline；
    也会让端口占用检查放行一个撞号的新 bot。
    """
    return {b["id"]: b["port"] for b in bots(include_disabled=True)}


def disabled_ids():
    """被 ``enabled: false`` 或旧 ``.disabled/<bot>`` 标记停用的 bot id 集合。

    管理台按它给行打"已停用"徽标。旧标记只读，本函数绝不创建/删除它。
    """
    return {bot_id for bot_id, cfg in _scan() if not is_enabled(cfg, bot_id)}


_safe_warned = False


def disabled_ids_safe():
    """``disabled_ids()`` 的 fail-open 包装：后台生产者（导演 / 主动消息 / 朋友圈 / 语音）判"停用"的唯一入口。

    逐文件容错由 ``_scan`` 负责（别的 bot 的 yml 坏了不影响本 bot 被判停用）；这里只兜目录级异常：
    整个 configs 读不了 → 空集（= 都当启用，退回改动前行为），stderr 只报一次类名。
    """
    global _safe_warned
    try:
        d = configs_dir()
        if d.is_dir():
            os.listdir(d)   # glob 会静默吞掉 PermissionError：显式探一次，让"目录读不了"可观测
        return disabled_ids()
    except Exception as e:
        if not _safe_warned:
            _safe_warned = True
            sys.stderr.write("disabled_ids failed: %s\n" % type(e).__name__)
        return set()


def namespace_for(bot_id):
    """``bot_id`` → namespace uuid；yml 里没有就回 legacy 兜底，再没有回 None。

    同 ``ports()`` 含停用 bot：命名空间是身份，停用期间也不该查不到
    （查不到 = 重新启用后 worker 认不出自己的记忆）。
    """
    for b in bots(include_disabled=True):
        if b["id"] == bot_id:
            return b["namespace_uuid"]
    return LEGACY_NAMESPACES.get(bot_id)


def namespace(bot_id):
    """CBL ``bot_registry.namespace()`` 的兼容名；语义同 ``namespace_for``。"""
    return namespace_for(bot_id)


def username(bot_id):
    """``telegram_username``（不带 @）；没有或不合规 → None。

    CBL ``bot_registry.username()`` 的兼容面：v2 只保留一个注册表模块，
    尚未移植的 adapters 代码按这个名字调用。
    """
    if not safe_bot_id(bot_id):
        return None
    for bid, cfg in _scan():
        if bid != bot_id:
            continue
        v = cfg.get("telegram_username")
        if isinstance(v, str) and _USERNAME_RE.fullmatch(v.strip()):
            return v.strip()
        return None
    return None


def entries():
    """CBL ``bot_registry.entries()`` 的兼容面：启用 bot 的字段表，按 id 排序。

    字段沿用 CBL 的 ``bot_id/id/dispatcher_port/bot_channel_path/namespace_uuid/
    telegram_username/launcher``；``dispatcher_port`` 是字符串（CBL 原口径）。
    未在 yml 里出现的可选字段记 None，``launcher`` 只认 ``registry``。
    v2 的新代码请直接用 ``bots()`` / ``ports()``，这里只服务尚未移植的 CBL 调用点。
    """
    cfg_by_id = {bot_id: cfg for bot_id, cfg in _scan()}
    out = []
    for item in bots():
        cfg = cfg_by_id.get(item["id"], {})
        channel_path = cfg.get("bot_channel_path")
        out.append({
            "bot_id": item["id"],
            "id": item["id"],
            "dispatcher_port": str(item["port"]),
            "bot_channel_path": (channel_path if isinstance(channel_path, str)
                                 and "\n" not in channel_path and "\x00" not in channel_path
                                 else None),
            "namespace_uuid": item["namespace_uuid"],
            "telegram_username": username(item["id"]),
            "launcher": "registry",
        })
    return out


def reload():
    """CBL ``bot_registry.reload()`` 的兼容面。

    v1 的 ``bot_registry`` 维护一张原地刷新的 ``BOT_PORTS``；v2 的 ``ports()`` 每次现读，
    没有缓存，因此这里只需返回当前端口表。**尚未移植的 provider_config 不要缓存返回值**，
    直接每次调用 ``ports()`` —— 缓存快照会在加/删 bot 后漂移。
    """
    return ports()


_USAGE = "用法：python3 -m bots_registry [--format=sh|json]\n"


def main(argv=None):
    # 中文 Windows 控制台默认 cp936，display_name 里一个 emoji 就让 --format=json 整个
    # UnicodeEncodeError 崩掉 → 调用方（start-bots.ps1 / register-tasks.ps1）读不到名单，
    # 一个 bot 都起不来。hasattr 判一下：pytest 的捕获流不一定有 reconfigure。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    fmt = "json"
    for a in argv:
        if not a.startswith("--format="):
            sys.stderr.write(_USAGE)
            return 2
        fmt = a.split("=", 1)[1]
    if fmt not in ("sh", "json"):
        sys.stderr.write(_USAGE)
        return 2
    rows = bots(warn=lambda m: sys.stderr.write(m + "\n"))
    if fmt == "json":
        sys.stdout.write(json.dumps(rows, ensure_ascii=False) + "\n")
    else:
        for b in rows:
            sys.stdout.write("%s\t%d\t%s\n" % (b["id"], b["port"],
                                               b["namespace_uuid"] or ""))
    return 0 if rows else 1        # 空名单 = 1：消费方据此报错，不静默起 0 个 bot


if __name__ == "__main__":
    sys.exit(main())
