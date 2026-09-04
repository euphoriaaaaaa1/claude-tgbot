# -*- coding: utf-8 -*-
"""§3.7 连通性自测：真发一次最小请求，业务结果用 ok/stage 表达而不是 HTTP 错误码。"""
import json
import os

import pytest

from conftest import provider_payload, FAKE_DOWNSTREAM_KEY, _Api

ALIAS = "claude-sonnet-4-5-20250929"


def _mk(api, **over):
    code, body = api.post("/hub/api/provider", provider_payload(**over))
    assert code == 201, body
    return body


def _activate(api, pid):
    assert api.post("/hub/api/provider/%s/activate" % pid)[0] == 200


def test_自测成功返回ok与延迟与模型回显(api, stub):
    p = _mk(api)
    _activate(api, p["id"])
    code, body = api.post("/hub/api/provider/%s/test" % p["id"])
    assert code == 200, body
    assert body["ok"] is True
    assert isinstance(body["latency_ms"], int)
    assert body["model_echo"] == ALIAS
    assert len(body.get("text_head", "")) <= 40


def test_自测请求体是最小请求且打的是v1_messages(api, stub):
    """§3.7：body {"model":...,"max_tokens":16,"messages":[{"role":"user","content":"hi"}]}"""
    p = _mk(api)
    _activate(api, p["id"])
    api.post("/hub/api/provider/%s/test" % p["id"])
    hits = [r for r in stub.requests if r["path"] == "/v1/messages"]
    assert hits, "自测没有真发请求（§3.7 要求真发一次最小请求）"
    sent = json.loads(hits[-1]["body"])
    assert sent["model"] == ALIAS
    assert sent["max_tokens"] == 16
    assert sent["messages"] == [{"role": "user", "content": "hi"}]
    assert hits[-1]["headers"].get("authorization") == "Bearer %s" % FAKE_DOWNSTREAM_KEY


def test_上游4xx时仍是HTTP200_用结构化字段表达(api, stub):
    """§3.7：只有"我们这边坏了"才用非 200。上游报错是业务结果。"""
    p = _mk(api)
    _activate(api, p["id"])
    stub.messages_mode = "http4xx_echo"
    code, body = api.post("/hub/api/provider/%s/test" % p["id"])
    assert code == 200, "上游 401 被映射成了 HTTP %s" % code
    assert body["ok"] is False
    assert body["stage"] == "upstream"
    assert body["upstream_status"] == 401
    assert body["error_type"] == "authentication_error"
    assert len(body.get("error_message", "")) <= 200


def test_上游4xx回显请求头时不得把原始响应体端出来(api, stub):
    """§0.2 必须存在的用例 2：上游 4xx 常把 Authorization 原样回显。"""
    p = _mk(api)
    _activate(api, p["id"])
    stub.messages_mode = "http4xx_echo"
    code, body = api.post("/hub/api/provider/%s/test" % p["id"])
    text = json.dumps(body, ensure_ascii=False)
    assert FAKE_DOWNSTREAM_KEY not in text, "上游回显的 Bearer 令牌被原样端上桌"
    assert "echoed_request_headers" not in text, "原始响应体被透传（§3.7 禁止）"
    assert "Bearer" not in text


@pytest.mark.parametrize("status", [400, 502])
def test_未知模型按message文本判routing_与状态码无关(api, stub, status):
    """㉓：真机回 400、[CPX] 写的是 502。按状态码归类会在上游一改版就把"配置缺失"误报成"上游报错"。"""
    p = _mk(api)
    _activate(api, p["id"])
    stub.messages_mode = "unknown_model"
    stub.unknown_model_status = status
    code, body = api.post("/hub/api/provider/%s/test" % p["id"])
    assert code == 200
    assert body["ok"] is False
    assert body["stage"] == "routing", "上游回 %d 时没判成 routing：%s" % (status, body)


def test_未激活的cliproxy条目自测返回stage_disabled(api, stub):
    """§3.7：alias 被别人占着时它是 disabled，自测应给"先切换到它"的提示而不是打上游。"""
    first = _mk(api)
    _activate(api, first["id"])
    second = _mk(api, label="备用网关", base_url="https://gw.example.com/v1",
                 upstream_model="glm-4.6")
    assert second["disabled"] is True
    n = len([r for r in stub.requests if r["path"] == "/v1/messages"])
    code, body = api.post("/hub/api/provider/%s/test" % second["id"])
    assert code == 200
    assert body["ok"] is False
    assert body["stage"] == "disabled"
    assert len([r for r in stub.requests if r["path"] == "/v1/messages"]) == n, \
        "disabled 条目不该真发请求"


def test_cliproxy不可达时自测返回503(api, stub):
    p = _mk(api)
    stub.set_down(True)
    code, body = api.post("/hub/api/provider/%s/test" % p["id"])
    assert code == 503
    assert body["error"] == "cliproxy_down"


def test_自测不存在的id返回404(api):
    code, body = api.post("/hub/api/provider/deadbeef/test")
    assert code == 404
    assert body["error"] == "not_found"


@pytest.mark.slow
@pytest.mark.skipif(not os.environ.get("HUB_TEST_SLOW"),
                    reason="30 秒超时用例，设 HUB_TEST_SLOW=1 才跑")
def test_上游不响应时30秒后返回stage_timeout(api, stub):
    p = _mk(api)
    _activate(api, p["id"])
    stub.messages_mode = "timeout"
    code, body = api.post("/hub/api/provider/%s/test" % p["id"])
    assert code == 200
    assert body["ok"] is False and body["stage"] == "timeout"


# ⑩（修订 4）：`anthropic-direct` fallback 永不启用 —— install 直接写
# CLIPROXY_ANTHROPIC_COMPAT=1，运行时探测已删。原先 4 条 direct 自测用例（路径拼接、
# 用户 key、不适用 stage:"disabled"）随之删除，INTERFACE 明标"当前不可达、不写用例"。
# 保留的回归护栏是 test_provider_crud.py 里"route 恒 cliproxy"那条。
