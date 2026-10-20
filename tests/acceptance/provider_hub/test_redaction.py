# -*- coding: utf-8 -*-
"""§0.2 脱敏契约 / BRIEF S8「全程日志与页面回显不出现任何 key 明文片段」。

用的是**无前缀 40 位随机串**（智谱 / Azure / 自建网关那类），正则黑名单挡不住它，
只有第一层的字段白名单投影能挡住 —— 这正是 [R4D] F2 要求的两条用例。
"""
import json

from conftest import provider_payload, _Api

RANDOM_KEY = "a3f9c2e1b7d4508a6c1f2e9d7b3a4c5e6f8091a2"   # 40 位，无任何已知前缀
ALIAS = "claude-sonnet-4-5-20250929"


def test_用例1_列表接口不带出上游明文key_响应与日志都不含(api, stub, capfd):
    stub.seed_openai_block(api_key=RANDOM_KEY)
    code, body = api.get("/hub/api/provider")
    assert code == 200
    assert RANDOM_KEY not in json.dumps(body, ensure_ascii=False), "响应正文含明文 key"
    out, err = capfd.readouterr()
    assert RANDOM_KEY not in out and RANDOM_KEY not in err, "进程 stdout/stderr 含明文 key"


def test_用例2_自测遇到回显请求头的上游4xx时不带出明文(make_client, stub, capfd):
    a = _Api(make_client()[0])
    code, p = a.post("/hub/api/provider", provider_payload(api_key=RANDOM_KEY))
    assert code == 201, p
    a.post("/hub/api/provider/%s/activate" % p["id"])
    stub.messages_mode = "http4xx_echo"
    code, body = a.post("/hub/api/provider/%s/test" % p["id"])
    assert code == 200, body
    assert RANDOM_KEY not in json.dumps(body, ensure_ascii=False)
    out, err = capfd.readouterr()
    assert RANDOM_KEY not in out and RANDOM_KEY not in err


def test_白名单投影_桩里的额外字段不会漏出来(api, stub):
    """§0.2 第一层：未列入白名单的字段一律丢弃，不带出 cliproxy_client。"""
    blk = stub.seed_openai_block()
    blk["secret-key"] = "top-secret-value-9999"
    blk["internal-note"] = "cliproxy 内部字段"
    code, body = api.get("/hub/api/provider")
    assert code == 200, body
    text = json.dumps(body, ensure_ascii=False)
    assert "top-secret-value-9999" not in text
    assert "cliproxy 内部字段" not in text


def test_凭据类字段名一律以四星加末4位出现(api, stub):
    stub.seed_openai_block(api_key="sk-test-abcdefghijkl9x7Q")
    code, body = api.get("/hub/api/provider")
    for p in body["providers"]:
        assert p["key_masked"].startswith("****")
        assert p["key_masked"].endswith("9x7Q"), "掩码没保留末 4 位：%s" % p["key_masked"]
        assert len(p["key_masked"]) <= 12


def test_新增provider时提交的明文key不回显也不进日志(api, capfd):
    plain = "sk-test-" + "z" * 32
    code, body = api.post("/hub/api/provider", provider_payload(api_key=plain))
    assert code == 201
    assert plain not in json.dumps(body, ensure_ascii=False)
    out, err = capfd.readouterr()
    assert plain not in out and plain not in err


def test_cliproxy管理密钥不出现在任何错误响应里(api, stub):
    stub.mgmt_status = 500
    for path, method in (("/hub/api/provider", "post"), ("/hub/api/oauth/start", "post")):
        code, body = getattr(api, method)(path, provider_payload())
        assert code in (400, 500, 502, 503), "%s 实得 %s" % (path, code)
        assert stub.mgmt_key not in json.dumps(body, ensure_ascii=False)


def test_登录日志不含口令(make_client, capfd):
    """§1.5 日志约定：形如 hub_login ok=false n=3，不含口令。"""
    from conftest import login
    pw = "hub-access-pw-12345"
    c, _ = make_client(HUB_ACCESS_PASSWORD=pw)
    assert login(c, "wrong-password-xxxx").status_code == 401
    assert login(c, pw).status_code == 303
    out, err = capfd.readouterr()
    assert pw not in out and pw not in err
    assert "wrong-password-xxxx" not in out and "wrong-password-xxxx" not in err


def test_脱敏只挂在hub_api上_老接口不被改写(make_client, stub):
    """§0.2b 裁决：新老边界就是路径前缀，无例外、无灰区。

    正向：`/hub/api/*` 的响应里凭据必须是掩码形态；
    反向：老接口 `/api/*` 的响应不经过新增中间件（此处以"形状未被动过"为可观测证据）。
    """
    stub.seed_openai_block(api_key=RANDOM_KEY)
    a = _Api(make_client()[0])
    code, hub_body = a.get("/hub/api/provider")
    assert code == 200, hub_body
    assert RANDOM_KEY not in json.dumps(hub_body, ensure_ascii=False)
    code, old_body = a.get("/api/styles")
    assert code == 200, "老接口被新中间件打挂了"
    assert "_raw_text" not in old_body, "老接口的 JSON 被改写成了别的形状"
