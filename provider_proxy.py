# -*- coding: utf-8 -*-
"""代理实例编排内核（**无 Flask 依赖**；**永不 import provider_config**，依赖单向）。

本模块负责 config.yaml 的生成与实例生命周期（写盘 / 核验 / kickstart / 状态探测）。
providers.json 的读写在 ``provider_config`` 侧——本模块只**消费**传入的快照。

接缝（运行期读取，**不得在模块顶层缓存**——测试会在运行期改这些 env）：

    CLAUDEBOT_PROXY_DIR       代理运行目录（缺省 ~/Library/Application Support/claudebotlife-provider-proxy）
    CLAUDEBOT_PROXY_PORT      实例端口（缺省 8770；``0`` = 不真起实例）
    CLAUDEBOT_PROXY_BIN       二进制路径（缺省 <运行目录>/cli-proxy-api）
    CLAUDEBOT_PROXY_LAUNCHCTL ``off`` = 不执行任何 launchctl（生产缺省 ``on``）
"""
from __future__ import annotations

import contextlib
import os
import sys
import threading
from typing import NamedTuple
from urllib.parse import urlsplit

import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_PORT = 8770
LAUNCHD_LABEL = "com.example.claudebotlife-provider-proxy"

# 实例的固定头（§3 落地文件契约）
_HOST = "127.0.0.1"


# ── 接缝（运行期读取）──────────────────────────────────────────────────────

_TLS = threading.local()


class Seams(NamedTuple):
    """不可变接缝快照：请求线程进入写入段时解析一次；写入段与 worker 只认它（R7/9.12）。"""
    run_dir: str
    providers: str
    port: int
    bin: str
    launchctl: str
    plist: str
    verify_timeout: float
    gated: bool


def _env(name: str):
    """唯一的环境变量读取口。核验 worker 线程读环境变量即是缺陷（测试恢复 env 后会解析到真实目录）。"""
    if getattr(_TLS, "worker", False):
        raise RuntimeError("核验 worker 不得读环境变量: %s" % name)
    return os.environ.get(name)


def _cur() -> "Seams | None":
    return getattr(_TLS, "seams", None)


@contextlib.contextmanager
def _using(seams: "Seams | None"):
    prev, _TLS.seams = _cur(), seams
    try:
        yield seams
    finally:
        _TLS.seams = prev


GATE_VARS = ("CLAUDEBOT_PROVIDER_PATH", "CLAUDEBOT_PROXY_DIR", "CLAUDEBOT_PROXY_PORT",
             "CLAUDEBOT_PROXY_BIN", "CLAUDEBOT_PROXY_LAUNCHCTL")


def gate_env_active() -> bool:
    """本进程是否测试态（TEST_MODE=1 或设了任一代理/清单接缝）。生产 env 恒 False。"""
    return _env("CLAUDEBOT_TEST_MODE") == "1" or any(_env(k) is not None for k in GATE_VARS)


def real_default_paths() -> tuple[str, str]:
    """「真实默认位置」：仓库 configs/providers.json 与真实主目录（按账户数据库取，不看 HOME）下的缺省运行目录。"""
    import pwd
    home = pwd.getpwuid(os.getuid()).pw_dir
    return (os.path.join(_HERE, "configs", "providers.json"),
            os.path.join(home, "Library", "Application Support", "claudebotlife-provider-proxy"))


GATE_MANIFEST, GATE_RUN_DIR = "manifest", "run_dir"


def gate_branch(gated: bool, manifest_path: str, run_dir: str, touches, real=None) -> str | None:
    """9.4 安全闸纯判定（请求侧与 worker 侧共用），返回命中支：清单指向仓库默认 → ``GATE_MANIFEST``
    （500 零写入）；运行目录解析到真实缺省位置且 ``touches()``（惰性，勘误 6）→ ``GATE_RUN_DIR``
    （勘误 8：跳过代理重建）；未命中 → None。"""
    if not gated:
        return None
    manifest, real_run = real or real_default_paths()
    if os.path.realpath(manifest_path) == os.path.realpath(manifest):
        return GATE_MANIFEST
    if os.path.realpath(run_dir) == os.path.realpath(real_run) and touches():
        return GATE_RUN_DIR
    return None


def resolve_seams() -> Seams:
    """一次性解析全部接缝（请求线程 / 面板启动时调用）。"""
    with _using(None):
        return Seams(proxy_dir(), providers_path(), proxy_port(), proxy_bin(),
                     _env("CLAUDEBOT_PROXY_LAUNCHCTL") or "", os.path.expanduser(PLIST_PATH),
                     _verify_timeout(), gate_env_active())


def _gate_hit() -> str | None:
    """快照侧安全闸（9.4）：写入段 / kickstart 前用快照复判；走到这里必触及运行目录。无快照按清单支拒。"""
    s = _cur()
    return GATE_MANIFEST if s is None else gate_branch(s.gated, s.providers, s.run_dir, lambda: True)


def proxy_dir() -> str:
    if _cur():
        return _cur().run_dir
    v = _env("CLAUDEBOT_PROXY_DIR")
    if v:
        return v
    return os.path.expanduser("~/Library/Application Support/claudebotlife-provider-proxy")


def proxy_port() -> int:
    """当前配置端口 = ``CLAUDEBOT_PROXY_PORT``（缺省 8770）。非法值回落缺省。"""
    if _cur():
        return _cur().port
    raw = _env("CLAUDEBOT_PROXY_PORT")
    if raw is None or raw == "":
        return DEFAULT_PORT
    try:
        return int(raw)
    except (TypeError, ValueError):
        return DEFAULT_PORT


def proxy_bin() -> str:
    if _cur():
        return _cur().bin
    v = _env("CLAUDEBOT_PROXY_BIN")
    return v if v else os.path.join(proxy_dir(), "cli-proxy-api")


def launchctl_enabled() -> bool:
    lc = _cur().launchctl if _cur() else _env("CLAUDEBOT_PROXY_LAUNCHCTL")
    return (lc or "on").lower() != "off"


def config_path() -> str:
    return os.path.join(proxy_dir(), "config.yaml")


def state_path() -> str:
    return os.path.join(proxy_dir(), "state.json")


def providers_path() -> str:
    """providers.json 的落点（与 provider_config 同一套 env 规则）。

    本模块只在 ``sync()`` 未收到快照时自读一次兜底；正常路径由调用方（路由层）在
    ``provider_config`` 锁内取好快照传入。
    """
    if _cur():
        return _cur().providers
    v = _env("CLAUDEBOT_PROVIDER_PATH")
    if v:
        return v
    return os.path.join(_HERE, "configs", "providers.json")


def base_url() -> str:
    return "http://%s:%d" % (_HOST, proxy_port())


# ── config.yaml 生成（纯函数）──────────────────────────────────────────────

def _normalize_protocol(value) -> str:
    """协议取值归一：``strip().lower()`` 后命中 anthropic/openai 才算数，否则 ``""``。"""
    if not isinstance(value, str):
        return ""
    value = value.strip().lower()
    return value if value in ("anthropic", "openai") else ""


def _protocol_from_base_url(base_url, anthropic_base_url=None) -> str:
    """供应商未显式给协议时的地址兜底（与 dsh 侧同一规则）。

    ``base_url`` 路径含 ``anthropic`` 段 → ``anthropic``；否则 ``anthropic_base_url``
    有值且其路径含 ``anthropic`` 段 → ``anthropic``；否则 ``openai``。
    """
    for candidate in (base_url, anthropic_base_url):
        if not isinstance(candidate, str) or not candidate:
            continue
        try:
            path = urlsplit(candidate).path
        except ValueError:
            continue
        if any(seg.lower() == "anthropic" for seg in path.split("/")):
            return "anthropic"
    return "openai"


def _entry_protocol(entry: dict) -> str:
    """条目**有效协议**（provider 级）——与 ``provider_config.entry_protocol`` 同一语义的镜像。

    本模块是 providers.json 的**消费方**，设计上不 import provider_config（见 ``_load_snapshot``）。
    显式 ``protocol``（归一后）优先；否则按地址兜底（``base_url`` 或
    ``anthropic_base_url`` 路径含 ``anthropic`` 段则 anthropic，否则 openai），
    **永不返回空串**。旧 ``protocols`` 表不再参与默认判定。
    """
    explicit = _normalize_protocol(entry.get("protocol"))
    if explicit:
        return explicit
    return _protocol_from_base_url(entry.get("base_url"), entry.get("anthropic_base_url"))


def _bucket(entry: dict) -> tuple[list, list]:
    """按条目**有效协议**把模型整桶分到 (anthropic 桶, openai 桶)。

    provider 级：该 provider 的全部模型进**唯一**对应块，另一桶恒空（INTERFACE §3）。
    协议归一/地址兜底后总有值（anthropic 或 openai），两桶都空的分支仅为防御性保留。
    """
    protocol = _entry_protocol(entry)
    models = []
    for m in entry.get("models") or []:
        mid = m.get("id") if isinstance(m, dict) else (m if isinstance(m, str) else None)
        if isinstance(mid, str) and mid:
            models.append(mid)
    if protocol == "anthropic":
        return models, []
    if protocol == "openai":
        return [], models
    return [], []


def build_config(snapshot: dict, generation_key: str = "") -> str:
    """``providers.json`` 全量快照 → ``config.yaml`` 文本（**纯函数、全量重建**）。

    全量重建 ⇒ 无「漏删旧条目」残留。输出方式是**结构化构造 + ``yaml.safe_dump``**，
    **绝不字符串拼接**：``name``/``model``/``base-url`` 是用户可控串，拼串方案下模型名
    里塞换行就能追加一个新的配置块（F5/I5）。

    两个块各自用**自己的 base-url**（§3.1 / §5.2）：``claude-api-key`` 用 anthropic_url、
    ``openai-compatibility`` 用 openai_url，两者都可由条目字段显式覆盖。
    某块无模型则**不生成该键**（I8：宁可没有块，也不写真 ``models: []``）。
    """
    from scripts.derive_endpoints import effective_endpoints

    meta = snapshot.get("proxy") if isinstance(snapshot.get("proxy"), dict) else {}
    key = meta.get("instance_key")
    obj: dict = {
        "host": _HOST,
        "port": proxy_port(),
        "auth-dir": os.path.join(os.path.abspath(proxy_dir()), "auths"),   # v6.9.0 缺它启动即退出（9.13）
        # 9.2：面板写入恰为 [instance_key, 本代代号 key]；代号 key 只在 config.yaml 与内存。
        "api-keys": [k for k in (key if isinstance(key, str) else "", generation_key) if k],
        "remote-management": {"allow-remote": False, "disable-control-panel": True},
    }

    anthropic_entries, openai_entries = [], []
    for entry in snapshot.get("providers") or []:
        if not isinstance(entry, dict) or entry.get("proxy") is not True:
            continue
        pid = entry.get("id")
        if not isinstance(pid, str) or not pid:
            continue
        api_key = entry.get("api_key")
        base = entry.get("base_url")
        a_url, o_url = effective_endpoints(
            base if isinstance(base, str) else "",
            entry.get("anthropic_base_url") or "",
            entry.get("openai_base_url") or "",
        )
        # E-15（裁定：保旧锁定契约）：openai 块保持既有约定——送入值原样（要 /v1 由用户自己带）。
        # 「地址可能少了 /v1」由保存/绑定响应的 warning + 面板提示承担（见 provider_config），
        # 不在这里擅自替用户补后缀。
        a_models, o_models = _bucket(entry)
        if a_models:
            anthropic_entries.append({
                "api-key": api_key,
                "base-url": a_url,
                "prefix": pid,
                "models": [{"name": m, "alias": m} for m in a_models],
            })
        if o_models:
            openai_entries.append({
                "name": pid,
                "prefix": pid,
                "base-url": o_url,
                "api-key-entries": [{"api-key": api_key}],
                "models": [{"name": m, "alias": m} for m in o_models],
            })

    if anthropic_entries:
        obj["claude-api-key"] = anthropic_entries
    if openai_entries:
        obj["openai-compatibility"] = openai_entries

    return yaml.safe_dump(obj, allow_unicode=True, sort_keys=False)


def proxy_providers(snapshot: dict) -> list:
    """快照里 ``proxy=true`` 的条目（供计数与 no-op 判定）。"""
    return [p for p in (snapshot.get("providers") or [])
            if isinstance(p, dict) and p.get("proxy") is True]


# ── 落盘 / 核验 / 生命周期（§5.5）──────────────────────────────────────────

# 一把编排锁：整个「读快照 → build → 写 → 核验」在锁内完成（I3）。
# 快照若在锁外读，两个并发 sync 会各读同一份旧快照，后者用旧数据覆盖前者 ⇒ 静默丢更新。
import hashlib  # noqa: E402
import json  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import http.client  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

_CONFIG_LOCK = threading.Lock()

_LAST_SYNC_AT: int | None = None
_LAST_ERROR: str | None = None

# 文案（唯一构造处，测试逐字比较）：INTERFACE §2.3。
ERR_RELOAD_VERIFY_FAIL = "代理重载核验失败"
ERR_BIN_MISSING = "代理二进制缺失或哈希失配"
ERR_CONFIG_UNPARSABLE = "生成的配置无法解析"
ERR_SELFCHECK = "代理配置自检失败: %s"
ERR_SEAMS = "测试接缝必须成组设置"   # 与 provider_config._SEAM_ERROR 同文案（9.4）
ERR_SEAMS_SKIPPED = "测试接缝必须成组设置（已跳过代理重建）"   # 勘误 8：运行目录支

# 代号（R9）：max(上一代号+1, 当前 Unix 毫秒)，初值为面板启动毫秒；每代一个随机代号 key。
import secrets  # noqa: E402
_GEN_LAST = int(time.time() * 1000)
_GEN_KEY: str | None = None
_LAST_ERROR_GEN: int | None = 0   # 9.15：last_error_generation 初值 0


_REQ = threading.local()   # 本请求线程分配到的代号（供路由层回 X-Proxy-Generation 头）


def take_request_generation() -> int | None:
    """取出并清空本线程记下的代号；无则 None（纯 no-op / 未进写入段）。"""
    g = getattr(_REQ, "gen", None)
    _REQ.gen = None
    return g


def _next_generation() -> tuple[int, str]:
    """调用方须持 _CONFIG_LOCK。返回 (代号, 本代代号 key)；作废的代号不回收。"""
    global _GEN_LAST
    _GEN_LAST = max(_GEN_LAST + 1, int(time.time() * 1000))
    _REQ.gen = _GEN_LAST
    return _GEN_LAST, secrets.token_urlsafe(24)


def _selfcheck(cfg, instance_key: str, gen_key: str) -> str | None:
    """替换前自检五项（9.5），返回第一个不满足的项名；重名不在此列（9.7 在提交清单时拦）。"""
    cfg = cfg if isinstance(cfg, dict) else {}
    if not instance_key or cfg.get("api-keys") != [instance_key, gen_key]:
        return "api-keys"
    ad = cfg.get("auth-dir")
    if not (isinstance(ad, str) and os.path.isabs(ad)):
        return "auth-dir"
    if cfg.get("host") != _HOST:
        return "host"
    if cfg.get("port") != proxy_port():
        return "port"
    rm = cfg.get("remote-management")
    if isinstance(rm, dict) and rm.get("secret-key"):
        return "secret-key"
    return None


def _tcp_reachable(port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((_HOST, port), timeout=timeout):
            return True
    except OSError:
        return False

# 核验轮询参数（I7）：300ms 起步、指数退避上限 2s、总超时 10s。
_VERIFY_TIMEOUT = 10.0
_VERIFY_INTERVAL0 = 0.3
_VERIFY_CAP = 2.0


def _open_private(path: str) -> int:
    """0600 独占创建（明文 key 不得先落在 0644 inode 上，与 provider_config 同款）。"""
    for _ in range(2):
        try:
            return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                os.remove(path)
            except OSError:
                pass
    raise OSError("无法创建私有临时文件: %s" % path)


def _atomic_write(path: str, text: str) -> None:
    """同目录 tmp → fsync → os.replace（同目录保证 rename 原子）；权限 0600。"""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d or ".", ".%s.tmp-%d-%d" % (
        os.path.basename(path), os.getpid(), int(time.time() * 1000)))
    fd = _open_private(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _instance_key_from_manifest() -> str:
    """从 providers.json 顶层 ``proxy.instance_key`` 取实例 key（**只读、不生成**）。

    本模块永不 import provider_config，故自己读这份清单；key 的**生成**在
    ``provider_config.ensure_instance_key`` 侧（写路径归它）。
    """
    try:
        with open(providers_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return ""
    meta = data.get("proxy") if isinstance(data, dict) else None
    key = meta.get("instance_key") if isinstance(meta, dict) else None
    return key if isinstance(key, str) else ""


_MAX_BODY = 4 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):   # 3xx 不跟随 → HTTPError(3xx) → 判未加载
        return None


# 显式空 ProxyHandler：核验绝不经任何环境代理（9.3，B10）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def _probe_models(port: int, key: str, timeout: float = 3.0):
    """单次核验请求（9.3）。返回 (状态码, id 集合)；200 且正文合法才有集合。
    超时/连接失败/超 4 MiB/坏 JSON → (None, None)，从不抛异常。http.client 负责 Content-Length 与 chunked。"""
    if port <= 0:
        return None, None
    req = urllib.request.Request("http://%s:%d/v1/models" % (_HOST, port),
                                 headers={"Authorization": "Bearer " + key, "Accept": "application/json"})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            status, body = r.status, r.read(_MAX_BODY + 1)
    except urllib.error.HTTPError as e:
        e.close()
        return e.code, None
    except (OSError, ValueError, http.client.HTTPException):
        return None, None
    if len(body) > _MAX_BODY:
        return None, None
    if status != 200:
        return status, None
    try:
        items = json.loads(body.decode("utf-8"))["data"]
        return 200, {it["id"] for it in items if isinstance(it, dict) and isinstance(it.get("id"), str)}
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return None, None


def _fetch_alias_set(port: int, key: str, timeout: float = 3.0):
    """``/v1/models`` 的 id 集合；非 200 或任何异常 → ``None``。"""
    return _probe_models(port, key, timeout)[1]


def _expected_pairs(snapshot: dict) -> set:
    """E 的来源：{(pid, m)}，只含进了 anthropic/openai 桶的模型（9.3）。"""
    return {tuple(a.split("\0")) for a in _pair_keys(snapshot)}


def _pair_keys(snapshot: dict) -> set:
    out = set()
    for entry in proxy_providers(snapshot):
        pid = entry.get("id")
        if not isinstance(pid, str) or not pid:
            continue
        # 协议已选（含派生）→ 该 provider 的**全部**模型进 E（INTERFACE §4：不再按模型协议过滤）。
        if _entry_protocol(entry) not in ("anthropic", "openai"):
            continue
        for m in entry.get("models") or []:
            mid = m.get("id") if isinstance(m, dict) else (m if isinstance(m, str) else None)
            if isinstance(mid, str) and mid:
                out.add(pid + "\0" + mid)
    return out


def _expected_aliases(snapshot: dict) -> set:
    """期望集合 E = ∪{m, pid/m}（9.3）。"""
    return {x for pid, m in _expected_pairs(snapshot) for x in (m, pid + "/" + m)}


# 「上一代」= 最近一次判为已加载的代（R4）：{"key": 代号 key, "pairs": {(pid, m)} 或 None=未知}。
_LOADED: dict | None = None


def _boot_loaded(instance_key: str) -> dict:
    """面板启动后首次写入前调用：取启动时 config.yaml 的 api-keys[1]（存在且 ≠ instance_key），否则随机错 key。"""
    key = None
    try:
        with open(config_path(), encoding="utf-8") as f:
            ks = (yaml.safe_load(f) or {}).get("api-keys")
        if isinstance(ks, list) and len(ks) > 1 and isinstance(ks[1], str) and ks[1] and ks[1] != instance_key:
            key = ks[1]
    except (OSError, yaml.YAMLError, AttributeError):
        pass
    return {"key": key or secrets.token_urlsafe(24), "pairs": None}


def _loaded_once(port: int, pairs: set, gen_key: str) -> bool:
    """判据五条（9.3）一次性判定。"""
    st, ids = _probe_models(port, gen_key)
    if st != 200 or ids is None:
        return False
    if _probe_models(port, (_LOADED or {}).get("key") or secrets.token_urlsafe(24))[0] != 401:
        return False
    if pairs and not ids:
        return False
    prev = (_LOADED or {}).get("pairs")
    added = pairs if prev is None else pairs - prev
    if any(pid + "/" + m not in ids for pid, m in added):
        return False
    still = {m for _, m in pairs}
    for pid, m in (set() if prev is None else prev - pairs):
        # 裸名仍被别的 provider 期望时不要求消失（否则永远判不过）
        if pid + "/" + m in ids or (m in ids and m not in still):
            return False
    return True


def _verify_reloaded(pairs: set, gen_key: str, timeout: float = _VERIFY_TIMEOUT) -> bool:
    """写后轮询核验：间隔 300ms 起、指数退避上限 2s、总超时 timeout（I7）。port ≤ 0 立即 False。
    判为已加载时把本代记为「上一代」。"""
    global _LOADED
    port = proxy_port()
    if port <= 0:
        return False
    deadline = time.monotonic() + timeout
    interval = _VERIFY_INTERVAL0
    while True:
        if _loaded_once(port, pairs, gen_key):
            _LOADED = {"key": gen_key, "pairs": set(pairs)}
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))
        interval = min(interval * 2, _VERIFY_CAP)


def _kickstart() -> bool:
    """兜底重启实例（launchctl）。``CLAUDEBOT_PROXY_LAUNCHCTL=off`` 时**不执行任何 launchctl**。"""
    if not launchctl_enabled() or _gate_hit():
        return
    lc = _cur().launchctl
    exe = lc if os.path.isabs(lc) else "launchctl"   # 桩接缝（R16）：绝对路径代替 launchctl，参数相同
    global _KICK_OK
    try:   # 非 0 退出或 5 s 超时即失败（9.15 restart 的 500 判据）
        _KICK_OK = subprocess.run(
            [exe, "kickstart", "-k", "gui/%d/%s" % (os.getuid(), LAUNCHD_LABEL)],
            capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.SubprocessError):
        _KICK_OK = False
    return _KICK_OK


def _check_binary() -> str | None:
    """二进制存在性与 sha256 校验（R8）。缺失/失配 → 文案；否则 ``None``。

    **只在有 proxy provider 时调用**（no-op 路径不碰盘）。首个成功 sync 会记下当前
    sha256 到 ``state.json``（非密钥元数据）；此后不符即报错，**不静默降级**。
    """
    path = proxy_bin()
    if not os.path.isfile(path):
        return ERR_BIN_MISSING
    digest = _bin_sha(path)
    if digest is None:
        return ERR_BIN_MISSING
    stored = _read_state()
    if stored.get("binary_sha256") and stored["binary_sha256"] != digest:
        return ERR_BIN_MISSING
    return None


def _read_state() -> dict:
    try:
        with open(state_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(**kw) -> None:
    """镜像**非密钥**元数据（端口 / 二进制 sha256 等）。**绝不写 instance_key**（I2）。"""
    data = _read_state()
    data.update(kw)
    try:
        _atomic_write(state_path(), json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    except OSError:
        pass


def _load_snapshot() -> dict:
    """``sync()`` 未收到快照时的兜底自读（仍是消费方，不 import provider_config）。"""
    try:
        with open(providers_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"providers": []}
    return data if isinstance(data, dict) else {"providers": []}


_IDLE = {"ok": True, "reloaded": False, "providers_count": 0, "models_count": 0, "last_error": None}
ERR_NOT_INSTALLED = "代理实例未安装"
ERR_SHRINK_RESTART = "已删除的 key 可能仍在实例内存中，请重启实例"
_BACKOFF = (60, 120, 240, 300)
_RETRY_MAX = 5   # 补跑上限（E-16）：核验持续失败时，到限即停止补跑、只保留 last_error
_KICK_INTERVAL = 60.0
PLIST_PATH = os.path.join("~", "Library", "LaunchAgents", LAUNCHD_LABEL + ".plist")

# ── 两段式（9.5）：写入段在请求内同步完成；核验段交唯一的核验 worker 串行执行 ──
_STATE = threading.Condition()   # 保护下列 worker 状态；任何等待都不持配置锁
_JOB: dict | None = None         # 最新待核验代
_RESULT: tuple | None = None     # (代号, 结果)：显式请求等它
_VERIFIED_GEN = 0                # 只增不减
_LAST_KICK = float("-inf")
_VERIFIED_E: set = set()         # verified_generation 那代的 E（裸名），供 suspended（9.15）
_SUSP_CACHE: tuple = (0.0, None)  # (monotonic, suspended) 缓存 2 s
_KICK_OK = True                  # 最近一次 kickstart 是否成功（restart 判 500 用）
_LAST_SEAMS = None             # 最近一次提交任务的接缝快照（到点补跑沿用）
_RETRY_N, _RETRY_AT = 0, None
_WINDOW, _WIN_GEN = False, None  # 启动窗口：窗口内写入段只分配（共用）代号
_PREV_WRITTEN: dict | None = None
_SHRINK_FORCED: set = set()
_WORKER: threading.Thread | None = None


def _verify_timeout() -> float:
    if _cur():
        return _cur().verify_timeout
    try:
        return max(0.1, float(_env("CLAUDEBOT_PROXY_VERIFY_TIMEOUT") or 5))
    except ValueError:
        return 5.0


def _plist_exists() -> bool:
    return os.path.exists(_cur().plist if _cur() else os.path.expanduser(PLIST_PATH))


def _write_stage(snap: dict | None = None, gen: int | None = None) -> tuple[str, dict]:
    """配置锁只覆盖「读清单→生成→自检→replace」。返回 (kind, info)，kind ∈
    noop / error / deferred（窗口内只分配代号）/ written。"""
    global _LAST_ERROR, _GEN_KEY, _LAST_ERROR_GEN, _LOADED, _WIN_GEN, _PREV_WRITTEN
    with _CONFIG_LOCK:
        snap = snap if snap is not None else _load_snapshot()
        pro = proxy_providers(snap)
        if not pro and not os.path.exists(config_path()):
            return "noop", dict(_IDLE)   # 纯 no-op（9.4）
        hit = _gate_hit()   # 分配代号之前判（勘误 8）：运行目录支跳过重建，清单支拒写
        if hit == GATE_RUN_DIR:
            _LAST_ERROR = ERR_SEAMS_SKIPPED
            return "noop", dict(_IDLE, last_error=ERR_SEAMS_SKIPPED)
        if hit:
            _LAST_ERROR = ERR_SEAMS
            return "error", {"ok": False, "error": ERR_SEAMS}
        if pro:
            err = _check_binary()
            if err:
                _LAST_ERROR = err
                return "error", {"ok": False, "error": err}
        if _WINDOW and gen is None:
            if _WIN_GEN is None:
                _WIN_GEN = _next_generation()[0]
            _REQ.gen = _WIN_GEN
            return "deferred", {"gen": _WIN_GEN, "count": len(pro)}
        if gen is None:
            gen, gen_key = _next_generation()
        else:
            gen_key = secrets.token_urlsafe(24)
        meta = snap.get("proxy") if isinstance(snap.get("proxy"), dict) else {}
        instance_key = meta.get("instance_key") if isinstance(meta.get("instance_key"), str) else ""
        if _LOADED is None:   # 首次写入前记下启动时 config 的代号 key 作「上一代」（9.3 判据 2）
            _LOADED = _boot_loaded(instance_key)
        text = build_config(snap, gen_key)
        try:
            bad = _selfcheck(yaml.safe_load(text), instance_key, gen_key)
        except yaml.YAMLError:
            _LAST_ERROR = ERR_CONFIG_UNPARSABLE
            return "error", {"ok": False, "error": ERR_CONFIG_UNPARSABLE}
        if bad:   # 不写盘、本代作废（9.5）
            _LAST_ERROR, _LAST_ERROR_GEN = ERR_SELFCHECK % bad, gen
            return "error", {"ok": False, "error": _LAST_ERROR}
        if pro:
            for sub in ("", "auths", "logs"):
                try:
                    os.makedirs(os.path.join(proxy_dir(), sub) if sub else proxy_dir(), exist_ok=True)
                except OSError:
                    pass
        try:
            _atomic_write(config_path(), text)
        except OSError:
            _LAST_ERROR = ERR_CONFIG_UNPARSABLE
            return "error", {"ok": False, "error": ERR_CONFIG_UNPARSABLE}
        _GEN_KEY = gen_key
        pairs = _expected_pairs(snap)
        keys = {p.get("id"): p.get("api_key") for p in pro}
        prev = _PREV_WRITTEN
        # 缩减代：删 provider 块 / 关代理 / 删模型 / 改任一 api_key（核验失败可绕限频强制 kickstart）
        shrink = bool(prev) and (bool(prev["pairs"] - pairs)
                                 or any(keys.get(pid) != k for pid, k in prev["keys"].items()))
        _PREV_WRITTEN = {"pairs": pairs, "keys": keys}
        if pro:
            _write_state(port=proxy_port(), binary_sha256=_bin_sha(proxy_bin()))   # 复用 _check_binary 的摘要
        return "written", {"gen": gen, "key": gen_key, "pairs": pairs, "count": len(pro), "shrink": shrink,
                           "seams": _cur()}


def _fail(n: int, msg: str, gen: int | None = None) -> dict:
    global _LAST_ERROR, _LAST_ERROR_GEN, _LAST_SYNC_AT
    _LAST_ERROR, _LAST_ERROR_GEN, _LAST_SYNC_AT = msg, gen, int(time.time())
    return {"ok": True, "reloaded": False, "last_error": msg, "providers_count": n, "models_count": 0}


def _publish(gen: int, result: dict) -> None:
    global _RESULT
    with _STATE:
        if _RESULT is None or gen >= _RESULT[0]:
            _RESULT = (gen, result)
        _STATE.notify_all()


def _submit(job: dict) -> None:
    global _JOB, _WORKER, _LAST_SEAMS
    with _STATE:
        _JOB, _LAST_SEAMS = job, job.get("seams") or _LAST_SEAMS
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_worker_main, name="proxy-verify", daemon=True)
            _WORKER.start()
        _STATE.notify_all()


def _poll(job: dict, timeout: float):
    """0.2 s 轮询判据；有写请求产生的更新代 → None（作废：不重写、不 kickstart、不改 last_error）。"""
    global _LOADED
    port, end = proxy_port(), time.monotonic() + timeout
    while True:
        if _loaded_once(port, job["pairs"], job["key"]):
            _LOADED = {"key": job["key"], "pairs": set(job["pairs"])}
            return True
        with _STATE:
            # 作废判据用**对象身份**：_JOB 一旦不是本 worker 正在处理的这个 job（请求线程或
            # restart 提交了新代），本代即作废。不能用 by_worker 这个粗判据——restart 提交的
            # 新代也带 by_worker=True，会被误当自己人而不作废。
            if _JOB is not None and _JOB is not job:
                return None
        if time.monotonic() >= end:
            return False
        time.sleep(0.2)


_KICK_LOCK = threading.Lock()


def _kick_with_window(min_gap: float = 0.0) -> tuple[dict | None, bool]:
    """串行化 restart() 与 worker 的 kick+窗口（review #6）。返回 (窗口结束落盘的新代或 None, 是否实际执行)。
    等锁期间别人已在 ``min_gap`` 秒内 kick 过则不重复执行（限频计数以实际执行为准）。"""
    with _KICK_LOCK:
        if time.monotonic() - _LAST_KICK < min_gap:
            return None, False
        return _kick_with_window_locked(), True


def _kick_with_window_locked() -> dict | None:
    """kickstart + 启动窗口（R2）。窗口内写入只分配代号；结束时用最新清单落盘一次，返回新代（无则 None）。"""
    global _WINDOW, _WIN_GEN, _LAST_KICK
    with _CONFIG_LOCK:
        _WINDOW = True
    try:
        _LAST_KICK = time.monotonic()
        _kickstart()
        port, t_kick, t0 = proxy_port(), time.monotonic(), None
        while time.monotonic() - t_kick < 5.0:
            if _tcp_reachable(port, 0.2):
                t0 = time.monotonic()
                break
            time.sleep(0.05)
        if t0 is not None:
            time.sleep(max(0.0, t0 + 0.5 - time.monotonic()))
    finally:
        with _CONFIG_LOCK:
            _WINDOW, wg, _WIN_GEN = False, _WIN_GEN, None
    if wg is None:
        return None
    kind, info = _write_stage(None, gen=wg)
    return dict(info, by_worker=True) if kind == "written" else None


def _run_job(job: dict, allow_kick: bool = True) -> dict:
    """核验一代；失败先重写一代再核验，仍失败才 kickstart（限频 60 s，缩减代可强制一次）。返回最后用到的 job。

    ``allow_kick=False``（worker 到点补跑，E-16）：只再写一代再核验，**绝不** kickstart——
    补跑是核验失败后的收敛动作，重启实例只由正常代（显式请求/新写）触发。
    """
    global _JOB, _VERIFIED_GEN, _VERIFIED_E, _RETRY_N, _RETRY_AT, _LAST_ERROR, _LAST_SYNC_AT
    t, j, origin = _verify_timeout(), job, job
    prev_j = job
    for step in ("verify", "rewrite", "kick"):
        ok = _poll(j, t)
        if ok is None:
            return j
        if ok:
            if j["gen"] >= _VERIFIED_GEN:
                _VERIFIED_GEN, _VERIFIED_E = j["gen"], {m for _, m in j["pairs"]}   # 该代的 E（裸名）
            _LAST_ERROR, _LAST_SYNC_AT, _RETRY_N, _RETRY_AT = None, int(time.time()), 0, None
            _publish(j["gen"], {"ok": True, "reloaded": True, "providers_count": j["count"],
                                "models_count": len({x for p, m in j["pairs"] for x in (m, p + "/" + m)})})
            return j
        if step == "verify":
            kind, info = _write_stage(None)
            if kind == "noop":   # 跳过重建（勘误 8 运行目录支 / 纯 no-op）：保留其 last_error，不判失败、不排补跑
                return j
            if kind != "written":
                break
            j = dict(info, by_worker=True, shrink=info["shrink"] or origin.get("shrink"))
        elif step == "rewrite":
            if not allow_kick:
                break   # 补跑阶段不 kickstart（E-16）：走结尾的统一记账（失败 + 补跑计数）
            shrink = bool(j.get("shrink"))
            if not launchctl_enabled() or not _plist_exists():
                msg = ERR_SHRINK_RESTART if shrink else (ERR_NOT_INSTALLED if launchctl_enabled() else ERR_RELOAD_VERIFY_FAIL)
                _publish(j["gen"], _fail(j["count"], msg, j["gen"]))
                return j   # 不 kickstart 的情形只记失败、不补跑
            forced = shrink and origin["gen"] not in _SHRINK_FORCED
            if not forced and time.monotonic() - _LAST_KICK < _KICK_INTERVAL:
                break
            if forced:
                _SHRINK_FORCED.add(origin["gen"])
            nj, _ = _kick_with_window(0.0 if forced else _KICK_INTERVAL)
            j = nj or j
        with _STATE:   # 请求线程 / restart 在重写期间提交的新代不得被覆盖（下一轮 _poll 会作废本代）
            # 守卫只认「本 worker 自己上一轮写下的那个 job」的对象身份：_JOB is None（无任务）或
            # _JOB is prev_j（worker 自身迭代）。不能用 by_worker——restart 提交的 _submit(dict(job,
            # by_worker=True)) 也带该标记，会让 worker 覆盖它、restart 空等到 202。
            if _JOB is None or _JOB is prev_j:
                _JOB = j
        prev_j = j
    _publish(j["gen"], _fail(j["count"], ERR_RELOAD_VERIFY_FAIL, j["gen"]))
    _RETRY_N += 1
    if _RETRY_N >= _RETRY_MAX:
        _RETRY_AT = None   # 补跑达到上限（E-16）：停止补跑，只保留 last_error，等下一次正常写
    else:
        _RETRY_AT = time.monotonic() + _BACKOFF[min(_RETRY_N - 1, len(_BACKOFF) - 1)]
    return j


def _worker_main() -> None:
    _TLS.worker = True   # 此后本线程任何环境变量读取都会抛错：只认任务快照
    while True:
        _TLS.seams = None
        try:
            _worker_step()
        except Exception:   # worker 不得死；失败只体现为未核验
            pass


def _worker_step() -> None:
    """一轮：取任务（或到点补跑），在任务快照内执行；补跑沿用最近一次提交的快照。"""
    global _JOB, _RETRY_AT
    with _STATE:
        while _JOB is None and (_RETRY_AT is None or time.monotonic() < _RETRY_AT):
            _STATE.wait(0.2)
        job = _JOB
    _TLS.seams = (job or {}).get("seams") or _LAST_SEAMS
    retry = job is None
    if retry:   # 到点补跑：用最新清单重写一代再核验（有上限、不 kickstart，E-16）
        with _STATE:
            _RETRY_AT = None
        if _RETRY_N >= _RETRY_MAX:
            return   # 补跑已达上限：停止补跑，只保留 last_error
        kind, info = _write_stage(None)
        if kind != "written":
            return
        job = dict(info, by_worker=True)
        with _STATE:
            _JOB = job
    try:
        last = _run_job(job, allow_kick=not retry)
    except Exception:   # worker 不得死；失败只体现为未核验
        last = job
    with _STATE:
        if _JOB is last or _JOB is job:
            _JOB = None


def boot_verify(ensure_key=None) -> "threading.Thread | None":
    """启动补核验（R2/R9，9.5）：运行目录已有 config.yaml 才做；接缝快照在启动时解析一次。
    端口不通 → 立即写一代再核验；已通 → 以探测到连通的时刻为 t0，等到 t0+0.5 s 再写一代并核验
    （避开实测丢失区间 [t0-0.015, t0+0.100] s）。代号取 max(上一代+1, 当前毫秒)，故重启后严格更大。

    ``ensure_key``：可选回调，在写第一代之前调用（与 ``restart`` 同一约定）。面板传
    ``provider_config.ensure_instance_key``——**全新安装**时运行目录已有安装脚本写的占位 config.yaml，
    而清单里还没有 ``proxy.instance_key``，不先备好 key 的话替换前自检必然失败（项名 api-keys）、
    整代作废、启动补核验等于没做（2026-09-29 真机验证发现，勘误 11）。"""
    s = resolve_seams()
    if s.port <= 0 or not os.path.exists(os.path.join(s.run_dir, "config.yaml")):
        return None
    if ensure_key is not None:
        # **必须在调用方线程里做**：核验线程禁止读环境变量（事故③的防线，_TLS.worker=True 时
        # provider_proxy._env 直接抛错），而 ensure_key 走 provider_config 的路径解析要读接缝。
        # 失败（清单损坏/被安全闸拒写）不得让面板起不来，只留痕并放弃本次补核验。
        try:
            ensure_key()
        except Exception as e:
            import sys as _sys
            print("[proxy-boot-verify] 确保 instance_key 失败，跳过本次补核验：%s" % e, file=_sys.stderr)
            return None

    def run() -> None:
        _TLS.worker = True   # 同核验 worker：只认启动快照，不读环境变量
        with _using(s):
            if _tcp_reachable(s.port, 0.2):
                t0 = time.monotonic()
                time.sleep(max(0.0, t0 + 0.5 - time.monotonic()))
            kind, info = _write_stage(None)
            if kind == "written":
                _submit(dict(info, by_worker=True))

    t = threading.Thread(target=run, name="proxy-boot-verify", daemon=True)
    t.start()
    return t


def sync(snapshot: dict | None = None, explicit: bool = False) -> dict:
    """入口：先把接缝解析成快照（已在快照内则沿用），写入段与交给 worker 的任务只用快照。"""
    with _using(_cur() or resolve_seams()):
        return _sync(snapshot, explicit)


def _sync(snapshot: dict | None = None, explicit: bool = False) -> dict:
    """写入段同步完成；核验交 worker。写路径（explicit=False）立即返回；显式 sync 在请求线程内
    等结果（不持任何锁），截止 D = 3T+11.2 s，到点回核验失败（202），核验在后台继续。"""
    kind, info = _write_stage(snapshot)
    if kind in ("noop", "error"):
        return info
    port = proxy_port()
    if port <= 0 or (kind == "written" and not info["pairs"] and not _tcp_reachable(port)):
        if not info.get("pairs") and not info["count"]:
            global _LAST_ERROR
            _LAST_ERROR = None
            return dict(_IDLE)   # 空清单重写且不可达：不核验、不 kickstart（9.4）
        return _fail(info["count"], ERR_RELOAD_VERIFY_FAIL, info["gen"])   # 端口 ≤0：请求内立即判失败
    if kind == "written":
        _submit(info)
    else:
        pass   # deferred：窗口结束由 worker 落盘
    if not explicit:
        return {"ok": True, "reloaded": False, "pending": True, "providers_count": info["count"], "models_count": 0}
    deadline = time.monotonic() + 3 * _verify_timeout() + 11.2
    with _STATE:
        while not (_RESULT and _RESULT[0] >= info["gen"]):
            left = deadline - time.monotonic()
            if left <= 0:
                return {"ok": True, "reloaded": False, "last_error": ERR_RELOAD_VERIFY_FAIL,
                        "providers_count": info["count"], "models_count": 0}
            _STATE.wait(min(left, 0.2))
        return dict(_RESULT[1])


_BIN_SHA: tuple = (None, None)   # ((path, mtime_ns, size), sha256)：二进制不变就不重算（review #8）


def _bin_sha(path: str) -> str | None:
    global _BIN_SHA
    try:
        st = os.stat(path)
    except OSError:
        return None
    sig = (path, st.st_mtime_ns, st.st_size)
    if _BIN_SHA[0] != sig:
        _BIN_SHA = (sig, _sha_of(path))
    return _BIN_SHA[1]


def _sha_of(path: str) -> str | None:
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


ERR_RESTART_OFF = "代理未启用"
ERR_RESTART_RATE = "重启过于频繁，请稍后重试"
ERR_KICK_FAIL = "kickstart 执行失败"


def restart(ensure_key=None) -> tuple[int, dict]:
    """``POST /api/proxy/restart``（9.15，D9=A）：写一代 → kickstart（含启动窗口）→ 核验，截止 T+11.2 s。
    判定顺序：安全闸 500 → 代理未启用 409 → 实例未安装 409 → 限频 429；前四项零写入、不计限频、不分配代号。
    LAUNCHCTL=off 或端口 ≤0 跳过 plist 判定与 kickstart（端口 ≤0 即 202）。返回 (状态码, 响应体)。"""
    t_arrive = time.monotonic()
    with _using(_cur() or resolve_seams()):
        if _gate_hit():
            return 500, {"ok": False, "error": ERR_SEAMS}
        snap = _load_snapshot()
        if not proxy_providers(snap) and not os.path.exists(config_path()):
            return 409, {"ok": False, "error": ERR_RESTART_OFF}
        kick = launchctl_enabled() and proxy_port() > 0
        if kick and not _plist_exists():
            return 409, {"ok": False, "error": ERR_NOT_INSTALLED}
        left = _KICK_INTERVAL - (time.monotonic() - _LAST_KICK)
        if kick and left > 0:
            return 429, {"ok": False, "error": ERR_RESTART_RATE, "retry_after": max(1, int(left + 0.999))}
        if ensure_key is not None:   # 与显式 sync 一致：写一代前确保清单有 instance_key（前四项判定之后，保零写入）
            ensure_key()
        kind, info = _write_stage(None)
        if kind == "error":
            return 500, {"ok": False, "error": info["error"]}
        fail = {"ok": False, "reloaded": False, "error": ERR_RELOAD_VERIFY_FAIL}
        if kind != "written" or proxy_port() <= 0:
            _fail(info.get("count", 0), ERR_RELOAD_VERIFY_FAIL, info.get("gen"))
            return 202, fail
        job = info
        if kick:
            nj, ran = _kick_with_window(_KICK_INTERVAL)   # 等锁期间 worker 刚 kick 过则沿用，不重复
            job = nj or info
            if ran and not _KICK_OK:
                _fail(info["count"], ERR_KICK_FAIL, info["gen"])
                return 500, {"ok": False, "error": ERR_KICK_FAIL}
        _submit(dict(job, by_worker=True))
        deadline = t_arrive + _verify_timeout() + 11.2
        with _STATE:
            while not (_RESULT and _RESULT[0] >= job["gen"]):
                if time.monotonic() >= deadline:
                    return 202, fail
                _STATE.wait(0.2)
            ok = _RESULT[0] == job["gen"] and _RESULT[1].get("reloaded") is True
        return (200, {"ok": True, "reloaded": True, "generation": job["gen"]}) if ok else (202, fail)


def generation_info() -> dict:
    """``GET /api/proxy/generation``（9.15）：恒恰六键。suspended 实时读实例（instance_key、超时 1 s、
    缓存 2 s）：verified 那代 E 中实例 /v1/models 缺失的裸名；verified=0 / 读失败 / 非 200 / data 空 → []。"""
    global _SUSP_CACHE
    verified, e = _VERIFIED_GEN, set(_VERIFIED_E)
    t, val = _SUSP_CACHE
    if val is None or time.monotonic() - t >= 2.0:
        got = _fetch_alias_set(proxy_port(), _instance_key_from_manifest(), 1.0) \
            if verified and e and proxy_port() > 0 else None
        val = sorted(e - got) if got else []
        _SUSP_CACHE = (time.monotonic(), val)
    return {"ok": True, "generation": _GEN_LAST, "verified_generation": verified,
            "last_error": _LAST_ERROR, "last_error_generation": _LAST_ERROR_GEN or 0,
            "suspended": val if verified else []}


def proxy_status(snapshot: dict | None = None) -> dict:
    """``GET /api/proxy`` 的形状（INTERFACE §2.2）。**不含任何明文 key**。

    ``running`` 探测 ``127.0.0.1:<port>`` 连通性；``port``/``base_url`` 反映**当前配置端口**。
    """
    snap = snapshot if snapshot is not None else _load_snapshot()
    pro = proxy_providers(snap)
    key = _instance_key_from_manifest()
    port = proxy_port()

    running = False
    if port > 0:
        try:
            with socket.create_connection((_HOST, port), timeout=1.0):
                running = True
        except OSError:
            running = False

    aliases = _fetch_alias_set(port, key) if running else None

    path = proxy_bin()
    exists = os.path.isfile(path)
    hash_ok = False
    if exists:
        stored = _read_state()
        digest = _sha_of(path)
        hash_ok = (not stored.get("binary_sha256")) or stored["binary_sha256"] == digest

    return {
        "ok": True,
        "running": running,
        "port": port,
        "base_url": base_url(),
        "key_masked": _mask(key),
        "has_key": bool(key),
        "binary": {"path": path, "exists": exists, "hash_ok": hash_ok},
        "config_path": config_path(),
        "providers_count": len(pro),
        "models_count": len(aliases) if aliases is not None else 0,
        "last_error": _LAST_ERROR,
        "last_sync_at": _LAST_SYNC_AT,
    }


def _mask(value: str) -> str:
    """``****`` + 末 4 位（与 provider_config._mask 同款；HTTP 出口绝不返回明文）。"""
    if not value:
        return ""
    return ("****" + value[-4:]) if len(value) >= 4 else "****"


# ── 自测（python3 provider_proxy.py）─────────────────────────────────────

def _selftest() -> None:
    os.environ.setdefault("CLAUDEBOT_PROXY_PORT", "0")
    os.environ.setdefault("CLAUDEBOT_PROXY_DIR", "/tmp/_pp_selftest")
    snap = {
        "proxy": {"instance_key": "sk-inst", "port": 0},
        "providers": [
            {"id": "p_a", "base_url": "https://a.example/anthropic", "api_key": "k1", "proxy": True,
             "protocol": "anthropic",
             "models": [{"id": "c1", "label": "c1"}, {"id": "c2", "label": "c2"}]},
            {"id": "p_b", "base_url": "https://b.example/v1", "api_key": "k2", "proxy": True,
             "protocol": "openai", "models": [{"id": "o1", "label": "o1"}]},
            {"id": "p_c", "base_url": "https://c.example/v1", "api_key": "k3", "proxy": False,
             "protocol": "anthropic", "models": [{"id": "z", "label": "z"}]},
        ],
    }
    cfg = yaml.safe_load(build_config(snap))
    assert cfg["host"] == "127.0.0.1"
    assert cfg["port"] == 0
    assert cfg["api-keys"] == ["sk-inst"]
    assert cfg["remote-management"] == {"allow-remote": False, "disable-control-panel": True}
    a = cfg["claude-api-key"]
    assert len(a) == 1 and a[0]["prefix"] == "p_a"
    assert a[0]["base-url"] == "https://a.example/anthropic"
    assert a[0]["models"] == [{"name": "c1", "alias": "c1"}, {"name": "c2", "alias": "c2"}]
    o = cfg["openai-compatibility"]
    assert len(o) == 1 and o[0]["prefix"] == "p_b"
    assert o[0]["base-url"] == "https://b.example"
    assert o[0]["api-key-entries"] == [{"api-key": "k2"}]
    assert "p_c" not in str(cfg), "proxy=false 的 provider 不得进配置"

    # 旧 protocols 表不参与判定：无显式协议时按 base_url 兜底为 openai
    cfg2 = yaml.safe_load(build_config({"providers": [
        {"id": "p_d", "base_url": "https://d.example", "api_key": "k", "proxy": True,
         "models": [{"id": "u", "label": "u"}], "protocols": {"u": "unknown"}}]}))
    assert "claude-api-key" not in cfg2
    assert cfg2["openai-compatibility"][0]["models"] == [{"name": "u", "alias": "u"}]

    # YAML 注入：模型名/字段含换行与控制字符时不得追加出新块
    cfg3 = yaml.safe_load(build_config({"proxy": {"instance_key": "k"}, "providers": [
        {"id": "p_x", "base_url": "https://x.example", "api_key": "a\nb", "proxy": True,
         "protocol": "anthropic",
         "models": [{"id": "m\nclaude-api-key: []", "label": "m"}]}]}))
    injected = cfg3["claude-api-key"][0]["models"][0]
    assert injected == {"name": "m\nclaude-api-key: []", "alias": "m\nclaude-api-key: []"}
    # 注入串只存活在字段值里，键集合未被改变 ⇒ 未追加新块
    assert set(cfg3) == {"host", "port", "api-keys", "remote-management", "claude-api-key"}

    print("provider_proxy selftest OK")


if __name__ == "__main__":
    if "--sync" in sys.argv[1:]:
        # 供 scripts/run_provider_proxy.sh 在 exec 外部 cli-proxy-api 之前生成 config.yaml。
        # 非显式 sync：写入段完成即返回，核验交后台 worker，不阻塞服务启动。
        sync()
    else:
        _selftest()
