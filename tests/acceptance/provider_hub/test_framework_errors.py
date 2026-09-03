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
