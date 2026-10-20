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
import urllib.parse

from flask import Blueprint, Response, request

hub_forward_bp = Blueprint("hub_forward", __name__)

API_PREFIX = "/adapter/v1"
MAX_BODY = 4 * 1024 * 1024
TIMEOUT = 30
DEFAULT_TOKEN_FILE = "~/.config/claude-dsh-console/adapter.token"


def _target() -> tuple[str, int, str]:
    """返回 (host, port, 路径前缀)；未配置/非法抛 ValueError。"""
    raw = os.environ.get("CONSOLE_DSH_ADAPTER_URL") or ""
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("unconfigured")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return parts.hostname, port, parts.path.rstrip("/")


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
        return _fail(503, "dsh_unconfigured")

    body = request.get_data(cache=False)
    if len(body) > MAX_BODY:
        return _fail(413, "too_large")

    target = prefix + API_PREFIX + "/" + path
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
