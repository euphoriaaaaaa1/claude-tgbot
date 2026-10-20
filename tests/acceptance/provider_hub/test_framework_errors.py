# -*- coding: utf-8 -*-
"""§0.1 框架级错误必须与业务错误同形状（Flask 默认回 HTML，违反契约）。

三条断言逐条对应 INTERFACE 的断言 A / B / C。
"""
import pytest


def _is_json(resp):
    return "application/json" in (resp.headers.get("Content-Type") or "")


def test_断言A_不存在的api路径回JSON形状404(client):
    r = client.post("/hub/api/provider/xxx/nonexistent", json={})
    assert r.status_code == 404
    assert _is_json(r), "框架 404 回了 HTML，前端 fetch 解不出 error 码"
    assert r.get_json().get("error") == "not_found"


def test_断言B_只允许POST的路径用GET回JSON形状405(client):
    r = client.get("/hub/api/provider/abc12345/activate")
    assert r.status_code == 405
    assert _is_json(r)
    assert r.get_json().get("error") == "method_not_allowed"


def test_断言C_超大请求体回JSON形状413(client):
    """MAX_CONTENT_LENGTH 未在 INTERFACE 里给出具体值，用 32MB 覆盖任何合理阈值。"""
    big = {"label": "x" * (32 * 1024 * 1024)}
    r = client.post("/hub/api/provider", json=big)
    assert r.status_code == 413, "超大体未被 MAX_CONTENT_LENGTH 挡住（实得 %s）" % r.status_code
    assert _is_json(r)
    assert r.get_json().get("error") == "payload_too_large"


@pytest.mark.parametrize("path", ["/hub/api/provider", "/hub/api/bots",
                                  "/hub/api/oauth/accounts"])
def test_api路径的错误一律是JSON不是HTML(client, path):
    r = client.delete(path)
    assert r.status_code in (404, 405), "期望 404/405，实得 %s" % r.status_code
    assert _is_json(r)
    assert "error" in r.get_json()


def test_错误detail不带traceback也不带管理密钥(client, stub):
    """§7 末行 + §0.4：cliproxy_reject 的 detail 必须含上游状态码，但不许含密钥/堆栈。

    用**写**端点触发（列表端点按 §3.2 恒 200，不产生 detail）。
    """
    from conftest import provider_payload
    stub.mgmt_status = 500
    r = client.post("/hub/api/provider", json=provider_payload())
    assert r.status_code == 502, "实得 %s（§0.4：管理 API 非 2xx = 502）" % r.status_code
    assert _is_json(r)
    body = r.get_json()
    assert body["error"] == "cliproxy_reject"
    detail = str(body.get("detail", ""))
    assert "500" in detail
    assert "Traceback" not in detail and 'File "' not in detail
    assert stub.mgmt_key not in detail, "管理密钥出现在错误 detail 里"


# ---------------- ㊳（修订 9，BUG-18）：请求体不是 dict → 400 bad_body，三段通用 ----------------

DICT_BODY_ENDPOINTS = [
    ("post", "/hub/api/provider"),
    ("post", "/hub/api/oauth/start"),
    ("post", "/hub/api/oauth/submit"),
]

NON_DICT_BODIES = ["[1,2,3]", '"a string"', "123", "null", "true"]


@pytest.mark.parametrize("method,path", DICT_BODY_ENDPOINTS)
def test_顶层数组体一律400_bad_body(client, method, path):
    """没有这条通则时，`body.get(...)` 会抛 AttributeError → 500。"""
    r = getattr(client, method)(path, data="[1,2,3]", content_type="application/json")
    assert r.status_code == 400, "%s %s 实得 %s" % (method.upper(), path, r.status_code)
    assert r.get_json()["error"] == "bad_body"


@pytest.mark.parametrize("raw", NON_DICT_BODIES)
def test_各种非dict顶层类型都是400(client, raw):
    r = client.post("/hub/api/provider", data=raw, content_type="application/json")
    assert r.status_code == 400, "体 %r 实得 %s" % (raw, r.status_code)
    assert r.get_json()["error"] == "bad_body"


@pytest.mark.parametrize("path", ["/hub/api/provider/deadbeef/activate",
                                  "/hub/api/provider/deadbeef/test",
                                  "/hub/api/claude-native/activate",
                                  "/hub/api/oauth/accounts/codex/deadbeef/activate"])
def test_不读body的端点收到数组体也不许500(client, path):
    """这些端点契约上收 `{}`，内容不看；但也不能因此崩成 500。"""
    r = client.post(path, data="[1,2,3]", content_type="application/json")
    assert r.status_code != 500, "%s 被一个数组体打崩了" % path
    assert "application/json" in (r.headers.get("Content-Type") or "")


def test_重启端点收到数组体也不许500(make_client):
    """单独一条：必须先把重启命令换成假命令，否则真会重启用户的 bot。"""
    c, _ = make_client(HUB_RESTART_CMD="true")
    r = c.post("/hub/api/bots/restart", data="[1,2,3]", content_type="application/json")
    assert r.status_code != 500


def test_PATCH收到数组体也是400_bad_body(api):
    """PATCH 单独测：用真实存在的 id，免得 404 抢在体校验前面。

    （id 不存在 + 数组体时谁先命中，§0.1 通则与 §3.4 的 404 未定先后 —— 见报告。
    那种情况只断言"不 500"。）
    """
    from conftest import provider_payload
    code, created = api.post("/hub/api/provider", provider_payload())
    assert code == 201, created
    r = api.c.patch("/hub/api/provider/%s" % created["id"], data="[1,2,3]",
                    content_type="application/json")
    assert r.status_code == 400, "实得 %s" % r.status_code
    assert r.get_json()["error"] == "bad_body"


def test_不存在的id加数组体至少不能500(client):
    r = client.patch("/hub/api/provider/deadbeef", data="[1,2,3]",
                     content_type="application/json")
    assert r.status_code in (400, 404), "实得 %s" % r.status_code
    assert "application/json" in (r.headers.get("Content-Type") or "")
