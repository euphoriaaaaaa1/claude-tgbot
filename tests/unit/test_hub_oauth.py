# -*- coding: utf-8 -*-
"""§4 OAuth 账户生命周期的段级单测（M5 · hub_routes 第 3 段）。

不起 moments.web（那会拖进 db / config_loader / 鉴权门，是 M0 与验收的地盘），
只把蓝图挂到裸 Flask app；cliproxy 一律用验收目录里按 §10 冻结的桩（**只读复用**）。
绝不联网、绝不打真实 8317、绝不碰真实 ~/.claude/settings.json
（conftest 的 autouse 已把 CLAUDE_SETTINGS_PATH 钉在 tmp_path）。
"""
import json
import os
import sys
from pathlib import Path

import pytest
from flask import Flask

_ROOT = Path(__file__).resolve().parents[2]
_STUB_DIR = _ROOT / "tests" / "acceptance" / "provider_hub"
# 只 append 不 insert(0)：验收目录里也有个 conftest.py，插到最前会让同目录其它单测的
# `from conftest import ...` 解析到验收那份（unit 与 acceptance 合跑时炸在收集期）。
if str(_STUB_DIR) not in sys.path:
    sys.path.append(str(_STUB_DIR))

from stub_cliproxy import StubCliproxy            # noqa: E402
from moments import hub_routes                    # noqa: E402

ALIAS = "claude-sonnet-4-5-20250929"
HAIKU = "claude-3-5-haiku-20241022"
MGMT_KEY = "sk-test-mgmt-key-0000"
DOWN_KEY = "sk-test-downstream-0000"
EMAIL = "alice@example.com"
CB = "http://localhost:1455/callback?code=abc123"


class _Api:
    def __init__(self, client):
        self.c = client

    def _wrap(self, r):
        return r.status_code, (r.get_json(silent=True) or {})

    def get(self, path):
        return self._wrap(self.c.get(path))

    def post(self, path, payload=None):
        return self._wrap(self.c.post(path, json=payload if payload is not None else {}))

    def delete(self, path):
        return self._wrap(self.c.delete(path))


@pytest.fixture(autouse=True)
def _clean_sessions():
    """state 登记表是模块级的：用例之间必须互不串（否则"未知 state"用例会假绿）。"""
    hub_routes._OAUTH_SESSIONS.clear()
    yield
    hub_routes._OAUTH_SESSIONS.clear()


@pytest.fixture
def stub():
    s = StubCliproxy(mgmt_key=MGMT_KEY, downstream_key=DOWN_KEY)
    s.start()
    try:
        yield s
    finally:
        s.stop()


@pytest.fixture
def env(stub, monkeypatch, tmp_path):
    monkeypatch.setenv("CLIPROXY_PORT", str(stub.port))
    monkeypatch.setenv("CLIPROXY_MGMT_KEY", MGMT_KEY)
    monkeypatch.setenv("CLIPROXY_API_KEY", DOWN_KEY)
    # §0.3 回落：指向一个不存在的文件，绝不读到机主真实的 ~/.claude-tgbot/hub.env
    monkeypatch.setenv("HUB_ENV_FILE", str(tmp_path / "nope.env"))
    return stub


def _client():
    app = Flask(__name__)
    app.register_blueprint(hub_routes.hub_bp)
    app.config["TESTING"] = True
    a = _Api(app.test_client())
    a.app = app                 # 并发用例要另开 test_client
    return a


@pytest.fixture
def api(env):
    return _client()


@pytest.fixture
def unconfigured(monkeypatch):
    """三键全无 = §0.3 的"压根没配"。"""
    for k in ("CLIPROXY_PORT", "CLIPROXY_MGMT_KEY", "CLIPROXY_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HUB_ENV_FILE", "/nonexistent/hub.env")
    return _client()


@pytest.fixture
def settings_file():
    return Path(os.environ["CLAUDE_SETTINGS_PATH"])


def _start(api, provider="codex"):
    code, body = api.post("/hub/api/oauth/start", {"provider": provider})
    assert code == 200, body
    return body["state"]


# ---------------- §4.2 start ----------------

@pytest.mark.parametrize("provider,port", [("codex", 1455), ("antigravity", 51121)])
def test_start_两个渠道各自的url_state_回调端口(api, provider, port):
    code, body = api.post("/hub/api/oauth/start", {"provider": provider})
    assert code == 200, body
    assert body["url"].startswith("http")
    assert isinstance(body["state"], str) and body["state"]
    assert body["callback_port"] == port
    assert body["expires_in"] == hub_routes.OAUTH_WAIT_SECONDS == 300


def test_start_回调端口取自顶部那张表(api, monkeypatch):
    """§4.2 [RA] 建议2：端口只此一份，路由不许自己再写一遍。"""
    monkeypatch.setitem(hub_routes.OAUTH_CALLBACK_PORTS, "codex", 14550)
    assert api.post("/hub/api/oauth/start", {"provider": "codex"})[1]["callback_port"] == 14550


@pytest.mark.parametrize("provider", ["anthropic", "claude", "", None, "CODEX", "gemini",
                                      123, ["codex"]])
def test_start_枚举外的provider一律400(api, provider):
    """anthropic 被显式移除（§4 开头）：Claude Pro/Max 走原生登录严格更优。"""
    code, body = api.post("/hub/api/oauth/start", {"provider": provider})
    assert code == 400, "provider=%r 被接受了" % provider
    assert body["error"] == "bad_provider"


def test_start_没配cliproxy时503(unconfigured):
    code, body = unconfigured.post("/hub/api/oauth/start", {"provider": "codex"})
    assert (code, body["error"]) == (503, "cliproxy_unconfigured")


def test_start_cliproxy没跑时503(api, env):
    env.set_down(True)
    code, body = api.post("/hub/api/oauth/start", {"provider": "codex"})
    assert (code, body["error"]) == (503, "cliproxy_down")


def test_start_管理API非2xx时502且detail不含管理密钥(api, env):
    env.mgmt_status = 500
    code, body = api.post("/hub/api/oauth/start", {"provider": "codex"})
    assert (code, body["error"]) == (502, "cliproxy_reject")
    assert "500" in body["detail"] and MGMT_KEY not in json.dumps(body)


def test_start_授权链接不是http时按502拒收(api, env, monkeypatch):
    """本机 cliproxy 被换掉时的兜底：这个 url 会被页面渲染成可点链接。"""
    from stub_cliproxy import _H
    monkeypatch.setattr(_H, "_h_v0_management_codex_auth_url",
                        lambda self, b, q: self._send(200, {"status": "ok",
                                                            "url": "javascript:alert(1)",
                                                            "state": "s"}))
    code, body = api.post("/hub/api/oauth/start", {"provider": "codex"})
    assert (code, body["error"]) == (502, "cliproxy_reject")


# ---------------- §4.3 submit ----------------

def test_submit_整串回调URL提交并把code送给cliproxy(api, env):
    st = _start(api)
    code, body = api.post("/hub/api/oauth/submit",
                          {"provider": "codex", "redirect_url": CB + "&state=" + st})
    assert (code, body) == (200, {"ok": True})
    sent = env.paths_hit("oauth-callback")
    assert sent, "没把回调提交给 cliproxy"
    assert json.loads(sent[-1]["body"])["code"] == "abc123"


def test_submit_code与state两字段形式(api, env):
    st = _start(api)
    code, body = api.post("/hub/api/oauth/submit",
                          {"provider": "codex", "code": "abc123", "state": st})
    assert (code, body) == (200, {"ok": True})


def test_submit_两种形式都缺400_bad_body(api):
    code, body = api.post("/hub/api/oauth/submit", {"provider": "codex"})
    assert (code, body["error"]) == (400, "bad_body")


def test_submit_code形式缺state400_bad_body(api):
    code, body = api.post("/hub/api/oauth/submit", {"provider": "codex", "code": "abc"})
    assert (code, body["error"]) == (400, "bad_body")


@pytest.mark.parametrize("url", ["localhost:1455/callback?code=x", "", "not a url",
                                 "javascript:x", "/callback?code=x", "http:///cb?code=x",
                                 None, 42, "ftp://h/cb?code=x",
                                 "http://localhost:1455/callback"])
def test_submit_畸形或没有code的回调URL一律400(api, url):
    code, body = api.post("/hub/api/oauth/submit",
                          {"provider": "codex", "redirect_url": url})
    assert code == 400, "redirect_url=%r 被接受了" % url
    assert body["error"] == "bad_redirect_url"


def test_submit_provider非法400_bad_provider(api):
    code, body = api.post("/hub/api/oauth/submit",
                          {"provider": "anthropic", "redirect_url": CB})
    assert (code, body["error"]) == (400, "bad_provider")


def test_submit_没配时503(unconfigured):
    code, body = unconfigured.post("/hub/api/oauth/submit",
                                   {"provider": "codex", "redirect_url": CB})
    assert (code, body["error"]) == (503, "cliproxy_unconfigured")


def test_submit_cliproxy没跑时503(api, env):
    env.set_down(True)
    code, body = api.post("/hub/api/oauth/submit", {"provider": "codex", "redirect_url": CB})
    assert (code, body["error"]) == (503, "cliproxy_down")


def test_submit_管理API非2xx时502(api, env):
    env.mgmt_status = 502
    code, body = api.post("/hub/api/oauth/submit", {"provider": "codex", "redirect_url": CB})
    assert (code, body["error"]) == (502, "cliproxy_reject")


def test_submit_没带state的回调URL照样送给cliproxy(api, env):
    """各家回调不一定带 state：本端不许因此拦下来，那一层由 cliproxy 自己兜。"""
    code, body = api.post("/hub/api/oauth/submit", {"provider": "codex", "redirect_url": CB})
    assert (code, body) == (200, {"ok": True})
    assert env.paths_hit("oauth-callback")


def test_submit_没发过的state直接拦掉(api, env):
    code, body = api.post("/hub/api/oauth/submit",
                          {"provider": "codex", "redirect_url": CB + "&state=forged"})
    assert (code, body["error"]) == (409, "oauth_state_expired")
    assert not env.paths_hit("oauth-callback"), "没校验就把伪造 state 转发给了 cliproxy"


def test_submit_state属于另一个渠道也拦掉(api):
    st = _start(api, "codex")
    code, body = api.post("/hub/api/oauth/submit",
                          {"provider": "antigravity", "code": "x", "state": st})
    assert (code, body["error"]) == (409, "oauth_state_expired")


def test_submit_超过5分钟窗口的state按过期拦掉(api, env):
    """§4.4 的等待窗口就是 5 分钟：过点的会话必须让用户重开，不能闷头提交。"""
    st = _start(api)
    provider, issued = hub_routes._OAUTH_SESSIONS[st]
    hub_routes._OAUTH_SESSIONS[st] = (provider, issued - hub_routes.OAUTH_WAIT_SECONDS - 1)
    code, body = api.post("/hub/api/oauth/submit",
                          {"provider": "codex", "code": "abc", "state": st})
    assert (code, body["error"]) == (409, "oauth_state_expired")
    assert body["detail"] and not env.paths_hit("oauth-callback")


def test_submit_窗口内的state正常放行(api):
    st = _start(api)
    provider, issued = hub_routes._OAUTH_SESSIONS[st]
    hub_routes._OAUTH_SESSIONS[st] = (provider, issued - hub_routes.OAUTH_WAIT_SECONDS + 30)
    assert api.post("/hub/api/oauth/submit",
                    {"provider": "codex", "code": "abc", "state": st})[0] == 200


def test_过期会话不会无限堆在内存里(api):
    st = _start(api)
    provider, issued = hub_routes._OAUTH_SESSIONS[st]
    hub_routes._OAUTH_SESSIONS["stale"] = (provider, issued - 10 * 60)
    _start(api, "antigravity")          # 再发一次 = 顺手清一次过期的
    assert "stale" not in hub_routes._OAUTH_SESSIONS


# ---------------- §4.4 status ----------------

@pytest.mark.parametrize("s", ["ok", "wait", "error"])
def test_status三态如实透传且各带一句人读文案(api, env, s):
    st = _start(api)
    env.auth_status = s
    code, body = api.get("/hub/api/oauth/status?state=%s" % st)
    assert code == 200, body
    assert body["status"] == s
    assert body["detail"]


def test_status认不出的上游态当wait(api, env):
    """上游改字面量时宁可继续轮询（页面有 5 分钟倒计时兜底），也别误报"授权失败"。"""
    st = _start(api)
    env.auth_status = "pending"
    assert api.get("/hub/api/oauth/status?state=%s" % st)[1]["status"] == "wait"


@pytest.mark.parametrize("qs", ["", "?state=", "?state=%20"])
def test_status缺state返回400(api, qs):
    code, body = api.get("/hub/api/oauth/status" + qs)
    assert (code, body["error"]) == (400, "bad_body")


def test_status未知state是200的error态而不是404(api):
    """§4.4 [R4D] I1-9：它是流程状态不是资源，轮询不该被 404 打断。"""
    code, body = api.get("/hub/api/oauth/status?state=never-issued")
    assert code == 200, "未知 state 回了 %s，页面轮询会被打断" % code
    assert body["status"] == "error" and body["detail"]


def test_status过期的state也走同一个error态(api, env):
    st = _start(api)
    provider, issued = hub_routes._OAUTH_SESSIONS[st]
    hub_routes._OAUTH_SESSIONS[st] = (provider, issued - hub_routes.OAUTH_WAIT_SECONDS - 1)
    env.auth_status = "ok"
    code, body = api.get("/hub/api/oauth/status?state=%s" % st)
    assert (code, body["status"]) == (200, "error")


def test_status_cliproxy没跑时503而不是当成会话过期(api, env):
    """依赖挂了显示成"会话过期"，用户只会一遍遍重开授权 —— 必须分得开。"""
    env.set_down(True)
    code, body = api.get("/hub/api/oauth/status?state=whatever")
    assert (code, body["error"]) == (503, "cliproxy_down")


def test_status_没配时503(unconfigured):
    code, body = unconfigured.get("/hub/api/oauth/status?state=x")
    assert (code, body["error"]) == (503, "cliproxy_unconfigured")


def test_status带state去问cliproxy且做了转义(api, env):
    st = _start(api)
    api.get("/hub/api/oauth/status?state=%s" % st)
    hit = env.paths_hit("get-auth-status")
    assert hit and "state=" + st in hit[-1]["path"]


# ---------------- §4.1 accounts ----------------

def test_账户列表七个字段如实产出(api, env):
    env.seed_auth_file(provider="codex", email=EMAIL)
    code, body = api.get("/hub/api/oauth/accounts")
    assert code == 200, body
    assert body["accounts"] == [{"provider": "codex", "email_masked": "a****@example.com",
                                 "email_hash": env.email_hash(EMAIL), "status": "active",
                                 "alias": ALIAS, "activatable": True,
                                 "not_activatable_reason": None}]
    assert body["warnings"] == []


def test_空列表是正常态_warnings是空数组不是缺省(api):
    code, body = api.get("/hub/api/oauth/accounts")
    assert (code, body) == (200, {"accounts": [], "warnings": []})


def test_被禁用的账户status是disabled(api, env):
    env.seed_auth_file(email="bob@example.com", disabled=True)
    assert api.get("/hub/api/oauth/accounts")[1]["accounts"][0]["status"] == "disabled"


def test_命令行登过的其它渠道原样列出但不给切换入口(api, env):
    """§10：不隐藏，隐藏会让用户以为没登过而重复授权。"""
    env.seed_auth_file(provider="anthropic", email="carol@example.com")
    acc = api.get("/hub/api/oauth/accounts")[1]["accounts"][0]
    assert (acc["provider"], acc["activatable"]) == ("anthropic", False)
    assert acc["not_activatable_reason"] == "unsupported_provider"


def test_没登记别名的账户alias是None且不给切换入口(api, env):
    """§4.1 ㊲：activatable 必须含 activate 的前置检查，否则页面给一个点了必 409 的按钮。"""
    env.seed_auth_file(email=EMAIL, alias=None)
    acc = api.get("/hub/api/oauth/accounts")[1]["accounts"][0]
    assert acc["alias"] is None and acc["activatable"] is False
    assert acc["not_activatable_reason"] == "no_alias"


def test_列表只投影安全字段_令牌之类不出站(api, env):
    """§0.2 第一层：auth-file 里还躺着 OAuth 凭证，整条带出去就是把订阅送到浏览器。"""
    f = env.seed_auth_file(email=EMAIL)
    f["refresh_token"] = "sk-test-refresh-000000000000"
    f["access_token"] = "ya29.super-secret-token"
    code, body = api.get("/hub/api/oauth/accounts")
    dumped = json.dumps(body, ensure_ascii=False)
    assert "refresh" not in dumped and "ya29" not in dumped and "sk-test-refresh" not in dumped
    assert set(body["accounts"][0]) == {"provider", "email_masked", "email_hash", "status",
                                        "alias", "activatable", "not_activatable_reason"}


def test_完整邮箱既不出站也不进URL段(api, env):
    """§4.5 [SEC] 2.1 坑 1：email_hash = sha1(email)[:8]。"""
    env.seed_auth_file(email=EMAIL)
    body = api.get("/hub/api/oauth/accounts")[1]
    acc = body["accounts"][0]
    assert EMAIL not in json.dumps(body, ensure_ascii=False)
    assert "@" not in acc["email_hash"] and len(acc["email_hash"]) == 8
    assert acc["email_masked"].count("@") == 1 and "alice" not in acc["email_masked"]


@pytest.mark.parametrize("email,masked", [
    ("alice@example.com", "a****@example.com"), ("x@y.z", "x****@y.z"),
    ("", "****"), (None, "****"), ("noatsign", "n****")])
def test_邮箱打码只留首字符(email, masked):
    assert hub_routes._mask_email(email) == masked


def test_账户列表在cliproxy没跑时仍200(api, env):
    """§4.1 恒 200：页面必须能开。"""
    env.seed_auth_file(email=EMAIL)
    env.set_down(True)
    code, body = api.get("/hub/api/oauth/accounts")
    assert (code, body) == (200, {"accounts": [], "warnings": ["cliproxy_down"]})


def test_账户列表在管理API非2xx时200并给cliproxy_error(api, env):
    env.mgmt_status = 500
    code, body = api.get("/hub/api/oauth/accounts")
    assert (code, body) == (200, {"accounts": [], "warnings": ["cliproxy_error"]})


def test_账户列表在没配时200并给unconfigured(unconfigured):
    code, body = unconfigured.get("/hub/api/oauth/accounts")
    assert (code, body) == (200, {"accounts": [], "warnings": ["cliproxy_unconfigured"]})


def test_列表读不出来与没登过账户可区分(api, env):
    """§4.1：前者 warnings 非空，后者 warnings == []。"""
    empty = api.get("/hub/api/oauth/accounts")[1]
    env.mgmt_status = 503
    broken = api.get("/hub/api/oauth/accounts")[1]
    assert empty["accounts"] == broken["accounts"] == []
    assert empty["warnings"] == [] and broken["warnings"]


def test_上游给回不认识的形状时按空列表处理(api, env, monkeypatch):
    """M2 unwrap_list 的口径：认不出来宁可"看不见条目"，也不能把 dict 当条目往下传。"""
    from stub_cliproxy import _H
    monkeypatch.setattr(_H, "_h_v0_management_auth_files",
                        lambda self, b, q: self._send(200, {"status": "ok"}))
    code, body = api.get("/hub/api/oauth/accounts")
    assert (code, body["accounts"]) == (200, [])


# ---------------- §4.5 删除 ----------------

def test_删除已登录账户_桩里真的没了(api, env):
    env.seed_auth_file(email=EMAIL)
    code, body = api.delete("/hub/api/oauth/accounts/codex/%s" % env.email_hash(EMAIL))
    assert (code, body) == (200, {"ok": True})
    assert env.auth_files == [], "桩里没真删掉"


def test_删除不存在的账户404(api, env):
    env.seed_auth_file(email=EMAIL)
    for path in ("codex/deadbeef", "antigravity/%s" % env.email_hash(EMAIL)):
        code, body = api.delete("/hub/api/oauth/accounts/" + path)
        assert (code, body["error"]) == (404, "not_found"), path
    assert env.auth_files, "404 的路径不该动数据"


def test_删除时邮箱不进URL也不进请求路径(api, env):
    env.seed_auth_file(email=EMAIL)
    api.delete("/hub/api/oauth/accounts/codex/%s" % env.email_hash(EMAIL))
    hit = env.paths_hit("auth-files")
    assert hit and all(EMAIL not in r["path"] for r in hit)


def _write_settings(path, env_obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": env_obj}, ensure_ascii=False), encoding="utf-8")


def test_删除当前生效的账户409(api, env, settings_file):
    env.seed_auth_file(email=EMAIL, alias=ALIAS)
    _write_settings(settings_file, {"ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % env.port,
                                    "ANTHROPIC_AUTH_TOKEN": DOWN_KEY,
                                    "ANTHROPIC_MODEL": ALIAS})
    code, body = api.delete("/hub/api/oauth/accounts/codex/%s" % env.email_hash(EMAIL))
    assert (code, body["error"]) == (409, "delete_active")
    assert env.auth_files, "409 之后账户不该被删"


def test_settings指向别的模型时照常可删(api, env, settings_file):
    env.seed_auth_file(email=EMAIL, alias=ALIAS)
    _write_settings(settings_file, {"ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % env.port,
                                    "ANTHROPIC_MODEL": "别的-alias"})
    assert api.delete("/hub/api/oauth/accounts/codex/%s" % env.email_hash(EMAIL))[0] == 200


def test_settings走claude原生时照常可删(api, env, settings_file):
    env.seed_auth_file(email=EMAIL, alias=ALIAS)
    _write_settings(settings_file, {})
    assert api.delete("/hub/api/oauth/accounts/codex/%s" % env.email_hash(EMAIL))[0] == 200


def test_settings写坏了不挡删除(api, env, settings_file):
    """§4.5 没有 settings_unparsable 这一态：证明不了它 active 就照常放行。"""
    env.seed_auth_file(email=EMAIL, alias=ALIAS)
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text("{坏掉的 json", encoding="utf-8")
    assert api.delete("/hub/api/oauth/accounts/codex/%s" % env.email_hash(EMAIL))[0] == 200


def test_删除时cliproxy没跑503(api, env):
    env.seed_auth_file(email=EMAIL)
    h = env.email_hash(EMAIL)
    env.set_down(True)
    code, body = api.delete("/hub/api/oauth/accounts/codex/%s" % h)
    assert (code, body["error"]) == (503, "cliproxy_down")


def test_删除时管理API非2xx502(api, env):
    env.seed_auth_file(email=EMAIL)
    h = env.email_hash(EMAIL)
    env.mgmt_status = 500
    code, body = api.delete("/hub/api/oauth/accounts/codex/%s" % h)
    assert (code, body["error"]) == (502, "cliproxy_reject")


def test_删除时没配503(unconfigured):
    code, body = unconfigured.delete("/hub/api/oauth/accounts/codex/deadbeef")
    assert (code, body["error"]) == (503, "cliproxy_unconfigured")


# ---------------- §4.6 账户级切换 ----------------

def _activate(api, env, email=EMAIL, provider="codex"):
    return api.post("/hub/api/oauth/accounts/%s/%s/activate" % (provider, env.email_hash(email)))


def test_切换按cliproxy形态写settings(api, env, settings_file):
    """§4.6 = §3.6 形态一：三键写入 + 删 ANTHROPIC_API_KEY，BASE_URL 不带 /v1。"""
    env.seed_auth_file(email=EMAIL, alias=ALIAS)
    _write_settings(settings_file, {"ANTHROPIC_API_KEY": "sk-old", "别的键": "保留"})
    code, body = _activate(api, env)
    assert (code, body) == (200, {"ok": True, "active_kind": "cliproxy"})
    env_out = json.loads(settings_file.read_text(encoding="utf-8"))["env"]
    assert env_out["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:%d" % env.port
    assert env_out["ANTHROPIC_AUTH_TOKEN"] == DOWN_KEY
    assert env_out["ANTHROPIC_MODEL"] == ALIAS
    assert "ANTHROPIC_API_KEY" not in env_out and env_out["别的键"] == "保留"


def test_切换是幂等的_重跑一字节不差(api, env, settings_file):
    env.seed_auth_file(email=EMAIL, alias=ALIAS)
    assert _activate(api, env)[0] == 200
    first = settings_file.read_text(encoding="utf-8")
    assert _activate(api, env)[0] == 200
    assert settings_file.read_text(encoding="utf-8") == first


def test_settings文件不存在时新建而不是报错(api, env, settings_file):
    env.seed_auth_file(email=EMAIL, alias=ALIAS)
    assert not settings_file.exists()
    assert _activate(api, env)[0] == 200
    assert settings_file.exists()


def test_切换不存在的账户404且不写settings(api, env, settings_file):
    code, body = _activate(api, env)
    assert (code, body["error"]) == (404, "not_found")
    assert not settings_file.exists()


def test_没登记别名的账户切不过去(api, env, settings_file):
    """没 alias 就没东西可写进 ANTHROPIC_MODEL，早拦掉而不是写一个 null 进去。"""
    env.seed_auth_file(email=EMAIL, alias=None)
    code, body = _activate(api, env)
    assert (code, body["error"]) == (409, "oauth_no_alias")
    assert "model_aliases" in body["detail"], "detail 没指出去哪配，用户无从下手"
    assert not settings_file.exists()


def test_settings写坏了先409再动cliproxy(api, env, settings_file):
    """§3.6 A2 在 B 之前：A 类失败必须发生在动 cliproxy 之前，天然无残留。"""
    env.seed_auth_file(email=EMAIL, alias=ALIAS)
    env.seed_openai_block(alias=ALIAS)
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings_file.write_text("{坏掉的 json", encoding="utf-8")
    code, body = _activate(api, env)
    assert (code, body["error"]) == (409, "settings_unparsable")
    assert not env.openai_compat[0].get("prefix"), "A 类失败却已经动了 cliproxy"


def test_切换时cliproxy没跑503_不写settings(api, env, settings_file):
    env.seed_auth_file(email=EMAIL, alias=ALIAS)
    env.set_down(True)
    code, body = _activate(api, env)
    assert (code, body["error"]) == (503, "cliproxy_down")
    assert not settings_file.exists()


def test_切换时没配503(unconfigured):
    code, body = unconfigured.post("/hub/api/oauth/accounts/codex/deadbeef/activate")
    assert (code, body["error"]) == (503, "cliproxy_unconfigured")


def test_切换时同alias的第三方provider条目让位(api, env, settings_file):
    """§3.0d：落选条目 prefix = 它自己的 id，裸官方名才只命中 OAuth 账户那边。"""
    env.seed_auth_file(email=EMAIL, alias=ALIAS)
    env.seed_openai_block(alias=ALIAS, haiku_alias=HAIKU)
    env.seed_openai_block(name="无关的", base_url="https://x.example.com/v1",
                          alias="别的-alias", haiku_alias="别的-haiku")
    assert _activate(api, env)[0] == 200
    # 桩的 PATCH 是整条替换，seed 返回的 dict 已经不在表里了 —— 断言一律读实时的块
    assert env.openai_compat[0].get("prefix"), "同 alias 的第三方条目没让位，请求还打到它"
    assert not env.openai_compat[1].get("prefix"), "不相干的条目被动了"


def test_被禁用的账户切过去时会被启用(api, env):
    env.seed_auth_file(email=EMAIL, alias=ALIAS, disabled=True)
    assert _activate(api, env)[0] == 200
    assert env.auth_files[0]["disabled"] is False


def test_同渠道另一个账户不会被禁掉(api, env):
    """多账户共用一个 alias 是配额池不是冲突，禁掉等于把用户刚登的号白登了。"""
    env.seed_auth_file(email=EMAIL, alias=ALIAS)
    env.seed_auth_file(email="bob@example.com", alias=ALIAS)
    assert _activate(api, env)[0] == 200
    assert [f["disabled"] for f in env.auth_files] == [False, False]


def test_B阶段写到一半失败_补偿失败时回activate_partial(api, env, settings_file):
    """§3.6 三态契约：补偿本身失败 = 不一致态，500 activate_partial，settings 不动。"""
    env.seed_auth_file(email=EMAIL, alias=ALIAS)
    env.seed_openai_block(alias=ALIAS, haiku_alias=HAIKU)
    env.seed_openai_block(name="第二个", base_url="https://gw.example.com/v1",
                          alias=ALIAS, haiku_alias=HAIKU)
    env.patch_fail_after_n = 1          # 第 1 次让位成功、第 2 次起全 500（含补偿那次）
    code, body = _activate(api, env)
    assert (code, body["error"]) == (500, "activate_partial")
    assert not settings_file.exists(), "不一致态里 settings 不该被写"
    assert any(b.get("prefix") for b in env.openai_compat), "前置条件不成立：一次都没写进去"


def test_B阶段第一次写就失败_502且无残留(api, env, settings_file):
    env.seed_auth_file(email=EMAIL, alias=ALIAS)
    env.seed_openai_block(alias=ALIAS, haiku_alias=HAIKU)
    env.patch_fail_after_n = 0          # 一次写都不许成功
    code, body = _activate(api, env)
    assert (code, body["error"]) == (502, "cliproxy_reject")
    assert not env.openai_compat[0].get("prefix") and not settings_file.exists()


def test_补偿成功时把prefix原样还回去(api, env, settings_file, monkeypatch):
    """C 阶段失败（settings 写不下去）→ B 的改动必须还原，对外是 500 且无副作用。"""
    env.seed_auth_file(email=EMAIL, alias=ALIAS)
    env.seed_openai_block(alias=ALIAS, haiku_alias=HAIKU)
    monkeypatch.setattr(hub_routes.settings_io, "write_json_atomic",
                        lambda *a, **kw: (_ for _ in ()).throw(
                            hub_routes.HubError(500, "settings_write_failed", "写不下去")))
    code, body = _activate(api, env)
    assert (code, body["error"]) == (500, "settings_write_failed")
    assert not env.openai_compat[0].get("prefix"), "补偿没把 prefix 还回去"


def test_命令行登过的其它渠道不给切换(api, env, settings_file):
    """列表给的是 activatable:false，接口就不能背着页面把它切了。"""
    env.seed_auth_file(provider="anthropic", email=EMAIL, alias=ALIAS)
    code, body = _activate(api, env, provider="anthropic")
    assert (code, body["error"]) == (400, "bad_provider")
    assert not settings_file.exists()


def test_命令行登过的其它渠道照样能删(api, env):
    """看得见就删得掉 —— 不给切换不等于不给管理。"""
    env.seed_auth_file(provider="anthropic", email=EMAIL)
    code, _ = api.delete("/hub/api/oauth/accounts/anthropic/%s" % env.email_hash(EMAIL))
    assert (code, env.auth_files) == (200, [])


# ---------------- §0.1 通则：请求体必须是 JSON 对象 ----------------

@pytest.mark.parametrize("path", ["/hub/api/oauth/start", "/hub/api/oauth/submit"])
@pytest.mark.parametrize("payload", [["codex"], "codex", 42, True, 1.5])
def test_请求体不是对象一律400_bad_body(api, path, payload):
    """BUG-18：合法 JSON 但顶层不是对象，body.get 会抛 AttributeError → 500。"""
    code, body = api.post(path, payload)
    assert (code, body["error"]) == (400, "bad_body"), (path, payload, body)


@pytest.mark.parametrize("path", ["/hub/api/oauth/start", "/hub/api/oauth/submit"])
def test_空体与ContentType不对按体缺失处理(api, path):
    """§0.1：禁用 force=True → 按体缺失 → 各端点自己的必填校验去报更准的码。"""
    r = api.c.post(path, data="provider=codex",
                   content_type="application/x-www-form-urlencoded")
    assert r.status_code == 400
    assert r.get_json()["error"] in ("bad_body", "bad_provider")


def test_submit成功后state即作废_二次提交409(api, env):
    """§4.3：授权码是一次性凭据，不作废等于给门户装了个免费重放放大器。"""
    st = _start(api)
    assert api.post("/hub/api/oauth/submit",
                    {"provider": "codex", "code": "abc", "state": st})[0] == 200
    env.requests.clear()
    code, body = api.post("/hub/api/oauth/submit",
                          {"provider": "codex", "code": "abc", "state": st})
    assert (code, body["error"]) == (409, "oauth_state_expired")
    assert not env.paths_hit("oauth-callback"), "重放请求还是被转发出去了"


def test_submit失败时state保留_可修正后重试(api, env):
    """§4.3：作废只发生在 200 之后 —— 上游 502 后用户重试不该被当成过期。"""
    st = _start(api)
    env.mgmt_status = 502
    assert api.post("/hub/api/oauth/submit",
                    {"provider": "codex", "code": "abc", "state": st})[0] == 502
    env.mgmt_status = 200
    assert api.post("/hub/api/oauth/submit",
                    {"provider": "codex", "code": "abc", "state": st})[0] == 200


def test_没带state的提交不影响别的会话(api, env):
    st = _start(api)
    assert api.post("/hub/api/oauth/submit", {"provider": "codex", "redirect_url": CB})[0] == 200
    assert st in hub_routes._OAUTH_SESSIONS, "没带 state 的提交把别人的会话清了"


def test_列表里activatable为true的账户点了一定切得成(api, env, settings_file):
    """㊲ 的唯一意义：可点入口保证点了会成。拿列表结果直接驱动 activate。"""
    env.seed_auth_file(email=EMAIL, alias=ALIAS)
    env.seed_auth_file(provider="anthropic", email="carol@example.com")
    env.seed_auth_file(email="bob@example.com", alias=None)
    accounts = api.get("/hub/api/oauth/accounts")[1]["accounts"]
    ok = [a for a in accounts if a["activatable"]]
    assert len(ok) == 1 and ok[0]["email_hash"] == env.email_hash(EMAIL)
    for a in accounts:
        code, _b = api.post("/hub/api/oauth/accounts/%s/%s/activate"
                            % (a["provider"], a["email_hash"]))
        assert (code == 200) is a["activatable"], (a, code)


def test_并发重放同一state只有一个能成(api, env):
    """㊱ 的检查与消费必须原子：分两步做的话，10 个并发重放会一起穿过检查。"""
    import threading
    st = _start(api)
    codes = []

    def one(i):
        r = api.app.test_client().post("/hub/api/oauth/submit",
                                       json={"provider": "codex", "code": "c%d" % i,
                                             "state": st})
        codes.append(r.status_code)

    ts = [threading.Thread(target=one, args=(i,)) for i in range(10)]
    [t.start() for t in ts]
    [t.join(20) for t in ts]
    assert (codes.count(200), codes.count(409)) == (1, 9), codes
    assert len(env.paths_hit("oauth-callback")) == 1, "重放被转发给了 cliproxy"
