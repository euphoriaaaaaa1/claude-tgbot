"""openrouter-model-id-fix 验收用例。

唯一依据：.devflow/openrouter-model-id-fix-接口.md。
每个用例只通过对外 HTTP 接口驱动：POST /api/providers/models 与
POST /api/providers/models/candidates；上游是本地回环假 HTTP 服务。
"""

import json
import re
import socket

import pytest

MODELS = "/api/providers/models"
CANDIDATES = "/api/providers/models/candidates"
KEY = "sk-openrouter-model-id-key"
GEMINI = "google/gemini-3.8-flash"


# --------------------------------------------------------------------------- 断言助手

def _assert_openai_headers(req, key=KEY):
    h = req["headers"]
    assert h.get("authorization") == f"Bearer {key}", f"Authorization 不对: {h.get('authorization')!r}"
    assert "x-api-key" not in h, f"openai 协议不应带 x-api-key: {h.get('x-api-key')!r}"
    assert "anthropic-version" not in h, f"openai 协议不应带 anthropic-version: {h.get('anthropic-version')!r}"


def _assert_anthropic_headers(req, key=KEY):
    h = req["headers"]
    assert h.get("x-api-key") == key, f"x-api-key 不对: {h.get('x-api-key')!r}"
    assert h.get("anthropic-version") == "2023-06-01", (
        f"anthropic-version 不对: {h.get('anthropic-version')!r}"
    )


def _json_bytes(payload):
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _closed_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


# --------------------------------------------------------------------------- 行为清单

def test_行为1_models新建openai_发Bearer且不带anthropic头(client, upstream):
    upstream.set_json([{"id": "m1", "display_name": "模型一"}])
    r = client.post(MODELS, json={
        "base_url": upstream.base_url, "api_key": KEY, "protocol": "openai"})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert r.get_json()["ok"] is True
    assert upstream.paths() == ["/v1/models"], "应只请求一次 /v1/models"
    _assert_openai_headers(upstream.requests[0])


def test_行为2_models新建anthropic_发x_api_key与anthropic_version(client, upstream):
    upstream.set_json([{"id": "m1"}])
    r = client.post(MODELS, json={
        "base_url": upstream.base_url, "api_key": KEY, "protocol": "anthropic"})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert r.get_json()["ok"] is True
    assert upstream.paths() == ["/v1/models"], "应只请求一次 /v1/models"
    _assert_anthropic_headers(upstream.requests[0])


def test_行为3_models新建缺省protocol_按openai选头(client, upstream):
    upstream.set_json([{"id": "m1"}])
    r = client.post(MODELS, json={"base_url": upstream.base_url, "api_key": KEY})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert r.get_json()["ok"] is True
    assert upstream.paths() == ["/v1/models"], "应只请求一次 /v1/models"
    _assert_openai_headers(upstream.requests[0])


def test_行为4_models已存provider_protocol_openai_不带anthropic头(client, upstream, make_provider):
    pid = make_provider(protocol="openai", base_url=upstream.base_url, api_key=KEY)
    upstream.set_json([{"id": "m1"}])
    r = client.post(MODELS, json={"provider_id": pid, "query_only": True})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert r.get_json()["ok"] is True
    assert upstream.paths() == ["/v1/models"], "应只请求一次 /v1/models"
    _assert_openai_headers(upstream.requests[0])


def test_行为5_models已存provider_protocol_anthropic_带x_api_key与anthropic_version(
        client, upstream, make_provider):
    pid = make_provider(protocol="anthropic", base_url=upstream.base_url, api_key=KEY)
    upstream.set_json([{"id": "m1"}])
    r = client.post(MODELS, json={"provider_id": pid, "query_only": True})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert r.get_json()["ok"] is True
    assert upstream.paths() == ["/v1/models"], "应只请求一次 /v1/models"
    _assert_anthropic_headers(upstream.requests[0])


@pytest.mark.parametrize(
    "protocol,check",
    [("openai", _assert_openai_headers), ("anthropic", _assert_anthropic_headers)],
    ids=["openai", "anthropic"],
)
def test_行为6_candidates已存provider_按条目protocol选头(
        client, upstream, make_provider, protocol, check):
    pid = make_provider(protocol=protocol, base_url=upstream.base_url, api_key=KEY)
    upstream.set_json([{"id": "m1"}])
    r = client.post(CANDIDATES, json={"provider_id": pid})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert r.get_json()["ok"] is True
    assert upstream.paths() == ["/v1/models"], "应只请求一次 /v1/models"
    check(upstream.requests[0])


def test_行为7_candidates新建openai_不带anthropic头(client, upstream):
    upstream.set_json([{"id": "m1"}])
    r = client.post(CANDIDATES, json={
        "base_url": upstream.base_url, "api_key": KEY, "protocol": "openai"})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    assert r.get_json()["ok"] is True
    assert upstream.paths() == ["/v1/models"], "应只请求一次 /v1/models"
    _assert_openai_headers(upstream.requests[0])


def test_行为8_models_模型id原样返回_不改写(client, upstream):
    upstream.set_json([{"id": GEMINI, "display_name": "Gemini 3.8 Flash"}])
    r = client.post(MODELS, json={
        "base_url": upstream.base_url, "api_key": KEY, "protocol": "openai"})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    body = r.get_json()
    assert body["ok"] is True
    assert body["models"] == [{"id": GEMINI, "label": "Gemini 3.8 Flash"}]
    assert [m["id"] for m in body["models"]] == [GEMINI]


def test_行为8_candidates_模型id原样返回_不改写(client, upstream):
    upstream.set_json([{"id": GEMINI}])
    r = client.post(CANDIDATES, json={
        "base_url": upstream.base_url, "api_key": KEY, "protocol": "openai"})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    body = r.get_json()
    assert body["ok"] is True
    assert [c["id"] for c in body["candidates"]] == [GEMINI]


def test_行为9_anthropic分页_按after_id续拉并合并去重(client, upstream):
    upstream.routes = {
        "/v1/models": (200, _json_bytes({
            "data": [{"id": "m1"}, {"id": "m2"}], "has_more": True, "last_id": "m2"})),
        "/v1/models?after_id=m2": (200, _json_bytes({
            "data": [{"id": "m2"}, {"id": "m3"}], "has_more": False})),
    }
    r = client.post(CANDIDATES, json={
        "base_url": upstream.base_url, "api_key": KEY, "protocol": "anthropic"})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    body = r.get_json()
    assert body["ok"] is True
    assert upstream.paths() == ["/v1/models", "/v1/models?after_id=m2"], (
        f"分页请求路径不对: {upstream.paths()}")
    assert [c["id"] for c in body["candidates"]] == ["m1", "m2", "m3"], "两页应合并去重"
    _assert_anthropic_headers(upstream.requests[1])


def test_行为10_openai无has_more_单次请求正常返回(client, upstream):
    upstream.set_json([{"id": "o1"}])
    r = client.post(MODELS, json={
        "base_url": upstream.base_url, "api_key": KEY, "protocol": "openai"})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    body = r.get_json()
    assert body["ok"] is True
    assert upstream.paths() == ["/v1/models"], "没有 has_more 不应追加请求"
    assert body["models"] == [{"id": "o1", "label": "o1"}]


# --------------------------------------------------------------------------- 错误契约

@pytest.mark.parametrize(
    "code,want",
    [
        (401, "上游返回 401（key 无效或无权限）"),
        (403, "上游返回 403（无权限，或上游 CDN 拦截）"),
        (404, "上游返回 404（该地址没有 /v1/models 端点）"),
        (500, "上游返回 500"),
    ],
    ids=["401", "403", "404", "500"],
)
def test_错误_上游状态码_200响应okfalse且文案逐字(client, upstream, code, want):
    upstream.set_raw(b'{"error": "upstream-secret-sentinel"}', status=code)
    r = client.post(MODELS, json={
        "base_url": upstream.base_url, "api_key": KEY, "protocol": "openai"})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    body = r.get_json()
    assert body["ok"] is False
    assert body["models"] == []
    assert body["error"] == want
    assert len(upstream.requests) == 1
    assert "upstream-secret-sentinel" not in r.get_data(as_text=True), "不得把上游正文回传给调用方"


def test_错误_响应不是JSON_文案逐字(client, upstream):
    upstream.set_raw(b"<html>not json</html>")
    r = client.post(MODELS, json={
        "base_url": upstream.base_url, "api_key": KEY, "protocol": "openai"})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    body = r.get_json()
    assert body["ok"] is False
    assert body["models"] == []
    assert body["error"] == "上游响应不是 JSON"


def test_错误_响应超过1MiB_文案逐字(client, upstream):
    upstream.set_raw(b" " * (1024 * 1024 + 1024))
    r = client.post(MODELS, json={
        "base_url": upstream.base_url, "api_key": KEY, "protocol": "openai"})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    body = r.get_json()
    assert body["ok"] is False
    assert body["models"] == []
    assert body["error"] == "响应过大"


def test_错误_连接失败_文案含异常类名(client):
    port = _closed_port()
    r = client.post(MODELS, json={
        "base_url": f"http://127.0.0.1:{port}", "api_key": KEY, "protocol": "openai"})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    body = r.get_json()
    assert body["ok"] is False
    assert body["models"] == []
    assert re.fullmatch(r"连接失败: [A-Za-z_][A-Za-z0-9_.]*", body["error"] or ""), (
        f"连接失败文案应含异常类名: {body['error']!r}")
