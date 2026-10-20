# -*- coding: utf-8 -*-
"""8767 管理台壳（Flask）。

启动：``MOMENTS_WEB_MODE=provider python3 -m moments.web``
访问：http://127.0.0.1:8767

T06 只落地“壳”本身：登录门、登录限速、同源 + JSON 写请求校验、安全响应头、
``next`` 站内路径校验，以及可启动的 ``/provider`` 占位页。console / lifecycle /
features / providers / styles 等页面和 API 蓝图由后续任务注册；本模块不 import
已删除的旧 Hub 模块，也不提供旧 8765 朋友圈页面。
"""
import os
import sys
from urllib.parse import quote, unquote, urlsplit

from flask import (Flask, abort, jsonify, make_response, redirect,
                   render_template, request)
from werkzeug.exceptions import HTTPException

from moments import panel_auth

# 公众版只有供应商模式；cookie 名按 INTERFACE §2.1 固定为 cb_provider。
COOKIE_NAME = panel_auth.COOKIE_NAME

app = Flask(__name__,
            template_folder=os.path.join(os.path.dirname(__file__), "templates"))


# ── 安全响应头 ─────────────────────────────────────────────────────────────
@app.after_request
def _security_headers(resp):
    # 面板可能被挂到隧道后面，是 iframe 点击劫持的目标。只加 frame-ancestors：
    # 页面模板是内联样式/脚本，加 default-src 之类会把页面自身一起挡掉。
    if resp.headers.get("X-Frame-Options") != "SAMEORIGIN":
        resp.headers["X-Frame-Options"] = "DENY"
    if "Content-Security-Policy" not in resp.headers:
        resp.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    return resp


# /api/* 的错误体统一为 JSON；页面路由仍返回 Flask 默认 HTML 错误页。
@app.errorhandler(HTTPException)
def _api_http_error(err):
    if request.path.startswith("/api/"):
        return jsonify({"error": err.name.lower().replace(" ", "_")}), err.code
    return err.get_response()


# ── 登录门（INTERFACE §2.1）────────────────────────────────────────────────
_NOT_READY_HTML = ("<html><body><p>面板未配置口令，请先运行 "
                   "python3 -m moments.panel_auth --set-password</p></body></html>")


def _safe_next(value) -> str:
    """白名单式：只放行本站相对路径，其余一律回落 ``/``。

    先看**原值**再看拆分结果：``urlsplit`` 会剥掉 tab/换行，也会把开头的 ``//``
    当成主机段；只信拆分结果会漏掉 ``/\\t/evil.com``、``///evil.com`` 这类形状。
    百分号编码的控制字符和斜杠也解一次再查 —— ``/%09/evil.com`` 解出来是
    ``/\\t/evil.com``，浏览器/中间层可能把它还原成协议相对 URL。
    """
    if not isinstance(value, str) or not value:
        return "/"
    if any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in value):
        return "/"
    if "\\" in value:
        return "/"
    if not value.startswith("/") or value.startswith("//"):
        return "/"
    try:
        parts = urlsplit(value)
    except ValueError:
        return "/"
    if parts.scheme or parts.netloc:
        return "/"

    try:
        decoded = unquote(value, errors="strict")
    except UnicodeDecodeError:
        return "/"
    if any(ord(c) < 0x20 or 0x7F <= ord(c) <= 0x9F for c in decoded):
        return "/"
    if "\\" in decoded or decoded.startswith("//"):
        return "/"
    try:
        dparts = urlsplit(decoded)
    except ValueError:
        return "/"
    if dparts.scheme or dparts.netloc:
        return "/"
    if not dparts.path.startswith("/") or dparts.path.startswith("//"):
        return "/"
    return value


def _authed() -> bool:
    sid = panel_auth.parse_cookie(request.cookies.get(COOKIE_NAME))
    return bool(sid and panel_auth.session_valid(sid))


@app.before_request
def _login_gate():
    if not panel_auth.secret_ready():
        return _NOT_READY_HTML, 503  # fail-closed：缺 secret 一律拒之门外
    if request.path == "/login":
        return None
    if _authed():
        return None
    if request.path.startswith("/hub/api/"):
        return jsonify({"error": "unauthorized"}), 401, {"Hub-Gate": "1"}
    if request.path.startswith("/api/"):
        return jsonify({"error": "unauthorized"}), 401
    target = request.path
    if request.query_string:
        target += "?" + request.query_string.decode("utf-8", "replace")
    return redirect("/login?next=" + quote(target, safe="/"))


def _source_host_matches(src: str) -> bool:
    """Origin/Referer 的 host 是否与请求 Host 一致（跨站 → False）。"""
    try:
        src_host = urlsplit(src).hostname
    except ValueError:
        return False
    if not src_host:
        return False  # Origin: null（沙箱/站外）一律不认
    try:
        req_host = urlsplit("//" + (request.host or "")).hostname
    except ValueError:
        req_host = None
    return bool(req_host) and src_host.lower() == req_host.lower()


@app.before_request
def _api_csrf_guard():
    """``/api/*`` 的非 GET 请求先验来源、再验内容类型。

    顺序固定为「来源 → 内容类型」：来源不合 403；来源合但 Content-Type 非 JSON 415。
    浏览器跨站请求必带 Origin，故没有 Origin/Referer 时不再校验内容类型 —— 那样会
    误伤同源脚本和历史无体动作端点。
    """
    if request.method in ("GET", "HEAD", "OPTIONS") or not request.path.startswith("/api/"):
        return None
    src = request.headers.get("Origin") or request.headers.get("Referer") or ""
    if not src:
        return None
    if not _source_host_matches(src):
        return jsonify({"error": "cross-site request rejected"}), 403
    if request.mimetype != "application/json":
        return jsonify({"error": "content-type must be application/json"}), 415
    return None


@app.route("/login", methods=["GET"])
def login_page():
    return render_template("login.html",
                           next=_safe_next(request.args.get("next")), error=None)


@app.route("/login", methods=["POST"])
def login_submit():
    nxt = _safe_next(request.form.get("next"))
    ip = panel_auth.client_key(request)
    remaining = panel_auth.is_blocked(ip)
    if remaining:
        resp = make_response(render_template(
            "login.html", next=nxt, error="尝试过于频繁，请稍后再试"), 429)
        resp.headers["Retry-After"] = str(remaining)
        return resp
    if not panel_auth._LOGIN_SEM.acquire(blocking=False):
        resp = make_response(render_template(
            "login.html", next=nxt, error="服务器繁忙，请稍后再试"), 429)
        resp.headers["Retry-After"] = "1"
        return resp
    try:
        if panel_auth.verify_password(request.form.get("password") or ""):
            panel_auth.clear_failures(ip)
            sid = panel_auth.new_session()
            resp = redirect(nxt)
            resp.set_cookie(
                COOKIE_NAME, panel_auth.make_cookie(sid),
                max_age=panel_auth.COOKIE_MAX_AGE, httponly=True, samesite="Lax",
                path="/",
                secure=(request.headers.get("X-Forwarded-Proto") == "https"
                        or request.is_secure))
            resp.headers["Cache-Control"] = "no-store"
            sys.stderr.write("[login] ok ip=%s path=%s\n" % (ip, nxt))
            return resp
        panel_auth.register_failure(ip)
        return render_template("login.html", next=nxt, error="口令错误"), 401
    finally:
        panel_auth._LOGIN_SEM.release()


@app.route("/logout", methods=["POST"])
def logout():
    sid = panel_auth.parse_cookie(request.cookies.get(COOKIE_NAME))
    if sid:
        panel_auth.revoke(sid)
    resp = redirect("/login")
    resp.set_cookie(COOKIE_NAME, "", max_age=0, path="/",
                    httponly=True, samesite="Lax")
    return resp


@app.route("/api/panel/logout_all", methods=["POST"])
def logout_all():
    return jsonify({"ok": True, "revoked": panel_auth.revoke_all()})


_SESSION_ENDPOINTS = frozenset(
    f.__name__ for f in (login_page, login_submit, logout, logout_all))


@app.route("/provider")
def provider_page():
    return render_template("provider.html")


@app.before_request
def _reachable_gate():
    """登录后的可达面：T06 只有登录/登出与 /provider 占位页。

    未匹配到路由的请求交给 Flask 回 404/405。后续任务的蓝图注册后，把对应端点
    加进这里或按蓝图名放行。
    """
    if request.path == "/":
        return redirect("/provider")
    endpoint = request.endpoint
    if endpoint is None:
        return None
    if endpoint in _SESSION_ENDPOINTS or endpoint == "provider_page":
        return None
    abort(404)


# ── 启动参数（INTERFACE §1.2）──────────────────────────────────────────────
class _StartupError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _listen_config(environ=None):
    """返回 (host, port)；非法配置抛 ``_StartupError``（不 bind 端口）。"""
    env = os.environ if environ is None else environ
    mode = env.get("MOMENTS_WEB_MODE") or ""
    if mode not in ("", "provider"):
        raise _StartupError(1, "[moments.web] MOMENTS_WEB_MODE 只接受 provider")
    host = env.get("MOMENTS_WEB_HOST") or "127.0.0.1"
    raw_port = env.get("MOMENTS_WEB_PORT") or "8767"
    digits = (raw_port.lstrip("0")
              if raw_port.isascii() and raw_port.isdigit() else "")
    port = int(digits) if 0 < len(digits) <= 5 else 0
    if host != "127.0.0.1" or not 1 <= port <= 65535:
        raise _StartupError(
            2, "[moments.web] provider 模式启动参数非法："
               "只绑 127.0.0.1，端口须为 1-65535 的整数")
    return host, port


# ── 兼容：moments/post.py 惰性导入的 worker 拉起入口 ──────────────────────
# T16 的朋友圈投递仍走这里；本函数不参与 T06 的壳路由，导入时也不加载网络/令牌。
def _ensure_worker_alive(bot_id: str, chat_id: str, bot_dir: str):
    import urllib.request
    from bots_registry import disabled_ids_safe, read_dispatcher_token
    from moments.bots_client import bot_port

    if bot_id in disabled_ids_safe():
        sys.stderr.write("[moments.web] %s stopped, skip\n" % bot_id)
        return None
    port = bot_port(bot_id)
    if not port:
        sys.stderr.write("[ensure_worker] 未知 bot %s，跳过 spawn\n" % bot_id)
        return None
    try:
        token = read_dispatcher_token(bot_id, bot_dir)
        if not token:
            sys.stderr.write(
                "[ensure_worker] api_token_missing bot=%s：未读到 .api-token，"
                "拒绝本次调用（fail-closed）\n" % bot_id)
            return None
        req = urllib.request.Request(
            "http://127.0.0.1:%d/ensure_worker" % port, method="POST",
            headers={"X-Api-Token": token})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        sys.stderr.write("[ensure_worker] %s 失败(dispatcher 未起?): %s\n"
                         % (bot_id, e))
    return None


if __name__ == "__main__":
    try:
        host, port = _listen_config()
    except _StartupError as e:
        sys.stderr.write(e.message + "\n")
        sys.exit(e.code)
    # 收敛器属于供应商栈（T09）；T06 壳如实报 off。
    sys.stderr.write("[moments.web] startup mode=provider port=%d converger=off\n"
                     % port)
    sys.stderr.flush()
    app.run(host=host, port=port, debug=False)
