# -*- coding: utf-8 -*-
"""cliproxy 管理 API 客户端（INTERFACE §0.3/§0.4/§3.0d/§3.7；PLAN M2 · 切缝 (b)）。

门户与 cliproxy 之间**唯一**的 HTTP 出口 —— 路由层不许自己发请求，
这样「错误分型」与「脱敏投影」各自只剩一处要看对。

错误分型（§0.4，写死）：

| 故障 | 判定 | 抛什么 |
|---|---|---|
| 没配 | §0.3 三个键任一缺失 | ``503 cliproxy_unconfigured`` |
| 没跑 | 传输层压根没拿到 HTTP 响应（拒连/超时/DNS） | ``503 cliproxy_down`` |
| 跑着但拒绝我们 | 拿到响应、管理 API 非 2xx（4xx 5xx 一视同仁） | ``502 cliproxy_reject`` |

**异常消息绝不携带上游响应体，也绝不携带管理密钥**：traceback 会进日志（M0 交接）。
``HubError`` 的 args 只有 ``"<状态码> <错误码>"``，``detail`` 只带上游状态码。
本模块一行日志都不打 —— 没有输出，就没有可漏的地方。

**代理必须显式关掉**：门户只跟 127.0.0.1 说话，而 urllib 默认吃 ``http_proxy``
环境变量，机器上挂着 Clash 之类的透明代理时本机请求会被送出去，误判成 cliproxy_down。
"""
import http.client
import json
import os
import socket
import time
import urllib.error
import urllib.request
from collections import namedtuple

from moments import env_file, provider_model, redact
from moments.provider_model import HubError, KIND_SPEC

MGMT_PREFIX = "/v0/management/"
ENV_KEYS = ("CLIPROXY_PORT", "CLIPROXY_MGMT_KEY", "CLIPROXY_API_KEY")
COMPAT_KEY = "CLIPROXY_ANTHROPIC_COMPAT"
HEALTH_TIMEOUT = 3.0        # §0.4：3 秒内拿不到 HTTP 响应 = 没跑
MGMT_TIMEOUT = 10.0
MESSAGES_TIMEOUT = 30.0     # §3.7

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class CliproxyError(HubError):
    """带上游状态码的 HubError（§3.2 的 ``cliproxy.error_status`` 要用它）。

    路由层照旧 ``except HubError`` 统一处理；要区分四态时读 ``upstream_status``。
    ``timed_out`` 只给 §3.7 用：自测的超时是业务结果（``stage:"timeout"``），
    不是"cliproxy 没跑"。
    """

    def __init__(self, status, error, detail="", upstream_status=None, timed_out=False):
        super().__init__(status, error, detail)
        self.upstream_status = upstream_status
        self.timed_out = timed_out


def _unconfigured():
    return CliproxyError(503, "cliproxy_unconfigured",
                         "cliproxy 还没配好（先跑一次 install.sh）")


def _down(timed_out=False):
    return CliproxyError(503, "cliproxy_down",
                         "连不上本机 cliproxy（装了但没在跑？）", timed_out=timed_out)


def _reject(status, why="management API 返回 %d"):
    # detail 只有状态码：既满足 §0.4「必须含上游状态码」，又不碰上游响应体（§0.2）
    return CliproxyError(502, "cliproxy_reject", why % status, upstream_status=status)


#: §0.3 的回落读法搬去 ``moments/env_file.py`` 与 ``hub_auth`` 共用（BUG-21：
#: 口令那一侧漏了回落 = 出货机器上鉴权门恒关）。这两个别名只为不改既有调用点。
_env_file_path = env_file.hub_env_path
_parse_env_file = env_file.parse


def _port(raw):
    """端口解析不出整数或越界 → None（= 没配）。带着 0 端口去连只会得到更难懂的 down。"""
    try:
        p = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return p if 0 < p < 65536 else None


def load_config(env=None):
    """§0.3：唯一来源是进程环境变量，**逐键**回落 ``hub.env``。本函数不抛错。

    列表端点恒 200（§3.2），所以"没配"必须是个可读的返回值而不是异常；
    要异常的路径走 :func:`from_env`。
    """
    env = os.environ if env is None else env
    src = {}
    if any(not env.get(k) for k in ENV_KEYS + (COMPAT_KEY,)):
        src = _parse_env_file(_env_file_path(env))     # 全配齐时零文件 IO

    def get(k):
        return (env.get(k) or src.get(k) or "").strip()

    port, mgmt_key, api_key = _port(get("CLIPROXY_PORT")), get(ENV_KEYS[1]), get(ENV_KEYS[2])
    return {
        "port": port, "mgmt_key": mgmt_key, "api_key": api_key,
        # 缺失取 1：§12.2 ⑩ 已删掉运行时探测，route 恒 cliproxy（provider_model.ROUTE），
        # 这里报 false 会与 route 自相矛盾。要退回 direct 得手动写 0（回归开关，§3.0b）。
        "anthropic_compat": get(COMPAT_KEY) != "0",
        "configured": bool(port and mgmt_key and api_key),
    }


def from_env(env=None):
    """按 §0.3 造一个客户端；三键任一缺失 → ``503 cliproxy_unconfigured``。"""
    cfg = load_config(env)
    if not cfg["configured"]:
        raise _unconfigured()
    return CliproxyClient(cfg["port"], cfg["mgmt_key"], cfg["api_key"],
                          cfg["anthropic_compat"])


#: 承载块里的一条 entry。``raw`` 含上游明文 key，**只许喂给写路径**
#: （PATCH 保原 key、按下标改 prefix —— 管理 API 的按下标写是整条替换，
#: 不带 key 写回去等于把用户的 key 删了）。要进响应体/日志的一律用 ``view``。
Entry = namedtuple("Entry", "kind index raw view")


def _to_view(kind, raw):
    """entry → §3.1 provider 对象。**§0.2 第一层就在这一行**：先白名单投影，
    明文 key 在进 ``from_block_entry`` 之前就被丢掉；``key_masked`` 单独掩出来。
    """
    view = provider_model.from_block_entry(kind, redact.project(raw))
    view["key_masked"] = provider_model.mask_key(provider_model.entry_api_key(kind, raw))
    return view


def _prefix(raw):
    """§3.0d：只认字符串 prefix；手写 config.yaml 里的非字符串一律当没设。"""
    v = raw.get("prefix")
    return v if isinstance(v, str) else ""


def _aliases(view):
    return {a for a in (view.get("model_alias"), view.get("haiku_alias")) if a}


def unwrap_list(payload, *keys):
    """管理 API 读侧的形状适配：**真 v7 回 ``{"<块名>": [...]}``，§10 的桩回裸数组**。

    M2 真机实测（7.2.148）：``GET /v0/management/claude-api-key`` →
    ``{"claude-api-key":[…]}``、``api-keys`` → ``{"api-keys":[…]}``、
    ``auth-files`` → ``{"files":[]}``（**键名不等于路径段**，所以允许多给几个候选键）。
    两种都认，形状再变也只坏这一个函数。认不出来一律回空列表 ——
    读侧宁可"看不见条目"，也不能把 dict 当成条目往下传。
    M5 读 auth-files 请用 ``unwrap_list(resp, "files", "auth-files")``。
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in keys:
            if isinstance(payload.get(k), list):
                return payload[k]
        lists = [v for v in payload.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
    return []


def _short(text, limit):
    """出站兜底（§0.2 第二层）+ 截断。非字符串一律回 None，别把结构体带出去。"""
    if not isinstance(text, str):
        return None
    return redact.scrub_text(text)[:limit]


class CliproxyClient:
    """一次请求造一个，不做连接池、不缓存 —— 门户是单用户低频场景。

    # ponytail: 读-改-写之间没有乐观锁，两个浏览器同时改会后写覆盖先写。
    # 单用户管理台够用；真要并发就给管理 API 加 If-Match 之类的版本号。
    """

    def __init__(self, port, mgmt_key, api_key, anthropic_compat=True):
        self.port = int(port)
        self.base = "http://127.0.0.1:%d" % self.port
        self.api_key = api_key          # 下游 api-key：写 settings.json 与 /v1/messages 用
        self.anthropic_compat = bool(anthropic_compat)
        self._mgmt_key = mgmt_key       # 只进请求头，永不进日志/响应/异常

    # ---------- 一次 HTTP 往返 ----------
    def _request(self, method, path, data=None, headers=None, timeout=MGMT_TIMEOUT):
        """返回 ``(status, body_bytes)``。**只有传输层失败才抛 cliproxy_down**；
        HTTP 状态码原样回给调用方（§0.4：拿到响应就不算"没跑"）。
        """
        raw = None if data is None else json.dumps(data).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=raw, method=method)
        req.add_header("Accept", "application/json")
        if raw is not None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with _OPENER.open(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:      # 4xx/5xx：响应拿到了
            try:
                return e.code, e.read()
            finally:
                e.close()
        except (OSError, http.client.HTTPException) as e:
            # 拒连 / 超时 / DNS / 半个响应 —— 传输层根本没拿到 HTTP 响应（§0.4）
            reason = getattr(e, "reason", e)
            raise _down(timed_out=isinstance(reason, (TimeoutError, socket.timeout)))

    # ---------- 管理 API ----------
    def _mgmt(self, method, path, data=None):
        """管理 API 的统一出口：非 2xx → 502 cliproxy_reject，正文只解析、绝不外带。"""
        if not path.startswith(MGMT_PREFIX):
            path = MGMT_PREFIX + path.lstrip("/")
        status, body = self._request(method, path, data,
                                     {"X-Management-Key": self._mgmt_key}, MGMT_TIMEOUT)
        if not (200 <= status < 300):
            raise _reject(status)
        if not body:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            # 2xx 却不是 JSON：回话了但不是我们能懂的 → 归"拒绝我们"，正文不带出去
            raise _reject(status, "management API 返回 %d 但正文不是 JSON")

    def mgmt_get(self, path):
        return self._mgmt("GET", path)

    def mgmt_put(self, path, data):
        return self._mgmt("PUT", path, data)

    def mgmt_patch(self, path, data):
        return self._mgmt("PATCH", path, data)

    def mgmt_post(self, path, data=None):
        return self._mgmt("POST", path, data)

    def mgmt_delete(self, path, data=None):
        return self._mgmt("DELETE", path, data)

    def healthz(self):
        """§0.4 探活：3 秒内拿不到 HTTP 响应就抛 ``cliproxy_down``。

        拿到了就算跑着（哪怕是 404/500）—— 传输层与应用层分家是 §0.4 的分界线。
        返回解析后的正文（拿不到 JSON 就是 None），M3 可从中取版本号之类。
        """
        _status, body = self._request("GET", "/healthz", timeout=HEALTH_TIMEOUT)
        try:
            return json.loads(body.decode("utf-8")) if body else None
        except (ValueError, UnicodeDecodeError):
            return None

    # ---------- provider 条目（§3.0 KIND_SPEC 派生，块名只在那一张表里）----------
    def entries(self, kinds=None):
        """把三个承载块读成一串 :class:`Entry`。非 dict 的条目跳过但**不影响下标**
        （手写 config.yaml 里混进标量时，按下标写还得打得准）。
        """
        out = []
        for kind in (kinds or tuple(KIND_SPEC)):
            for i, raw in enumerate(self._block(kind)):
                if isinstance(raw, dict):
                    out.append(Entry(kind, i, raw, _to_view(kind, raw)))
        return out

    def providers(self):
        """§3.2 的 ``providers[]``：只含安全字段，无明文 key。"""
        return [e.view for e in self.entries()]

    def find(self, provider_id, entries=None):
        """按派生 id 找条目，找不到回 None（404 由路由层判）。"""
        for e in (self.entries() if entries is None else entries):
            if e.view["id"] == provider_id:
                return e
        return None

    def _block(self, kind):
        """读一个承载块的条目数组（真机是包装对象，桩是裸数组，见 :func:`unwrap_list`）。"""
        name = KIND_SPEC[kind]["block"]
        return unwrap_list(self.mgmt_get(name), name)

    def add_entry(self, kind, entry):
        """新增一条：整块 PUT 回去（v7 的 PUT 收裸数组、整表替换，[CPX] Q3）。**恰好一次写**。"""
        items = list(self._block(kind))
        items.append(entry)
        self.mgmt_put(KIND_SPEC[kind]["block"], items)

    def replace_entry(self, kind, index, entry):
        """按下标整条替换：``PATCH {"index":n,"value":{...}}``，不动同块其它条目。"""
        self.mgmt_patch(KIND_SPEC[kind]["block"], {"index": index, "value": entry})

    def delete_entry(self, kind, index):
        """删一条：管理 API 没有"删第 n 条"，只能把其余条目整块 PUT 回去。"""
        items = list(self._block(kind))
        if 0 <= index < len(items):
            items.pop(index)
        self.mgmt_put(KIND_SPEC[kind]["block"], items)

    # ---------- §3.0d alias 互斥（PLAN 切缝 (b)：机制归 M2，编排归 M3）----------
    def set_alias_exclusive(self, alias, target_id):
        """让 ``alias`` 在运行时只命中 ``target_id`` 那一条（§3.6 B 阶段）。

        手段是 per-entry ``prefix``，三块统一：**落选**条目 ``prefix`` = 它自己的 id，
        **当选**条目清空 ``prefix``；配骨架里的全局 ``force-model-prefix: true``，
        裸官方名就只会命中没有前缀的那一条。
        **禁止写 cliproxy 的 ``disabled`` 键** —— 它只在 ``openai-compatibility`` 有，
        在另两块上被静默忽略（互斥会假装成功），而且那是用户手改的地盘，只读不写。

        写的顺序固定「先停落选、再启当选」，且**只写有变化的条目**——
        §10 的 ``patch_fail_after_n`` 时序表按这个数（B1 一次、B2 一次）。
        返回可撤销记录 ``[{"kind","index","prefix"(旧值)}, ...]``；中途失败时
        异常上挂 ``.undo``（同结构，已成功的那部分），交给 M3 走 §3.6 的补偿。
        主 alias 与 haiku alias **各调一次**（§3.0c，少跑一次 = 后台摘要 502）。
        """
        entries = self.entries()
        if not any(e.view["id"] == target_id for e in entries):
            raise HubError(404, "not_found", "provider 不存在")
        touched = [(e, "" if e.view["id"] == target_id else e.view["id"])
                   for e in entries if alias in _aliases(e.view)]
        touched = [(e, want) for e, want in touched if _prefix(e.raw) != want]
        touched.sort(key=lambda t: t[1] == "")      # False(落选) 在前，稳定排序
        done = []
        for e, want in touched:
            old = _prefix(e.raw)
            try:
                self._write_prefix(e, want)
            except HubError as exc:
                exc.undo = done                     # 已经改过谁，调用方据此补偿
                raise
            done.append({"kind": e.kind, "index": e.index, "prefix": old})
        return done

    def restore_prefixes(self, records):
        """§3.6 的补偿：把 :meth:`set_alias_exclusive` 改过的 prefix 原样写回。

        倒序还原（后改的先撤），条目已经不在了就跳过。抛错即"补偿本身失败"，
        M3 据此回 ``500 activate_partial``。
        """
        for r in reversed(list(records or [])):
            e = self._entry_at(r["kind"], r["index"])
            if e is not None:
                self._write_prefix(e, r["prefix"])

    def _entry_at(self, kind, index):
        items = self._block(kind)
        if 0 <= index < len(items) and isinstance(items[index], dict):
            return Entry(kind, index, items[index], _to_view(kind, items[index]))
        return None

    def _write_prefix(self, entry, value):
        """只改 ``prefix``，其余字段原样写回。

        ⚠️ 按下标写是**整条替换**（桩与真机 v7 都是），所以必须把整条 entry 送回去，
        含上游明文 key —— 不带它等于把用户的 key 删了。key 的值在这条路上不变、
        不进日志、不进响应，这是 §3.0d「api-key 不被读出重写」在管理 API 语义下
        能兑现的最接近形态（真机核验见 M2 报告 V1）。
        """
        self.replace_entry(entry.kind, entry.index, dict(entry.raw, prefix=value))

    # ---------- §3.7 连通性自测 ----------
    def probe_messages(self, model, timeout=MESSAGES_TIMEOUT):
        """真发一次最小 ``POST /v1/messages``，返回**已经安全**的结构化结果。

        上游 4xx 常把请求头（含 ``Authorization: Bearer <上游key>``）原样回显，
        所以只取 ``error.type`` / ``error.message`` 两个字段并过一遍兜底脱敏，
        原始响应体绝不出本函数（[R4D] I3-3）。

        | 情况 | 返回 |
        |---|---|
        | 上游 2xx | ``{"ok":True,"status":200,"latency_ms":…,"model_echo":…,"text_head":…}`` |
        | 上游 4xx/5xx | ``{"ok":False,"status":…,"error_type":…,"error_message":…}`` |
        | 超时 | ``{"ok":False,"timeout":True,…}``（§3.7 的 ``stage:"timeout"``，**不是** down） |
        | 连不上 cliproxy | 抛 ``503 cliproxy_down`` |
        """
        body = {"model": model, "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}]}
        t0 = time.monotonic()
        try:
            status, raw = self._request("POST", "/v1/messages", body,
                                        {"Authorization": "Bearer " + self.api_key}, timeout)
        except CliproxyError as e:
            if e.timed_out:                  # 超时是业务结果，不是"cliproxy 没跑"
                return {"ok": False, "status": None, "timeout": True,
                        "latency_ms": int((time.monotonic() - t0) * 1000)}
            raise
        out = {"ok": 200 <= status < 300, "status": status, "timeout": False,
               "latency_ms": int((time.monotonic() - t0) * 1000)}
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if out["ok"]:
            out["model_echo"] = _short(data.get("model"), 80)
            out["text_head"] = _short(_first_text(data), 40)
        else:
            err = data.get("error")
            err = err if isinstance(err, dict) else {}
            out["error_type"] = _short(err.get("type"), 80)
            out["error_message"] = _short(err.get("message"), 200)
        return out


def _first_text(data):
    """Anthropic 响应的首个文本块。形状不对就回 None —— 上游是谁写的都不奇怪。"""
    for blk in data.get("content") or []:
        if isinstance(blk, dict) and isinstance(blk.get("text"), str):
            return blk["text"]
    return None
