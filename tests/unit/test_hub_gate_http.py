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
    guard = c.application.extensions["hub_login_guard"]
    _login(c, PW, "wrong-admin")
    assert guard.record_failure() == 5, "错误 admin 口令重置了全站爆破窗口"


def test_两把全对才清窗(app_client):
    c = app_client(PW, ADMIN)
    _login(c, "bad-0")
    guard = c.application.extensions["hub_login_guard"]
    assert _login(c, PW, ADMIN).status_code == 303
    assert guard.record_failure() == 1
