# -*- coding: utf-8 -*-
"""上游探测：协议判定探针 + 地址探针（PLAN §4.3 / §5.2；INTERFACE §7）。

三类探针：

    ①  ``/v1/models`` 的 ``supported_endpoints``  —— model 级，复用 R6 的 GET，零额外请求
    ②a 带模型探针 ``POST <anthropic_url>/v1/messages``（body 含 model）—— model 级，**可能计费**
    ②b 空体探针   ``POST <anthropic_url>/v1/messages`` 与 ``POST <openai_url>/chat/completions``
                  body ``{}`` —— **provider 级**（只答「路由在不在」，与 model 无关）
    地址探针      ``POST <openai_url>/chat/completions`` 空体 —— 只答「host 活不活」

**必须带浏览器式 UA**（BRIEF §6 + RESEARCH §1.3）：Cloudflare 对无浏览器指纹的请求回
``403 error code: 1010``；不带 UA 会把 WAF 拦截误判成「协议不支持」。

**探针中间产物不落任何文件、不进任何日志**（P1/E12）：请求与响应体只活在内存，
判定完即弃；本模块不含任何写日志/写文件代码。
"""
from __future__ import annotations

import errno
import http.client
import json
import socket
import threading
import urllib.error
import urllib.request

# 浏览器式 UA（硬编码，不进配置）；同时是 R6 拉模型与三类探针的**唯一来源**。
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_ANTHROPIC_VERSION = "2023-06-01"
_MAX_BODY = 1024 * 1024

# 探测默认超时（§4.5：单次 6s）
DEFAULT_TIMEOUT = 6

# ── ②a 类别 ───────────────────────────────────────────────────────────────
PROTO_OK = "proto_ok"                      # 200，真跑通
MODEL_UNSUPPORTED = "model_unsupported"    # 400/422，该模型不支持此端点 → 判 openai
PROTO_NOAUTH_OK = "proto_noauth_ok"        # 400/422，缺字段/格式类 → 判 anthropic（需 ②b 佐证）
MODEL_NOT_IN_PLAN = "model_not_in_plan"    # 403 套餐/权限类 → 判 anthropic 且记 unavailable
AUTH_OK = "auth_ok"                        # 401/403 凭据类 → unknown
ROUTE_MISSING = "route_missing"            # 404 或其它 4xx → 落逐模型 #5
NET = "net"                                # 5xx / 超时 / 连接层 → unknown

# ── ②b / 地址探针类别 ─────────────────────────────────────────────────────
SCHEMA_OK = "schema_ok"       # 400/422 缺字段类：路由存在且认该请求形状
AUTH = "auth"
UNKNOWN_CLASS = "unknown"

_MODEL_UNSUPPORTED_HINTS = (
    "/chat/completions", "not supported on this endpoint", "不支持此端点",
    "does not support", "unsupported model", "use /chat",
)
_SCHEMA_HINTS = (
    "required", "missing", "invalid type", "unexpected", "expected",
    "schema", "field", "格式", "缺", "参数", "too small", "minimum",
    "at least", "must be", "不能为空", "is required",
)
_PLAN_HINTS = (
    "model_not_in_plan", "not in plan", "insufficient", "quota", "套餐",
    "余额", "充值", "plan", "billing", "balance", "insufficient_quota",
)
_AUTH_HINTS = (
    "invalid api key", "invalid_api_key", "invalid key", "unauthorized",
    "authentication", "api key", "apikey", "token", "credential",
    "凭据", "鉴权", "认证", "未授权", "error code: 1010", "forbidden",
    "permission", "无权", "权限",
)


class TransportError(Exception):
    """传输层失败（**没拿到 HTTP 响应**）——**与「收到 HTTP 响应」严格区分**。

    地址探针的判据正是这条边界：收到任何 HTTP 响应（含 4xx/5xx）都算「host 可达」，
    只有**连接层**失败才算「地址不通过」。

    ``unreachable`` 再细分一层：契约 §3.2/§7.2 列举的「不通过」只有 **DNS 失败 / 连接被拒 /
    超时**——三者都是 **TCP 根本没连上**。而 TLS 握手被重置（``SSLEOFError``）、服务端收下
    连接却不回响应（``RemoteDisconnected``）都意味着 **TCP 已连上**（host 可达），只是这一跳
    失败；与「404 也算通过（host 可达、仅无该端点）」是同一原则，故**不算地址不通过**。

    协议探针（②a/②b）不看这个标志，一律按 :data:`NET` 处理（→ ``unknown``）。
    """

    def __init__(self, kind: str, exc_name: str, *, unreachable: bool = False):
        self.kind = kind          # "timeout" | "conn"
        self.exc_name = exc_name
        self.unreachable = unreachable   # True = TCP 层就没连上（DNS 失败/拒连/超时）
        super().__init__("%s(%s)" % (kind, exc_name))


# TCP 层就没连上的异常类型（DNS 解析失败 / 主动拒绝 / 超时）。
# **不含** ssl.SSLError / http.client.RemoteDisconnected / ConnectionResetError ——
# 那几类都以「TCP 三向握手已完成」为前提（见 TransportError.unreachable 的说明）。
_UNREACHABLE_EXC = (socket.gaierror, socket.herror, ConnectionRefusedError,
                    TimeoutError, socket.timeout)


def _is_unreachable(reason) -> bool:
    return isinstance(reason, _UNREACHABLE_EXC)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None  # 3xx 不跟随：作为状态码返回，由调用方决定


def browser_headers(extra: dict | None = None) -> dict:
    """浏览器式请求头基底（探针与 R6 拉模型共用）。"""
    h = {"User-Agent": BROWSER_UA, "Accept": "application/json"}
    if extra:
        h.update(extra)
    return h


def http_request(url: str, *, method: str = "GET", headers: dict | None = None,
                 body: bytes | None = None, timeout: int = DEFAULT_TIMEOUT):
    """发一次请求，返回 ``(status:int, body:bytes)``。

    - 3xx / 4xx / 5xx **都算「收到了 HTTP 响应」**，按状态码返回（不跟随重定向）。
    - 连接层失败抛 :class:`TransportError`。
    - 响应体 ≤ ``_MAX_BODY``；禁用环境代理（``ProxyHandler({})``），避免请求被
      ``http_proxy`` 静默劫持到第三方。
    """
    req = urllib.request.Request(url, data=body, method=method,
                                 headers=browser_headers(headers))
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(_MAX_BODY + 1)
    except urllib.error.HTTPError as e:
        try:
            raw = e.read(_MAX_BODY + 1)
        except Exception:
            raw = b""
        return e.code, raw
    except (socket.timeout, TimeoutError):
        raise TransportError("timeout", "TimeoutError", unreachable=True)
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", None)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise TransportError("timeout", "TimeoutError", unreachable=True)
        if isinstance(reason, BaseException):
            raise TransportError("conn", type(reason).__name__,
                                 unreachable=_is_unreachable(reason))
        raise TransportError("conn", type(e).__name__, unreachable=_is_unreachable(e))
    except (http.client.HTTPException, OSError) as e:
        raise TransportError("conn", type(e).__name__, unreachable=_is_unreachable(e))


def _text_of(raw: bytes) -> str:
    if not raw:
        return ""
    try:
        return raw.decode("utf-8", "replace")
    except Exception:
        return ""


def classify_messages_response(status: int, raw: bytes) -> str:
    """②a 带模型探针的响应分类（INTERFACE §7.1.1，**纯函数**，可离线单测）。

    E6 保底：400/422 且无法可靠区分「模型不支持」与「schema 缺字段」时一律
    :data:`NET`（→ 上游按「无法判定」处理为 unknown）。原则：宁可 unknown 让人手选，
    不可猜错桶。
    """
    text = _text_of(raw)
    low = text.lower()

    if status == 200:
        return PROTO_OK
    if status in (400, 422):
        if any(h in low for h in _MODEL_UNSUPPORTED_HINTS):
            return MODEL_UNSUPPORTED
        if any(h in low for h in _SCHEMA_HINTS):
            return PROTO_NOAUTH_OK
        return NET           # E6：含糊 400 ⇒ 无法可靠落类
    if status == 403:
        if any(h in low for h in _PLAN_HINTS):
            return MODEL_NOT_IN_PLAN
        return AUTH_OK       # 凭据类 / WAF（含 Cloudflare 1010）⇒ unknown
    if status == 401:
        return AUTH_OK
    if status == 404:
        return ROUTE_MISSING
    if 400 <= status < 500:
        return ROUTE_MISSING  # E4：其余 4xx 与 404 同格
    if status >= 500:
        return NET
    return NET


def classify_empty_response(status: int, raw: bytes) -> str:
    """②b 空体探针 / 地址探针的响应分类（INTERFACE §7.1.2，**纯函数**）。"""
    if status in (400, 422):
        return SCHEMA_OK
    if status in (401, 403):
        return AUTH
    if status == 404:
        return ROUTE_MISSING
    if 400 <= status < 500:
        return ROUTE_MISSING
    if status >= 500:
        return NET
    # 200：空体却不报错，不给任何类别（不据此下结论）
    return NET


def messages_headers(api_key: str) -> dict:
    return {"x-api-key": api_key, "anthropic-version": _ANTHROPIC_VERSION,
            "Content-Type": "application/json"}


def probe_model(anthropic_url: str, api_key: str, model: str,
                timeout: int = DEFAULT_TIMEOUT) -> str:
    """②a 带模型探针 —— **model 级**事实，**可能真跑并计费**（`max_tokens=1`）。

    输入固定为 ``{"model": M, "max_tokens": 1, "messages":[{"role":"user","content":"x"}]}``。
    """
    url = anthropic_url.rstrip("/") + "/v1/messages"
    body = json.dumps({
        "model": model, "max_tokens": 1,
        "messages": [{"role": "user", "content": "x"}],
    }).encode("utf-8")
    try:
        status, raw = http_request(url, method="POST",
                                   headers=messages_headers(api_key), body=body, timeout=timeout)
    except TransportError:
        return NET
    return classify_messages_response(status, raw)


def probe_empty_anthropic(anthropic_url: str, api_key: str,
                          timeout: int = DEFAULT_TIMEOUT) -> str:
    """②b 空体探针（anthropic 侧）—— **provider 级**，零 token。"""
    url = anthropic_url.rstrip("/") + "/v1/messages"
    try:
        status, raw = http_request(url, method="POST", headers=messages_headers(api_key),
                                   body=b"{}", timeout=timeout)
    except TransportError:
        return NET
    return classify_empty_response(status, raw)


def probe_empty_openai(openai_url: str, api_key: str,
                       timeout: int = DEFAULT_TIMEOUT) -> str:
    """②b 空体探针（openai 侧）—— **provider 级**，零 token。"""
    url = openai_url.rstrip("/") + "/chat/completions"
    try:
        status, raw = http_request(url, method="POST",
                                   headers={"Authorization": "Bearer %s" % api_key,
                                            "Content-Type": "application/json"},
                                   body=b"{}", timeout=timeout)
    except TransportError:
        return NET
    return classify_empty_response(status, raw)


# 最近一次地址探针收到的 HTTP 状态（thread-local）：只读旁路，供面板把「探测 404 = 地址未
# 验证」升级为可见 warning（E-15 审计条目另一修法）。**不改 probe_base_openai 的返回判据**。
_PROBE_TLS = threading.local()


def last_openai_probe_status() -> int | None:
    """本线程最近一次 ``probe_base_openai`` 收到的 HTTP 状态码；连接层失败 / 未探测 → None。"""
    return getattr(_PROBE_TLS, "status", None)


def reset_openai_probe_status() -> None:
    """发起新一轮探测前清掉，避免读到上一轮残留（调用方须在探测后立刻读）。"""
    _PROBE_TLS.status = None


def probe_base_openai(openai_url: str, api_key: str,
                      timeout: int = DEFAULT_TIMEOUT) -> bool:
    """地址探针（**仅 openai 侧**，INTERFACE §3.2）——返回 ``host 是否可达``。

    通过判据：收到**任何 HTTP 响应**（400/401/404/422 均可）= 通过；
    DNS 失败 / 连接被拒 / 超时 = 不通过。**404 不算地址错**（host 可达、仅无该端点）。

    同一原则下，**TCP 已连上而这一跳失败**（TLS 被重置 ``SSLEOFError``、服务端收下连接却
    不回响应 ``RemoteDisconnected``）也算**通过**——host 明明可达，只是探针这一次没走完；
    判成「地址错」会让用户去改一个本来就对的地址。只有 TCP 层就没连上（``TransportError
    .unreachable``）才不通过。

    副记录：本次收到的 HTTP 状态存进 thread-local（``last_openai_probe_status`` 可读），
    供上层把 404（无该端点）提示为「未验证」；判定结果与返回值一字不变。
    """
    url = openai_url.rstrip("/") + "/chat/completions"
    try:
        status, _raw = http_request(url, method="POST",
                                    headers={"Authorization": "Bearer %s" % api_key,
                                             "Content-Type": "application/json"},
                                    body=b"{}", timeout=timeout)
    except TransportError as e:
        _PROBE_TLS.status = None   # 这一跳没拿到 HTTP 响应（含 TCP 已连上但被重置）
        return not e.unreachable
    _PROBE_TLS.status = status
    return True


def extract_supported_endpoints(payload) -> dict:
    """从 ``/v1/models`` 响应里抽 ``{model_id: supported_endpoints}``（缺字段的不收录）。

    ``supported_endpoints`` 的确切键名同时兼容 ``supported_endpoints`` 与
    ``supported_endpoint_types``（OpenAI 兼容中转站两种都见过）。
    """
    out = {}
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        mid = it.get("id")
        if not isinstance(mid, str) or not mid:
            continue
        eps = it.get("supported_endpoints")
        if eps is None:
            eps = it.get("supported_endpoint_types")
        if isinstance(eps, list):
            out[mid] = [e for e in eps if isinstance(e, str)]
    return out


# ── 自测（python3 scripts/proxy_upstream.py）──────────────────────────────

def _selftest() -> None:
    # ②a 分类表（INTERFACE §7.1.1）
    assert classify_messages_response(200, b'{"type":"message"}') == PROTO_OK
    assert classify_messages_response(400, b"Use /chat/completions for this model.") == MODEL_UNSUPPORTED
    assert classify_messages_response(400, b"Model is not supported on this endpoint.") == MODEL_UNSUPPORTED
    assert classify_messages_response(400, b"missing required field: max_tokens") == PROTO_NOAUTH_OK
    assert classify_messages_response(422, b"unprocessable entity: field messages required") == PROTO_NOAUTH_OK
    assert classify_messages_response(400, b"Bad Request") == NET              # E6 含糊 400
    assert classify_messages_response(400, b"") == NET
    assert classify_messages_response(403, b"MODEL_NOT_IN_PLAN") == MODEL_NOT_IN_PLAN
    assert classify_messages_response(403, b"invalid api key") == AUTH_OK
    assert classify_messages_response(401, b"invalid key") == AUTH_OK
    assert classify_messages_response(404, b"Not found") == ROUTE_MISSING
    assert classify_messages_response(405, b"nope") == ROUTE_MISSING
    assert classify_messages_response(500, b"boom") == NET

    # ②b / 地址分类
    assert classify_empty_response(400, b"missing required field: messages") == SCHEMA_OK
    assert classify_empty_response(422, b"field required") == SCHEMA_OK
    assert classify_empty_response(404, b"Not found") == ROUTE_MISSING
    assert classify_empty_response(401, b"nope") == AUTH
    assert classify_empty_response(403, b"nope") == AUTH
    assert classify_empty_response(500, b"boom") == NET

    # supported_endpoints 抽取
    eps = extract_supported_endpoints({"data": [
        {"id": "a", "supported_endpoints": ["/messages"]},
        {"id": "b", "supported_endpoint_types": ["/chat/completions"]},
        {"id": "c"},
        {"noid": 1},
    ]})
    assert eps == {"a": ["/messages"], "b": ["/chat/completions"]}, eps
    assert extract_supported_endpoints(None) == {}
    assert extract_supported_endpoints({"data": "x"}) == {}

    # UA 常量：必须存在且非 Python 默认
    assert "Mozilla" in BROWSER_UA and "Python-urllib" not in BROWSER_UA
    assert "User-Agent" in browser_headers()

    print("proxy_upstream selftest OK")


if __name__ == "__main__":
    _selftest()
