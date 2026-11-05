# -*- coding: utf-8 -*-
"""白盒单测：dsh 适配器的情绪与节奏转发（约定 5.24；dsh-rhythm INTERFACE 3.4）。

dsh 一律是本文件自己在 127.0.0.1 随机端口上起的假服务（按"方法 + 路径"查表回应，记下每个请求的原始头与体），
令牌文件放 tmp_path、内容是假值。不连任何真实端口。写法照 tests/unit/test_dsh_lifecycle.py。
"""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from adapters import dsh_http
from adapters.base import BOT_ID_MESSAGE, AdapterError, AdapterUnavailable
from adapters.dsh_http import RHYTHM_BODY_MESSAGE, RHYTHM_TIMEOUT_MESSAGE, DshHttpAdapter

FAKE_TOKEN = "unit-fake-adapter-token-rhythm"
PATH = "/adapter/v1/bots/d1/rhythm"
SCHEMA = [{"key": "jiwen.rates.connection_per_min", "bot_key": "jiwen.rates_override.connection_per_min",
           "label": "基础累积速率", "unit": "每分钟", "type": "float", "default": 0.0007, "global": 0.0007,
           "global_is_default": False, "tab": "emotion", "min": 0.0, "max": 1.0},
          {"key": "moments.daily_post_limit_per_bot", "bot_key": "moments_daily_limit", "label": "每个 bot 每天发帖上限",
           "unit": "条", "type": "int", "default": 3, "global": 3, "global_is_default": True, "tab": "behavior",
           "min": 0, "max": 50}]
GET_BODY = {"ok": True, "bot_id": "d1", "version": "11-22-33", "schema": SCHEMA,
            "overrides": {"moments.daily_post_limit_per_bot": 0}, "warnings": ["legacy_key:jiwen.rates.x"]}


class FakeDsh:
    def __init__(self):
        self.routes, self.requests = {}, []
        self.delay, self.release = 0.0, threading.Event()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                fake.requests.append({"method": self.command, "path": self.path, "headers": dict(self.headers),
                                      "body": body})
                if fake.delay:
                    fake.release.wait(fake.delay)
                status, payload = fake.routes.get((self.command, self.path), (404, {"ok": False, "code": "nope"}))
                raw = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except OSError:      # 客户端已超时走了
                    pass

            do_GET = do_POST = do_PUT = _serve

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def dsh(tmp_path):
    token = tmp_path / "adapter.token"
    token.write_text(FAKE_TOKEN + "\n", encoding="utf-8")
    fake = FakeDsh()
    fake.adapter = DshHttpAdapter(base_url="http://127.0.0.1:%d" % fake.port, token_file=str(token))
    yield fake
    fake.close()


def _set(adapter, **kw):
    args = dict(set_values={"moments.daily_post_limit_per_bot": 0}, remove_keys=["jiwen.rates.connection_per_min"],
                version="11-22-33")
    args.update(kw)
    return adapter.set_rhythm("d1", args["set_values"], args["remove_keys"], args["version"])


CALLS = {"get": lambda a: a.get_rhythm("d1"), "set": lambda a: _set(a)}
METHOD = {"get": "GET", "set": "PUT"}


# ======================================================================== 正常转发


def test_get_rhythm_forwards_and_strips_ok(dsh):
    dsh.routes[("GET", PATH)] = (200, GET_BODY)
    got = dsh.adapter.get_rhythm("d1")
    assert got == {k: v for k, v in GET_BODY.items() if k != "ok"}
    (req,) = dsh.requests
    assert (req["method"], req["path"], req["body"]) == ("GET", PATH, b"")
    assert req["headers"]["Authorization"] == "Bearer " + FAKE_TOKEN


def test_get_rhythm_bot_id_percent_encoded_path(dsh):
    dsh.routes[("GET", "/adapter/v1/bots/a.b_c-1/rhythm")] = (200, dict(GET_BODY, bot_id="a.b_c-1"))
    assert dsh.adapter.get_rhythm("a.b_c-1")["bot_id"] == "a.b_c-1"
    assert dsh.requests[0]["path"] == "/adapter/v1/bots/a.b_c-1/rhythm"


@pytest.mark.parametrize("warnings, want", [
    (None, []), ("legacy_key:x", []), ({"a": 1}, []), (["a", 1, None, "b", ["c"]], ["a", "b"]),
], ids=["missing", "string", "object", "mixed"])
def test_get_rhythm_warnings_normalized(dsh, warnings, want):
    body = {k: v for k, v in GET_BODY.items() if k != "warnings"}
    if warnings is not None:
        body["warnings"] = warnings
    dsh.routes[("GET", PATH)] = (200, body)
    assert dsh.adapter.get_rhythm("d1")["warnings"] == want


@pytest.mark.parametrize("patch", [
    {"version": None}, {"version": ""}, {"version": 3}, {"schema": None}, {"schema": {"a": 1}},
    {"schema": [1]}, {"schema": [{"label": "x"}]}, {"schema": [{"key": 3}]}, {"overrides": None},
    {"overrides": []},
], ids=["no_version", "empty_version", "int_version", "no_schema", "schema_obj", "schema_item_int",
        "schema_no_key", "schema_key_int", "no_overrides", "overrides_list"])
def test_get_rhythm_bad_shape(dsh, patch):
    body = dict(GET_BODY)
    for k, v in patch.items():
        if v is None:
            body.pop(k)
        else:
            body[k] = v
    dsh.routes[("GET", PATH)] = (200, body)
    with pytest.raises(AdapterUnavailable) as e:
        dsh.adapter.get_rhythm("d1")
    assert (e.value.status, e.value.message) == ("error", "底层回的形状不对")


def test_set_rhythm_forwards_exact_body(dsh):
    dsh.routes[("PUT", PATH)] = (200, {"ok": True, "version": "44-55-66",
                                       "changed": ["moments.daily_post_limit_per_bot",
                                                   "jiwen.rates.connection_per_min"]})
    got = _set(dsh.adapter)
    assert got == {"version": "44-55-66",
                   "changed": ["moments.daily_post_limit_per_bot", "jiwen.rates.connection_per_min"]}
    (req,) = dsh.requests
    assert (req["method"], req["path"]) == ("PUT", PATH)
    assert req["headers"]["Content-Type"] == "application/json"
    assert req["headers"]["Authorization"] == "Bearer " + FAKE_TOKEN
    assert json.loads(req["body"].decode("utf-8")) == {
        "set": {"moments.daily_post_limit_per_bot": 0}, "remove": ["jiwen.rates.connection_per_min"],
        "version": "11-22-33"}


def test_set_rhythm_empty_set_and_remove_still_sent_and_extra_keys_kept(dsh):
    dsh.routes[("PUT", PATH)] = (200, {"ok": True, "version": "v1", "changed": [], "note": "x"})
    assert _set(dsh.adapter, set_values={}, remove_keys=[]) == {"version": "v1", "changed": [], "note": "x"}
    assert json.loads(dsh.requests[0]["body"]) == {"set": {}, "remove": [], "version": "11-22-33"}


@pytest.mark.parametrize("body", [
    {"ok": True, "changed": []}, {"ok": True, "version": "", "changed": []},
    {"ok": True, "version": "v", "changed": "a"}, {"ok": True, "version": "v", "changed": ["a", 1]},
    {"ok": True, "version": "v"},
], ids=["no_version", "empty_version", "changed_str", "changed_mixed", "no_changed"])
def test_set_rhythm_bad_shape(dsh, body):
    dsh.routes[("PUT", PATH)] = (200, body)
    with pytest.raises(AdapterUnavailable) as e:
        _set(dsh.adapter)
    assert (e.value.status, e.value.message) == ("error", "底层回的形状不对")


# ======================================================================== dsh 的失败原样包成 AdapterError


ERRORS = [
    (404, {"ok": False, "code": "unknown_bot", "message": "没有这只 bot"}, {}),
    (422, {"ok": False, "code": "invalid", "message": "有几项没填对",
           "fields": {"moments.same_kind_hours": "超出允许范围 [0, 72]", "jiwen.rates.immersion_map": "不能在这里改",
                      "bad": 3}},
     {"moments.same_kind_hours": "超出允许范围 [0, 72]", "jiwen.rates.immersion_map": "不能在这里改"}),
    (409, {"ok": False, "code": "stale", "message": "这只 bot 的节奏刚被别处改过，请刷新后再改"}, {}),
    (409, {"ok": False, "code": "busy", "message": "另一次保存正在进行，这次没有保存，请稍后重试"}, {}),
    (500, {"ok": False, "code": "internal", "message": "写回校验没通过，文件没有改动"}, {}),
    (501, {"ok": False, "code": "unsupported", "message": "这只 bot 没有单独的生活设定文件，不能在这里改"}, {}),
]


@pytest.mark.parametrize("which", ["get", "set"])
@pytest.mark.parametrize("status, payload, fields", ERRORS,
                         ids=["404", "422_fields", "409_stale", "409_busy", "500", "501"])
def test_errors_passed_through(dsh, which, status, payload, fields):
    dsh.routes[(METHOD[which], PATH)] = (status, payload)
    with pytest.raises(AdapterError) as e:
        CALLS[which](dsh.adapter)
    err = e.value
    assert (err.code, err.http_status, err.message, err.fields) == (payload["code"], status, payload["message"], fields)
    assert len(dsh.requests) == 1                     # 不重试


@pytest.mark.parametrize("which", ["get", "set"])
def test_long_message_truncated(dsh, which):
    dsh.routes[(METHOD[which], PATH)] = (422, {"ok": False, "code": "invalid", "message": "长" * 400})
    with pytest.raises(AdapterError) as e:
        CALLS[which](dsh.adapter)
    assert e.value.message == "长" * dsh_http.MAX_MESSAGE


@pytest.mark.parametrize("which", ["get", "set"])
@pytest.mark.parametrize("status, payload, want", [
    (401, {"ok": False, "code": "unauthorized", "message": "令牌缺失或不对"}, "unauthorized"),
    (200, b"not json", "error"),
    (302, {"ok": True}, "error"),
    (500, b"<html>oops</html>", "error"),
], ids=["401", "non_json", "redirect", "500_non_json"])
def test_transport_failures(dsh, which, status, payload, want):
    dsh.routes[(METHOD[which], PATH)] = (status, payload)
    with pytest.raises(AdapterUnavailable) as e:
        CALLS[which](dsh.adapter)
    assert e.value.status == want


def test_set_timeout_is_down_with_message_and_no_retry(dsh, monkeypatch):
    monkeypatch.setattr(dsh_http, "READ_TIMEOUT", 0.3)
    dsh.routes[("PUT", PATH)] = (200, {"ok": True, "version": "v", "changed": []})
    dsh.delay = 2.0
    with pytest.raises(AdapterUnavailable) as e:
        _set(dsh.adapter)
    assert (e.value.status, e.value.message) == ("down", RHYTHM_TIMEOUT_MESSAGE)
    assert len(dsh.requests) == 1


def test_get_timeout_is_down(dsh, monkeypatch):
    monkeypatch.setattr(dsh_http, "READ_TIMEOUT", 0.3)
    dsh.routes[("GET", PATH)] = (200, GET_BODY)
    dsh.delay = 2.0
    with pytest.raises(AdapterUnavailable) as e:
        dsh.adapter.get_rhythm("d1")
    assert e.value.status == "down" and FAKE_TOKEN not in e.value.message


def test_connection_refused_is_down(tmp_path):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                           # 端口空着：连不上
    token = tmp_path / "t"
    token.write_text(FAKE_TOKEN, encoding="utf-8")
    ad = DshHttpAdapter(base_url="http://127.0.0.1:%d" % port, token_file=str(token))
    for call in CALLS.values():
        with pytest.raises(AdapterUnavailable) as e:
            call(ad)
        assert e.value.status == "down"


# ======================================================================== 入参不合规：不读令牌、不发请求


@pytest.fixture
def no_token(tmp_path):
    """令牌文件不存在的适配器，外加一台记账的假服务：入参不合规时必须报 invalid，而不是 unconfigured。"""
    fake = FakeDsh()
    fake.adapter = DshHttpAdapter(base_url="http://127.0.0.1:%d" % fake.port,
                                  token_file=str(tmp_path / "missing.token"))
    yield fake
    fake.close()


@pytest.mark.parametrize("bot_id", ["", ".", "..", "a/b", "测试", "a b", "x" * 65, "a%2Fb"])
def test_bad_bot_id_invalid_without_token(no_token, bot_id):
    for call in (lambda: no_token.adapter.get_rhythm(bot_id),
                 lambda: no_token.adapter.set_rhythm(bot_id, {}, [], "v")):
        with pytest.raises(AdapterError) as e:
            call()
        assert (e.value.code, e.value.http_status, e.value.message) == ("invalid", 422, BOT_ID_MESSAGE)
    assert no_token.requests == []


@pytest.mark.parametrize("set_values, remove_keys, version", [
    (None, [], "v"), ([], [], "v"), ("a", [], "v"), ({}, None, "v"), ({}, (), "v"), ({}, {"a": 1}, "v"),
    ({}, [], None), ({}, [], ""), ({}, [], 3), ({}, [], b"v"),
], ids=["set_none", "set_list", "set_str", "remove_none", "remove_tuple", "remove_dict",
        "version_none", "version_empty", "version_int", "version_bytes"])
def test_bad_body_invalid_without_token(no_token, set_values, remove_keys, version):
    with pytest.raises(AdapterError) as e:
        no_token.adapter.set_rhythm("d1", set_values, remove_keys, version)
    assert (e.value.code, e.value.http_status, e.value.message, e.value.fields) == (
        "invalid", 422, RHYTHM_BODY_MESSAGE, {})
    assert no_token.requests == []


def test_valid_input_without_token_is_unconfigured(no_token):
    # 对照：入参合规时才去读令牌，令牌没配 → unconfigured（同样不发请求）
    for call in CALLS.values():
        with pytest.raises(AdapterUnavailable) as e:
            call(no_token.adapter)
        assert e.value.status == "unconfigured"
    assert no_token.requests == []
