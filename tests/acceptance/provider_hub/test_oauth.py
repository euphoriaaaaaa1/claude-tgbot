# -*- coding: utf-8 -*-
"""§4 OAuth 订阅账户（BRIEF S3 的可完成引导路径）。

修订 3 已补齐桩的 auth-files 三端点（§10），因此"已登录账户的脱敏显示 / 删除成功 /
账户级 activate"三条路径改为硬用例，不再是未覆盖项。
"""
import pytest


def test_start_codex返回授权链接与回调端口(api):
    code, body = api.post("/hub/api/oauth/start", {"provider": "codex"})
    assert code == 200, body
    assert body["url"].startswith("http")
    assert body["state"]
    assert body["callback_port"] == 1455
    assert body["expires_in"] == 300


def test_start_antigravity的回调端口是51121(api):
    code, body = api.post("/hub/api/oauth/start", {"provider": "antigravity"})
    assert code == 200, body
    assert body["callback_port"] == 51121


@pytest.mark.parametrize("provider", ["anthropic", "claude", "", None, "CODEX", "gemini"])
def test_非法provider返回400_bad_provider(api, provider):
    """anthropic 被显式移除（§4 开头）：Claude Pro/Max 走原生登录严格更优。"""
    code, body = api.post("/hub/api/oauth/start", {"provider": provider})
    assert code == 400, "provider=%r 被接受了" % provider
    assert body["error"] == "bad_provider"


def test_start时cliproxy不可达返回503(api, stub):
    stub.set_down(True)
    code, body = api.post("/hub/api/oauth/start", {"provider": "codex"})
    assert code == 503
    assert body["error"] == "cliproxy_down"


def test_start时cliproxy管理API报错返回502(api, stub):
    stub.mgmt_status = 500
    code, body = api.post("/hub/api/oauth/start", {"provider": "codex"})
    assert code == 502
    assert body["error"] == "cliproxy_reject"


def test_submit_粘贴整串回调URL可提交(api):
    """§4.7 引导的主路径：用户把浏览器地址栏整串 URL 复制回来。"""
    st = api.post("/hub/api/oauth/start", {"provider": "codex"})[1]["state"]
    code, body = api.post("/hub/api/oauth/submit", {
        "provider": "codex",
        "redirect_url": "http://localhost:1455/callback?code=abc123&state=%s" % st})
    assert code == 200, body
    assert body == {"ok": True}


def test_submit_code与state两字段形式也可提交(api):
    st = api.post("/hub/api/oauth/start", {"provider": "codex"})[1]["state"]
    code, body = api.post("/hub/api/oauth/submit",
                          {"provider": "codex", "code": "abc123", "state": st})
    assert code == 200, body


def test_submit_两种形式都缺返回400_bad_body(api):
    code, body = api.post("/hub/api/oauth/submit", {"provider": "codex"})
    assert code == 400
    assert body["error"] == "bad_body"


@pytest.mark.parametrize("url", ["localhost:1455/callback?code=x", "", "not a url",
                                 "javascript:x", "/callback?code=x"])
def test_submit_回调URL不是绝对http_url返回400(api, url):
    code, body = api.post("/hub/api/oauth/submit",
                          {"provider": "codex", "redirect_url": url})
    assert code == 400, "redirect_url=%r 被接受了" % url
    assert body["error"] == "bad_redirect_url"


def test_submit_provider非法返回400_bad_provider(api):
    code, body = api.post("/hub/api/oauth/submit", {
        "provider": "anthropic", "redirect_url": "http://localhost:1455/callback?code=x"})
    assert code == 400
    assert body["error"] == "bad_provider"


def test_submit_cliproxy不可达返回503(api, stub):
    stub.set_down(True)
    code, body = api.post("/hub/api/oauth/submit", {
        "provider": "codex", "redirect_url": "http://localhost:1455/callback?code=x"})
    assert code == 503
    assert body["error"] == "cliproxy_down"


@pytest.mark.parametrize("s", ["ok", "wait", "error"])
def test_status三态如实返回(api, stub, s):
    st = api.post("/hub/api/oauth/start", {"provider": "codex"})[1]["state"]
    stub.auth_status = s
    code, body = api.get("/hub/api/oauth/status?state=%s" % st)
    assert code == 200, body
    assert body["status"] == s


def test_status缺state参数返回400(api):
    code, body = api.get("/hub/api/oauth/status")
    assert code == 400
    assert body["error"] == "bad_body"


def test_status未知state返回200的error态而不是404(api):
    """§4.4 [R4D] I1-9：它是流程状态不是资源。"""
    code, body = api.get("/hub/api/oauth/status?state=never-issued-state")
    assert code == 200, "未知 state 回了 %s，页面轮询会被打断" % code
    assert body["status"] == "error"
    assert body.get("detail")


def test_status时cliproxy不可达返回503(api, stub):
    stub.set_down(True)
    code, body = api.get("/hub/api/oauth/status?state=whatever")
    assert code == 503
    assert body["error"] == "cliproxy_down"


def test_账户列表在cliproxy挂掉时仍200(api, stub):
    """§4.1：页面要能开。"""
    stub.set_down(True)
    code, body = api.get("/hub/api/oauth/accounts")
    assert code == 200
    assert body["accounts"] == []
    assert "cliproxy_down" in body.get("warnings", [])


def test_账户列表正常时返回accounts数组(api):
    code, body = api.get("/hub/api/oauth/accounts")
    assert code == 200
    assert isinstance(body["accounts"], list)


def test_删除不存在的账户返回404(api):
    code, body = api.delete("/hub/api/oauth/accounts/codex/deadbeef")
    assert code == 404
    assert body["error"] == "not_found"


def test_激活不存在的账户返回404(api):
    code, body = api.post("/hub/api/oauth/accounts/codex/deadbeef/activate")
    assert code == 404
    assert body["error"] == "not_found"


def test_账户URL里不出现明文邮箱(api):
    """§4.5 [SEC] 2.1 坑1：email_hash = sha1(email)[:8]，邮箱不进 URL。"""
    code, body = api.get("/hub/api/oauth/accounts")
    assert code == 200, body
    for acc in body.get("accounts", []):
        assert "@" not in str(acc.get("email_hash", "")), "URL 标识里出现了明文邮箱"
        assert acc["email_masked"].count("@") == 1 and "****" in acc["email_masked"]


# ---------------- §4.1 修订 3：auth-files 铺好数据后的账户列表 ----------------

def test_账户列表按auth_files如实产出五个字段(api, stub):
    stub.seed_auth_file(provider="codex", email="alice@example.com")
    code, body = api.get("/hub/api/oauth/accounts")
    assert code == 200, body
    acc = body["accounts"][0]
    assert acc["provider"] == "codex"
    assert acc["email_masked"] == "a****@example.com", "本地部分只留首字符"
    assert acc["email_hash"] == stub.email_hash("alice@example.com")
    assert acc["status"] == "active"
    assert acc["alias"] == "claude-sonnet-4-5-20250929"
    assert acc["activatable"] is True


def test_被禁用的账户status是disabled(api, stub):
    stub.seed_auth_file(email="bob@example.com", disabled=True)
    code, body = api.get("/hub/api/oauth/accounts")
    assert body["accounts"][0]["status"] == "disabled"


def test_空列表是正常态_warnings是空数组不是缺省(api):
    """§4.1：据此区分"没登过账户"与"读不出来"。"""
    code, body = api.get("/hub/api/oauth/accounts")
    assert code == 200
    assert body["accounts"] == []
    assert body["warnings"] == [], "warnings 必须是空数组，不能缺省"


def test_命令行登过的其他渠道原样列出但不给切换入口(api, stub):
    """§10：不隐藏，隐藏会让用户以为没登过而重复授权。"""
    stub.seed_auth_file(provider="anthropic", email="carol@example.com")
    code, body = api.get("/hub/api/oauth/accounts")
    acc = body["accounts"][0]
    assert acc["provider"] == "anthropic"
    assert acc["activatable"] is False


def test_账户列表不回显完整邮箱(api, stub):
    """S8 同源反向用例：邮箱是 PII，只能出现打码形态。"""
    stub.seed_auth_file(email="alice@example.com")
    code, body = api.get("/hub/api/oauth/accounts")
    assert code == 200 and len(body["accounts"]) == 1, "前置条件不成立，断言会空转：%s" % body
    assert "alice@example.com" not in str(body)


def test_账户列表在管理API非2xx时200并给cliproxy_error(api, stub):
    """§4.1 / §0.4：列表端点恒 200，故障用 warnings 表达。"""
    stub.mgmt_status = 500
    code, body = api.get("/hub/api/oauth/accounts")
    assert code == 200
    assert body["accounts"] == []
    assert "cliproxy_error" in body["warnings"]


# ---------------- §4.5 删除 / §4.6 账户级切换 ----------------

def test_删除已登录账户成功(api, stub):
    stub.seed_auth_file(email="alice@example.com")
    h = stub.email_hash("alice@example.com")
    code, body = api.delete("/hub/api/oauth/accounts/codex/%s" % h)
    assert code == 200, body
    assert body == {"ok": True}
    assert api.get("/hub/api/oauth/accounts")[1]["accounts"] == [], "桩里没真删掉"


def test_删除当前生效的账户返回409(api, stub, settings_path):
    from conftest import write_settings
    alias = "claude-sonnet-4-5-20250929"
    stub.seed_auth_file(email="alice@example.com", alias=alias)
    stub.seed_openai_block(alias=alias)
    write_settings(settings_path, {"env": {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % stub.port, "ANTHROPIC_MODEL": alias}})
    code, body = api.delete("/hub/api/oauth/accounts/codex/%s"
                            % stub.email_hash("alice@example.com"))
    assert code == 409
    assert body["error"] == "delete_active"


def test_删除账户时cliproxy不可达返回503(api, stub):
    stub.seed_auth_file(email="alice@example.com")
    h = stub.email_hash("alice@example.com")
    stub.set_down(True)
    code, body = api.delete("/hub/api/oauth/accounts/codex/%s" % h)
    assert code == 503
    assert body["error"] == "cliproxy_down"


def test_切换到OAuth账户按cliproxy形态写settings(api, stub, settings_path):
    """§4.6：语义与错误契约完全等同 §3.6，走 cliproxy 路由形态。"""
    from conftest import read_settings, FAKE_DOWNSTREAM_KEY
    alias = "claude-sonnet-4-5-20250929"
    stub.seed_auth_file(email="alice@example.com", alias=alias)
    code, body = api.post("/hub/api/oauth/accounts/codex/%s/activate"
                          % stub.email_hash("alice@example.com"))
    assert code == 200, body
    env = read_settings(settings_path)["env"]
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:%d" % stub.port
    assert env["ANTHROPIC_AUTH_TOKEN"] == FAKE_DOWNSTREAM_KEY
    assert env["ANTHROPIC_MODEL"] == alias
    assert "ANTHROPIC_API_KEY" not in env


def test_切换OAuth账户是幂等的(api, stub, settings_path):
    stub.seed_auth_file(email="alice@example.com")
    path = "/hub/api/oauth/accounts/codex/%s/activate" % stub.email_hash("alice@example.com")
    assert api.post(path)[0] == 200
    first = settings_path.read_text(encoding="utf-8")
    assert api.post(path)[0] == 200
    assert settings_path.read_text(encoding="utf-8") == first
