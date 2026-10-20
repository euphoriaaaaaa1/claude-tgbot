# -*- coding: utf-8 -*-
"""dsh 页面/接口转发（INTERFACE §2.14）。

``/hub/<path>`` 原样转发到 ``CONSOLE_DSH_ADAPTER_URL`` + ``/adapter/v1/<path>``：

- 方法、查询串、请求体、Content-Type 保留；
- 请求体上限 4 MiB，超限 413；
- 未配置 503、连不上 502、超时 504；
- 只转发回环地址（与适配器同一安全边界），错误体不含令牌。
"""
import http.client
import os
import posixpath
import urllib.parse

from flask import Blueprint, Response, request

from adapters.guard import NotLoopbackError, loopback_address

hub_forward_bp = Blueprint("hub_forward", __name__)

API_PREFIX = "/adapter/v1"
MAX_BODY = 4 * 1024 * 1024
TIMEOUT = 30
DEFAULT_TOKEN_FILE = "~/.config/claude-dsh-console/adapter.token"


def _target() -> tuple[str, int, str]:
    """返回 (host, port, 路径前缀)；未配置/非法/非回环抛 ValueError。"""
    raw = os.environ.get("CONSOLE_DSH_ADAPTER_URL") or ""
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("backend_unconfigured")
    try:
        # 目标必须是本机回环：这个转发会带上适配器令牌。
        host = loopback_address(parts.hostname)
    except NotLoopbackError:
        raise ValueError("backend_unconfigured") from None
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return host, port, parts.path.rstrip("/")


def _safe_path(path: str):
    """归一化并校验转发路径；不合法返回 None。

    拒绝：控制字符、反斜杠、`..` 段、`//`、非 `/` 开头（归一化后）。
    百分号编码只解一次；解完仍含 `%` 视为多重编码，直接拒绝（`%25252e%252e`
    这类会剩下 `%`，后端再解一次就是 `..`）。
    """
    if not isinstance(path, str):
        return None
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in path):
        return None
    if "\\" in path or "//" in path:
        return None
    decoded = urllib.parse.unquote(path)
    if "%" in decoded:
        return None
    if decoded != path:
        if any(ord(c) < 0x20 or ord(c) == 0x7f for c in decoded) or "\\" in decoded or "//" in decoded:
            return None
        if ".." in decoded.split("/"):
            return None
    if ".." in path.split("/"):
        return None
    norm = posixpath.normpath("/" + path.lstrip("/"))
    if not norm.startswith("/") or norm == "/":
        return None
    return norm


def _token() -> str:
    path = os.environ.get("CONSOLE_ADAPTER_TOKEN_FILE") or DEFAULT_TOKEN_FILE
    try:
        with open(os.path.expanduser(path), "r", encoding="utf-8") as f:
            token = f.read(4096).strip()
    except OSError:
        return ""
    return token if token.isascii() and token.isprintable() else ""


def _fail(status: int, error: str):
    return Response('{"error":"%s"}' % error, status=status, mimetype="application/json")


@hub_forward_bp.route("/hub/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def hub_proxy(path: str):
    try:
        host, port, prefix = _target()
    except ValueError:
        return _fail(503, "backend_unconfigured")

    clean = _safe_path(path)
    if clean is None:
        return _fail(404, "not_found")

    body = request.get_data(cache=False)
    if len(body) > MAX_BODY:
        return _fail(413, "too_large")

    target = prefix + API_PREFIX + clean
    if request.query_string:
        target += "?" + request.query_string.decode("utf-8", "replace")
    headers = {"Accept": request.headers.get("Accept", "*/*")}
    if body:
        headers["Content-Type"] = request.headers.get("Content-Type", "application/json")
    token = _token()
    if token:
        headers["Authorization"] = "Bearer " + token

    conn = http.client.HTTPConnection(host, port, timeout=TIMEOUT)
    try:
        try:
            conn.request(request.method, target, body=body or None, headers=headers)
            resp = conn.getresponse()
            raw = resp.read(MAX_BODY + 1)
        except TimeoutError:
            return _fail(504, "dsh_timeout")
        except (OSError, http.client.HTTPException):
            return _fail(502, "dsh_down")
    finally:
        conn.close()

    if len(raw) > MAX_BODY:
        return _fail(502, "dsh_response_too_large")

    out_headers = {}
    ctype = resp.getheader("Content-Type")
    if ctype:
        out_headers["Content-Type"] = ctype
    csp = resp.getheader("Content-Security-Policy")
    if csp:
        out_headers["Content-Security-Policy"] = csp
    if "html" in (ctype or "").lower():
        out_headers["X-Frame-Options"] = "SAMEORIGIN"
    return Response(raw, status=resp.status, headers=out_headers)
