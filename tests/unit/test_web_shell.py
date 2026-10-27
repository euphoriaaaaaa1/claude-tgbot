# -*- coding: utf-8 -*-
"""T06 8767 壳单测：登录门、限速、CSRF、next 白名单、安全头、启动参数。

只起 Flask test_client 与一个临时端口的真实进程；不碰生产 8767、不读真实
``~/.claude/secrets/``、不启动 Telegram/dsh。
"""
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from moments import panel_auth  # noqa: E402
from moments import web  # noqa: E402

PASSWORD = "test-panel-pw-12345"
HTML = {"Accept": "text/html,application/xhtml+xml"}


@pytest.fixture(autouse=True)
def _clean_failures():
    panel_auth._fail.clear()
    yield
    panel_auth._fail.clear()


@pytest.fixture
def secrets(tmp_path, monkeypatch):
    """把口令/会话文件钉在 tmp 下，绝不读真实 ~/.claude/secrets。"""
    monkeypatch.setattr(panel_auth, "SECRET_PATH",
                        str(tmp_path / "secrets" / "moments-panel.env"))
    monkeypatch.setattr(panel_auth, "SESSIONS_PATH",
                        str(tmp_path / "secrets" / "moments-panel-sessions.json"))
    return tmp_path


@pytest.fixture
def client(secrets):
    panel_auth.set_password(PASSWORD)
    web.app.config["TESTING"] = True
    return web.app.test_client()


def _login(c, nxt="/", password=PASSWORD):
    return c.post("/login", data={"password": password, "next": nxt})


def _login_ok(c, nxt="/"):
    r = _login(c, nxt)
    assert r.status_code == 302, r.get_data(as_text=True)
    return r


# ── 未配置口令 / 未登录 ────────────────────────────────────────────────────

def test_未配置口令时登录页503(secrets):
    c = web.app.test_client()
    r = c.get("/login")
    assert r.status_code == 503
    assert "panel_auth --set-password" in r.get_data(as_text=True)


def test_未登录页面302到login并带next(client):
    r = client.get("/console", headers=HTML)
    assert r.status_code == 302
    assert r.headers["Location"] == "/login?next=/console"


def test_未登录api返回401_json(client):
    r = client.get("/api/console/overview")
    assert r.status_code == 401
    assert r.get_json() == {"error": "unauthorized"}


def test_未登录hub_api返回401且带HubGate(client):
    r = client.get("/hub/api/provider")
    assert r.status_code == 401
    assert r.get_json() == {"error": "unauthorized"}
    assert r.headers.get("Hub-Gate") == "1"


# ── 登录 / 登出 ────────────────────────────────────────────────────────────

def test_登录成功302并设置30天cookie(client):
    r = _login_ok(client, "/provider")
    assert r.headers["Location"] == "/provider"
    cookie = r.headers.get("Set-Cookie", "")
    assert cookie.startswith("cb_provider=")
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie
    assert "Max-Age=2592000" in cookie
    assert "Path=/" in cookie
    assert r.headers.get("Cache-Control") == "no-store"


def test_登录后可打开provider_登出清cookie(client):
    _login_ok(client)
    r = client.get("/provider")
    assert r.status_code == 200
    assert "8767" in r.get_data(as_text=True)

    r = client.post("/logout")
    assert r.status_code == 302
    assert r.headers["Location"] == "/login"
    assert "cb_provider=;" in r.headers.get("Set-Cookie", "")
    assert client.get("/provider").status_code == 302


def test_登录后未知路径404(client):
    _login_ok(client)
    assert client.get("/definitely-not-a-route").status_code == 404


def test_登录后logout_all撤销会话(client):
    _login_ok(client)
    r = client.post("/api/panel/logout_all", json={})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert r.get_json()["revoked"] >= 1
    assert client.get("/provider").status_code == 302


# ── 限速 ───────────────────────────────────────────────────────────────────

def test_连续失败五次后429并带RetryAfter(client):
    for i in range(5):
        r = _login(client, "/", password="wrong-%d" % i)
        assert r.status_code == 401
    r = _login(client, "/", password="wrong-again")
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1


# ── next 白名单 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    "//evil.com",
    "///evil.com",
    "/\\evil.com",
    "https://evil.com",
    "http://evil.com/x",
    "/%09/evil.com",
    "/%2F%2Fevil.com",
    "/%5Cevil.com",
    "/\t/evil.com",
    "/\r\n/evil.com",
    "javascript:alert(1)",
    "",
])
def test_next_绕过变体一律回落根(client, bad):
    r = _login_ok(client, bad)
    assert r.headers["Location"] == "/"


def test_next_站内路径保留(client):
    r = _login_ok(client, "/console?b=x")
    assert r.headers["Location"] == "/console?b=x"


def test_登录页回显的next也经过白名单(client):
    r = client.get("/login?next=//evil.com")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'value="//evil.com"' not in body
    assert 'value="/"' in body


# ── CSRF / Content-Type ────────────────────────────────────────────────────

def test_跨源写请求403(client):
    _login_ok(client)
    r = client.post("/api/panel/logout_all", json={},
                    headers={"Origin": "http://evil.com"})
    assert r.status_code == 403
    assert r.get_json()["error"] == "cross-site request rejected"


def test_同源非json写请求415(client):
    _login_ok(client)
    r = client.post("/api/panel/logout_all", data="{}",
                    content_type="text/plain",
                    headers={"Origin": "http://localhost"})
    assert r.status_code == 415
    assert "application/json" in r.get_json()["error"]


def test_同源json写请求放行(client):
    _login_ok(client)
    r = client.post("/api/panel/logout_all", json={},
                    headers={"Origin": "http://localhost"})
    assert r.status_code == 200


# ── 安全头 ─────────────────────────────────────────────────────────────────

def test_登录页与api响应带CSP和XFO(client):
    for resp in (client.get("/login"), client.get("/api/console/overview")):
        assert resp.headers.get("Content-Security-Policy") == "frame-ancestors 'none'"
        assert resp.headers.get("X-Frame-Options") == "DENY"


def test_登录后页面同样带安全头(client):
    _login_ok(client)
    r = client.get("/provider")
    assert r.headers.get("Content-Security-Policy") == "frame-ancestors 'none'"
    assert r.headers.get("X-Frame-Options") == "DENY"


# ── 启动参数 / 默认端口 ────────────────────────────────────────────────────

def test_默认端口8767且模式默认provider():
    assert web._listen_config({}) == ("127.0.0.1", 8767)
    assert web._listen_config({"MOMENTS_WEB_MODE": "provider"}) == ("127.0.0.1", 8767)
    assert web._listen_config({"MOMENTS_WEB_PORT": "08767"}) == ("127.0.0.1", 8767)


@pytest.mark.parametrize("env,code", [
    ({"MOMENTS_WEB_MODE": "legacy"}, 1),
    ({"MOMENTS_WEB_HOST": "0.0.0.0"}, 2),
    ({"MOMENTS_WEB_HOST": "localhost"}, 2),
    ({"MOMENTS_WEB_PORT": "abc"}, 2),
    ({"MOMENTS_WEB_PORT": "0"}, 2),
    ({"MOMENTS_WEB_PORT": "70000"}, 2),
    ({"MOMENTS_WEB_PORT": "8767 "}, 2),
])
def test_非法启动参数退出码(env, code):
    with pytest.raises(web._StartupError) as exc:
        web._listen_config(env)
    assert exc.value.code == code


def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _assert_starts_on_temp_port(tmp_path):
    port = _free_port()
    assert port != 8767, "测试不得占用生产 8767"
    home = tmp_path / "home"
    home.mkdir()
    env = os.environ.copy()
    env.update(HOME=str(home), MOMENTS_WEB_MODE="provider",
               MOMENTS_WEB_PORT=str(port))
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "moments.web"],
        cwd=str(_ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        assert proc.poll() is None, proc.stderr.read().decode("utf-8", "replace")
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/login" % port, timeout=5)
            raise AssertionError("未配置口令的 /login 应返回 503")
        except urllib.error.HTTPError as e:
            assert e.code == 503
    finally:
        proc.terminate()
        try:
            _out, err = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            _out, err = proc.communicate()
    assert ("[moments.web] startup mode=provider port=%d" % port) in \
        err.decode("utf-8", "replace")


def test_临时端口可启动且不碰生产8767(tmp_path):
    _assert_starts_on_temp_port(tmp_path)


@pytest.mark.skipif(sys.platform != "win32",
                    reason="Windows 启动已在 T02 VM 验证；macOS 上跳过")
def test_windows_临时端口启动冒烟(tmp_path):
    _assert_starts_on_temp_port(tmp_path)


# ── 依赖边界 ───────────────────────────────────────────────────────────────

def test_壳不依赖旧Hub模块():
    source = Path(web.__file__).read_text(encoding="utf-8")
    assert "hub_routes" not in source
    assert "hub_auth" not in source
    # T11 起 styles 蓝图是本壳的合法依赖（画风页与样式管理端点），不再算旧 Hub 模块。
    assert "moments.hub_routes" not in sys.modules
    assert "moments.hub_config" not in sys.modules
