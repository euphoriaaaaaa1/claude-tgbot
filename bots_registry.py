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
"""
import json
import os
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


def bots(warn=None):
    """全部可用 bot，按 id 排序。CLI 与全部 Python 消费方共用这一个出口。"""
    rows = _scan(warn)
    fixed, derived = _resolve_ports(rows)
    out = []
    for bot_id, cfg in rows:
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
    """``{bot_id: port}``。moments 侧端口表的唯一来源。"""
    return {b["id"]: b["port"] for b in bots()}


def namespace_for(bot_id):
    """``bot_id`` → namespace uuid；yml 里没有就回 legacy 兜底，再没有回 None。"""
    for b in bots():
        if b["id"] == bot_id:
            return b["namespace_uuid"]
    return LEGACY_NAMESPACES.get(bot_id)


_USAGE = "用法：python3 -m bots_registry [--format=sh|json]\n"


def main(argv=None):
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
