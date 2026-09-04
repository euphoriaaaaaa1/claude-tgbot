# -*- coding: utf-8 -*-
"""管理台鉴权门（INTERFACE §1）。

设计照抄 voicecall/server.py（无服务端会话表的签名 cookie、按 scheme 条件设 Secure、
GET+text/html 302 其余 401、compare_digest），代码不能复用：那边是 FastAPI/ASGI。

两把口令（§1.0 方案 B）：
- ``HUB_ACCESS_PASSWORD`` 管全站，cookie ``hub_session``，盐串 ``hubv2|``
- ``HUB_ADMIN_PASSWORD`` 可选，只在 ``/hub`` 之上再加一道，cookie ``hub_admin``，盐串 ``hubadminv2|``
两把允许设成同值 —— 所以盐串必须不同，否则一个 cookie 能冒充另一个，两道锁塌成一道。

口令不存哈希：由 env 注入，进程内本就持有明文，再哈希一遍不减少攻击面（PLAN D3）。
"""
import hashlib
import hmac
import ipaddress
import os
import posixpath
import re
import secrets
import threading
import time
from collections import deque

from flask import jsonify, redirect, render_template, request

from moments import env_file

COOKIE_SESSION = "hub_session"
COOKIE_ADMIN = "hub_admin"
SALT_SESSION = "hubv2|"
SALT_ADMIN = "hubadminv2|"
COOKIE_TTL = 7 * 86400

# §1.3 放行清单：精确相等匹配。禁止 startswith —— 否则 /login-bypass 之类会被误放行。
PASSLIST = ("/login", "/hub/healthz")

WEAK_PASSWORD_LEN = 12


def access_password():
    """全站口令。每次现读，进程内不缓存 —— 改配置重启即生效，测试也好隔离。

    🔴 BUG-21 / 安审 F1：**必须逐键回落 ``hub.env``**，与 cliproxy 三键同一口径。
    口令由 ``hub_bootstrap`` 生成后只写进 ``~/.claude-tgbot/hub.env``，而三条自启路径
    （launchd plist / systemd unit / Windows 计划任务）**没有一条 source 它**。
    只读 ``os.environ`` 的话，按文档装完的机器上这里恒为空 → ``gate_open()`` 恒 False
    → 整站零鉴权，而功能测试全绿。
    """
    return env_file.get("HUB_ACCESS_PASSWORD")


def admin_password():
    """管理口令，可选。未设 → /hub* 退化为单口令（§1.0 规则 1）。回落同上。"""
    return env_file.get("HUB_ADMIN_PASSWORD")


def gate_open() -> bool:
    return bool(access_password())


# ---------- cookie 签发与校验（§1.2） ----------

def _sign(password: str, salt: str, exp: int, nonce: str) -> str:
    msg = "%s%d|%s" % (salt, exp, nonce)
    return hmac.new(password.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_cookie(password: str, salt: str, ttl: int = COOKIE_TTL) -> str:
    """``<exp>.<16位hex nonce>.<hmac_sha256>``。带 exp+nonce：cookie 不是口令的确定性函数，
    泄漏一次不等于永久凭据；仍然无服务端会话表，改口令即全体失效。"""
    exp = int(time.time()) + ttl
    nonce = secrets.token_hex(8)
    return "%d.%s.%s" % (exp, nonce, _sign(password, salt, exp, nonce))


def verify_cookie(value: str, password: str, salt: str) -> bool:
    """格式合法 → 未过期 → 常量时间比签名。任一不过即视为无 cookie（不可区分）。"""
    if not value or not password:
        return False
    parts = value.split(".")
    if len(parts) != 3:
        return False
    exp_raw, nonce, sig = parts
    try:
        exp = int(exp_raw)
    except ValueError:
        return False
    if exp <= time.time():
        return False
    return hmac.compare_digest(sig, _sign(password, salt, exp, nonce))


# ---------- 爆破防护（§1.5）：进程内全局滑窗，不按 IP、不按 cookie 分桶 ----------

class LoginGuard:
    """10 分钟滑窗记全部登录失败。

    不按 IP 分桶：门户在 tunnel 之后，源 IP 多是 CF 边缘地址，分桶等于不设防；
    不按 cookie 分桶：那个罐子由攻击者自己保管，不带 cookie 就永远停在 n=1。
    # ponytail: 全局计数的已知 DoS 面 —— 攻击者能让登录页变慢/返回 429，
    # 其余页面不受影响。要更强就上反代限流，别在这里加复杂度。
    """
    WINDOW = 600
    HARD_LIMIT = 20
    MAX_DELAY = 5.0

    def __init__(self):
        self._fails = deque()
        self._lock = threading.Lock()

    def _prune(self, now):
        while self._fails and now - self._fails[0] > self.WINDOW:
            self._fails.popleft()

    def blocked(self) -> bool:
        now = time.time()
        with self._lock:
            self._prune(now)
            return len(self._fails) >= self.HARD_LIMIT

    def record_failure(self) -> int:
        now = time.time()
        with self._lock:
            self._prune(now)
            self._fails.append(now)
            return len(self._fails)

    def reset(self):
        with self._lock:
            self._fails.clear()

    def delay_for(self, n: int) -> float:
        return min(0.5 * n, self.MAX_DELAY)


# ---------- 启动检查（§1.1） ----------

def is_loopback(host: str) -> bool:
    """回环判定：ip_address().is_loopback，localhost 显式加入。
    **解析失败一律按非回环处理（fail-closed）** —— 主机名拼错不能变成"默认放行"。"""
    h = (host or "").strip().strip("[]")
    if h.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def check_startup(host: str, out=None, err=None):
    """启动前自检。返回 None 表示可以起；返回 int 表示应以该码退出。

    调用方（web.py 的 __main__）拿到非 None 就 sys.exit，**不得 bind 端口**。
    任何输出都不含口令值。
    """
    import sys
    out = out or sys.stdout
    err = err or sys.stderr
    pw = access_password()
    if admin_password() and not pw:
        print("WARN: 只设了 HUB_ADMIN_PASSWORD 没设 HUB_ACCESS_PASSWORD —— /hub 会被关死"
              "（管理锁是叠加在全站锁之上的，拿不到 hub_session 就进不去）。补上 "
              "HUB_ACCESS_PASSWORD 才能登录管理台。", file=out)
    if pw:
        if len(pw) < WEAK_PASSWORD_LEN:
            print("WARN: HUB_ACCESS_PASSWORD 短于 %d 字符，建议换成 `python3 -c \"import secrets;"
                  "print(secrets.token_urlsafe(24))\"` 生成的随机串" % WEAK_PASSWORD_LEN, file=out)
        return None
    if is_loopback(host):
        # 绑回环仍然放行：本机自用不该被逼着设口令。但 BRIEF 的暴露方式是 cloudflare
        # tunnel（守护进程从本机连 127.0.0.1:8765），这道闸永远不触发 —— 所以这里必须
        # 出声，否则"隧道一挂就全网可写 key / 改 settings / 重启 bot"没有任何可发现路径。
        print("WARN: 未设 HUB_ACCESS_PASSWORD —— 管理台当前无鉴权。仅本机访问没问题；"
              "**挂 cloudflare tunnel 之类的公网隧道前必须先设口令**"
              "（写进 ~/.claude-tgbot/hub.env，见 README「管理台鉴权」）。", file=err)
        return None
    print("REFUSE: MOMENTS_WEB_HOST=%s 不是回环地址，且未设 HUB_ACCESS_PASSWORD —— "
          "拒绝把无口令的门户 bind 到非本机地址。要么改回 127.0.0.1，"
          "要么先设 HUB_ACCESS_PASSWORD（见 README）。" % host, file=err)
    return 2


# ---------- 挂门 ----------

def _set_cookie(resp, name: str, value: str):
    # Secure 只在 https 下设：本机 http://127.0.0.1 下设了浏览器根本不存，用户永远登不上。
    # 隧道部署（浏览器侧 https、到本进程是明文 http）想拿到 Secure，设
    # HUB_TRUST_PROXY=1 让 web.py 挂上 ProxyFix —— scheme 就会按 X-Forwarded-Proto 走。
    # 这里不直接读转发头：那等于**无条件**信任一个任何人都能伪造的请求头。
    resp.set_cookie(name, value, max_age=COOKIE_TTL, httponly=True,
                    samesite="Lax", secure=(request.scheme == "https"), path="/")


def _deny(error_code: str):
    """§1.4 两种形状。cookie 不合法与 cookie 缺失走的是同一条路径，响应完全一致。"""
    if request.method in ("GET", "HEAD") and "text/html" in (request.headers.get("Accept") or ""):
        return redirect("/login", code=302)
    resp = jsonify({"error": error_code})
    resp.status_code = 401
    resp.headers["Hub-Gate"] = "1"    # 前端 fetch 的判别位：带此头才跳登录
    return resp


def is_admin_path(path: str) -> bool:
    """管理锁的作用面判定：先归一化再比。

    `/./hub/api/x`、`/x/../hub`、`/HUB` 今天靠"路由匹配不上回 404"兜着，
    前置反代（nginx/CF）一做归一化就变成真绕过 —— 判定不能依赖路由表兜底。
    尾界要卡死：`/hubx` 不是管理面，别误锁。
    """
    p = posixpath.normpath(re.sub(r"^/+", "/", path or "/")).lower()
    return p == "/hub" or p.startswith("/hub/")


def _eq(supplied, expected) -> bool:
    return hmac.compare_digest((supplied or "").encode("utf-8"), (expected or "").encode("utf-8"))


def install(app):
    """挂 before_request 门 + /login 两个方法 + 免鉴权的 /hub/healthz。"""
    guard = LoginGuard()
    # 管理口令的失败自成一窗。合用一个窗时，"只填 access 的正常登录"会顺手把 admin 的
    # 失败计数一并清掉 —— 持 access 口令的人（家人）交替 [猜 admin, 正常登录] 就能无限猜。
    admin_guard = LoginGuard()
    app.extensions["hub_login_guard"] = guard
    app.extensions["hub_admin_login_guard"] = admin_guard

    @app.before_request
    def _hub_gate():
        path = request.path
        admin_pw = admin_password()
        # 只设 HUB_ADMIN_PASSWORD 没设 HUB_ACCESS_PASSWORD：用户显然想锁管理面。
        # 按 §1.0 规则 3（两个 cookie 缺一不可）这就是个**拿不到 hub_session** 的配置，
        # 管理面只能关死，绝不能因为"门关闭"就把 /hub 裸奔出去。
        admin_scope = bool(admin_pw) and is_admin_path(path)
        if not gate_open() and not admin_scope:
            return None                      # 门关闭：行为与改造前完全一致
        if path in PASSLIST:                 # 精确相等，不是前缀
            return None
        if not verify_cookie(request.cookies.get(COOKIE_SESSION), access_password(), SALT_SESSION):
            return _deny("unauthorized")
        if admin_scope:
            # 叠加的第二道锁：两个 cookie 缺一不可（§1.0 规则 3）
            if not verify_cookie(request.cookies.get(COOKIE_ADMIN), admin_pw, SALT_ADMIN):
                return _deny("admin_unauthorized")
        return None

    @app.route("/hub/healthz")
    def hub_healthz():
        """门户自己的健康检查（不是 cliproxy 的那个）。免鉴权，只回 status，多一个字段都是泄配置。"""
        return jsonify({"status": "ok"})

    @app.route("/login", methods=["GET", "POST"])
    def hub_login():
        if not gate_open():
            return redirect("/", code=303)   # 门关时登录无意义
        if request.method == "GET":
            return render_template("login.html", need_admin=bool(admin_password()), error=False)

        if guard.blocked() or admin_guard.blocked():
            resp = jsonify({"error": "too_many_attempts", "detail": "稍后再试"})
            resp.status_code = 429
            resp.headers["Retry-After"] = "60"
            return resp

        pw, admin_pw = access_password(), admin_password()
        # 缺字段 / Content-Type 不是表单编码 → request.form 为空 → 与口令错误同一条路径。
        # 不得 KeyError→500，也不得用 400 泄漏"字段格式对不对"这一位信息。
        if not _eq(request.form.get("password"), pw):
            n = guard.record_failure()
            print("hub_login ok=false n=%d" % n, flush=True)
            time.sleep(guard.delay_for(n))
            page = render_template("login.html", need_admin=bool(admin_pw), error=True)
            return page, 401

        supplied_admin = request.form.get("admin_password") or ""
        admin_ok = bool(admin_pw) and _eq(supplied_admin, admin_pw)
        # admin 留空 = 家人只要浏览权的正常路径，§1.5 明说不计入爆破计数；
        # admin 填了却填错 = 有人在猜管理口令。全站口令本来就会分给家人，家人正是管理锁的
        # 攻击面，这条内锁不能是零成本的 —— 计入同一个滑窗，享受同样的递增延迟与 429。
        if admin_pw and supplied_admin and not admin_ok:
            n = admin_guard.record_failure()
            print("hub_login ok=false n=%d scope=admin" % n, flush=True)
            time.sleep(admin_guard.delay_for(n))
        else:
            guard.reset()               # 全站口令对了，全站窗清掉
            if admin_ok:
                admin_guard.reset()     # 管理窗**只能**由"两把全对"清
            print("hub_login ok=true n=0", flush=True)
        # 只有全站口令对 → 只签一个 cookie，跳 `/`（§1.5 表第二行）
        resp = redirect("/hub" if (not admin_pw or admin_ok) else "/", code=303)
        _set_cookie(resp, COOKIE_SESSION, issue_cookie(pw, SALT_SESSION))
        if admin_ok:
            _set_cookie(resp, COOKIE_ADMIN, issue_cookie(admin_pw, SALT_ADMIN))
        return resp

    return app
