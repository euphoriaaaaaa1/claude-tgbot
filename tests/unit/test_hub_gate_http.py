# -*- coding: utf-8 -*-
"""M0 单测（HTTP 层）：只装一个裸 Flask + hub_auth.install，不碰 db / 朋友圈路由。

覆盖攻击面实测报出的三条边缘：admin 内锁的爆破防护、admin-only 配置、路径归一化。
"""
import sys
import time
from pathlib import Path

import pytest
from flask import Flask

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from moments import hub_auth as A  # noqa: E402

PW = "hub-access-pw-12345"
ADMIN = "hub-admin-pw-67890"
FETCH = {"Accept": "*/*"}


@pytest.fixture
def app_client(monkeypatch):
    def _make(access=None, admin=None):
        for k, v in (("HUB_ACCESS_PASSWORD", access), ("HUB_ADMIN_PASSWORD", admin)):
            monkeypatch.setenv(k, v) if v else monkeypatch.delenv(k, raising=False)
        app = Flask(__name__, template_folder=str(Path(A.__file__).parent / "templates"))
        app.config["TESTING"] = True

        @app.route("/")
        def home():
            return "feed"

        @app.route("/hub")
        @app.route("/hub/api/provider")
        @app.route("/hubx")
        def hub():
            return "TOP-SECRET-HUB-PAGE"

        A.install(app)
        return app.test_client()
    return _make


def _login(c, pw=None, admin=None):
    form = {}
    if pw is not None:
        form["password"] = pw
    if admin is not None:
        form["admin_password"] = admin
    return c.post("/login", data=form, content_type="application/x-www-form-urlencoded")


def test_admin填错也要计入爆破窗口并延迟(app_client):
    """access 会分给家人，家人正是管理锁的攻击面 —— admin 内锁不能零成本猜。"""
    c = app_client(PW, ADMIN)
    t0 = time.time()
    r = _login(c, PW, "wrong-admin-1")
    first = time.time() - t0
    assert r.status_code == 303 and r.headers["Location"].endswith("/")   # §1.5 表第二行不变
    t0 = time.time()
    _login(c, PW, "wrong-admin-2")
    assert time.time() - t0 > first, "第二次猜 admin 没有变慢，爆破成本为零"


def test_admin留空是正常路径_不计失败不延迟(app_client):
    """§1.5：家人只拿到全站口令时 admin 留空，不算失败、不计入爆破计数。"""
    c = app_client(PW, ADMIN)
    for _ in range(3):
        t0 = time.time()
        r = _login(c, PW, "")
        assert r.status_code == 303
        assert time.time() - t0 < 0.4, "admin 留空被当成失败在延迟"


def test_admin填错不得清空全站失败计数(app_client):
    """原缺陷：走到 guard.reset() 把 n 打回 1，等于给全站口令爆破送了个免费重置键。"""
    c = app_client(PW, ADMIN)
    for i in range(3):
        _login(c, "bad-%d" % i)
    ext = c.application.extensions
    _login(c, PW, "wrong-admin")
    assert ext["hub_login_guard"].record_failure() == 4, "错误 admin 口令重置了全站爆破窗口"
    assert ext["hub_admin_login_guard"].record_failure() == 2, "admin 失败没进 admin 窗"


def test_只填access成功后_admin失败计数不清(app_client):
    """持 access 口令者交替 [猜 admin, 只填 access 正常登录] 就能无限猜 —— 管理窗只能由两把全对清。"""
    c = app_client(PW, ADMIN)
    ag = c.application.extensions["hub_admin_login_guard"]
    _login(c, PW, "guess-1")
    assert _login(c, PW).status_code == 303          # 只填 access 的成功（免费清窗动作）
    assert ag.record_failure() == 2, "只填 access 的成功清掉了 admin 窗"
    ag.reset()
    _login(c, PW, "guess-2")
    assert _login(c, PW, ADMIN).status_code == 303   # 两把全对
    assert ag.record_failure() == 1, "两把全对没清 admin 窗"


def test_两把全对才清窗(app_client):
    c = app_client(PW, ADMIN)
    _login(c, "bad-0")
    guard = c.application.extensions["hub_login_guard"]
    assert _login(c, PW, ADMIN).status_code == 303
    assert guard.record_failure() == 1


def test_只设admin口令时_hub关死而不是裸奔(app_client):
    """配置误用：用户显然想锁管理面。§1.0 规则 3 下这是个拿不到 hub_session 的配置，
    只能关死；绝不能因为"门关闭"就把 /hub 连同管理 API 一起裸奔出去。"""
    c = app_client(access=None, admin=ADMIN)
    for path in ("/hub", "/hub/api/provider"):
        r = c.get(path, headers=FETCH)
        assert r.status_code == 401, "%s 裸奔了" % path
        assert b"TOP-SECRET-HUB-PAGE" not in r.get_data()
    assert c.get("/", headers=FETCH).status_code == 200, "全站锁没开，朋友圈不该受影响"
    assert c.get("/hub/healthz").status_code == 200, "healthz 是放行清单，别一起关掉"


def test_只设admin口令时启动打WARN不拒绝启动(monkeypatch):
    import io
    monkeypatch.delenv("HUB_ACCESS_PASSWORD", raising=False)
    monkeypatch.setenv("HUB_ADMIN_PASSWORD", ADMIN)
    out, err = io.StringIO(), io.StringIO()
    assert A.check_startup("127.0.0.1", out=out, err=err) is None
    assert any(l.startswith("WARN:") for l in (out.getvalue() + err.getvalue()).splitlines())
    assert ADMIN not in out.getvalue() + err.getvalue()


@pytest.mark.parametrize("path", ["/hub", "/hub/", "/hub/api/provider", "/./hub",
                                  "/x/../hub/api/provider", "/HUB", "/Hub/API/x"])
def test_管理锁的路径变体一律锁住(app_client, path):
    """靠路由 404 兜住不算防住：前置反代一归一化就是真绕过。"""
    c = app_client(PW, ADMIN)
    _login(c, PW)                      # 只有全站 cookie
    r = c.get(path, headers=FETCH)
    assert r.status_code == 401 and r.get_json()["error"] == "admin_unauthorized", path


def test_尾界_hubx不是管理面(app_client):
    """§1.0：管理锁只管 `/hub` 与 `/hub/*`。/hubx 是别的路由，不该被误锁。"""
    c = app_client(PW, ADMIN)
    _login(c, PW)
    r = c.get("/hubx", headers=FETCH)
    assert r.status_code == 200, "/hubx 被管理锁误伤"


def test_归一化不放宽放行清单(app_client):
    """反向：放行清单仍是精确相等，/./login 不得被当成 /login 放行（fail-closed）。"""
    c = app_client(PW, ADMIN)
    assert c.get("/./login", headers=FETCH).status_code == 401


def test_管理面判定的纯函数边界():
    """`//hub` 只能在这一层验：werkzeug 的 test client 会把它当协议相对 URL（host=hub）。"""
    for p in ("/hub", "/hub/", "//hub", "///hub/api", "/./hub", "/x/../hub", "/HUB", "/hub/api/x"):
        assert A.is_admin_path(p), p
    for p in ("/", "/hubx", "/hub-secret", "/styles", "/api/moments", "/hub/../x", ""):
        assert not A.is_admin_path(p), p


# ---------------- 安审 S4：隧道下的 cookie Secure（HUB_TRUST_PROXY，默认关）----------------

def _web_app(monkeypatch, trust_proxy=None):
    """按给定 env 重载真的 moments.web —— ProxyFix 挂在那边的模块顶层，照抄一份等于没测。"""
    import importlib
    monkeypatch.setenv("HUB_ACCESS_PASSWORD", PW)
    if trust_proxy is None:
        monkeypatch.delenv("HUB_TRUST_PROXY", raising=False)
    else:
        monkeypatch.setenv("HUB_TRUST_PROXY", trust_proxy)
    mod = sys.modules.get("moments.web")
    mod = importlib.reload(mod) if mod else importlib.import_module("moments.web")
    mod.app.config["TESTING"] = True
    return mod.app.test_client()


def _login_cookies(c, **headers):
    r = c.post("/login", data={"password": PW}, headers=headers)
    got = [v for k, v in r.headers.items() if k == "Set-Cookie"]
    assert got, "登录没签出 cookie（状态码 %s）" % r.status_code
    return got


def test_默认不信转发头_伪造的proto拿不到Secure(monkeypatch):
    """直连 http://127.0.0.1:8765 时任何人都能塞 X-Forwarded-Proto，信了等于白送。"""
    got = _login_cookies(_web_app(monkeypatch), **{"X-Forwarded-Proto": "https"})
    assert all("Secure" not in v for v in got), got


def test_开了HUB_TRUST_PROXY后按转发头给Secure(monkeypatch):
    """隧道到本进程是明文 http，不认转发头的话公网 https 门户上 cookie 永远没有 Secure。"""
    got = _login_cookies(_web_app(monkeypatch, "1"), **{"X-Forwarded-Proto": "https"})
    assert all("Secure" in v for v in got), got
    assert all("HttpOnly" in v and "SameSite=Lax" in v for v in got), got


def test_开了开关但本机走http时仍不设Secure(monkeypatch):
    """本机 http 访问下设了 Secure 浏览器根本不存 cookie，用户会永远登不上。"""
    assert all("Secure" not in v for v in _login_cookies(_web_app(monkeypatch, "1"))), "误伤本机 http"


def test_转发头只认proto一项(monkeypatch):
    """X-Forwarded-For / Host 一并认了只是白送伪造面 —— 我们不按 IP 判权。"""
    src = (Path(A.__file__).parent / "web.py").read_text(encoding="utf-8")
    assert "x_for=0" in src and "x_host=0" in src, "ProxyFix 认了 IP/Host 转发头"
