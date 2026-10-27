# -*- coding: utf-8 -*-
"""adapters.dsh_http 单测：起 127.0.0.1 随机端口的 mock dsh，不连真实 17990。

覆盖 INTERFACE §7 的端点表：meta、bots、create、启停/切换、供应商、重启、生活设定、人设、语音、
功能开关、群设置、令牌转交、归档；以及 401、503、超时、非 JSON、令牌不落日志。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from adapters import dsh_http, registry
from adapters.base import DSH_CAPABILITIES, AdapterError, AdapterUnavailable, SecretToken

TOKEN = "test-adapter-token-DO-NOT-LEAK-0123456789"
TELEGRAM_TOKEN = "123456789:" + "A" * 35
BOT = "alpha"
CAPS = dict(DSH_CAPABILITIES)
META = {"backend": "dsh", "backend_version": "1.2.3", "adapter": "v1",
        "capabilities": CAPS, "effort_levels": ["low", "medium", "high", "xhigh", "max"]}


class MockDsh:
    """回环 mock dsh。``routes[(method, path)] -> callable(record) -> (status, payload)``。"""

    def __init__(self):
        self.routes = {}
        self.records = []
        self.expected_token = TOKEN
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):   # 不把请求写进 stderr
                pass

            def _reply(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else None
                except (ValueError, UnicodeDecodeError):
                    body = None
                rec = {"method": self.command, "path": self.path,
                       "headers": dict(self.headers), "body": body, "raw": raw}
                outer.records.append(rec)
                if outer.expected_token is not None and \
                        self.headers.get("Authorization") != "Bearer " + outer.expected_token:
                    return self._send(401, {"code": "unauthorized", "message": "bad token"})
                route = outer.routes.get((self.command, self.path.split("?", 1)[0]))
                if route is None:
                    return self._send(404, {"code": "not_found", "message": "no route"})
                try:
                    status, payload = route(rec)
                except Exception:   # pragma: no cover - 测试桩自身出错时给个 500
                    return self._send(500, {"code": "internal", "message": "mock failed"})
                return self._send(status, payload)

            def _send(self, status, payload):
                if isinstance(payload, bytes):
                    raw, ctype = payload, "application/octet-stream"
                elif payload is None:
                    raw, ctype = b"", "application/json"
                else:
                    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    ctype = "application/json"
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_GET = do_POST = do_PUT = do_DELETE = _reply

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.handle_error = lambda *args: None
        self.port = self.server.server_address[1]
        self.base_url = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def route(self, method, path, payload, status=200):
        if callable(payload):
            self.routes[(method, path)] = payload
        else:
            self.routes[(method, path)] = lambda rec: (status, payload)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def mock():
    server = MockDsh()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "adapter.token"
    path.write_text(TOKEN + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


@pytest.fixture
def adapter(mock, token_file, monkeypatch):
    monkeypatch.delenv("CONSOLE_DSH_ADAPTER_URL", raising=False)
    monkeypatch.delenv("CONSOLE_ADAPTER_TOKEN_FILE", raising=False)
    return dsh_http.DshHttpAdapter(base_url=mock.base_url, token_file=str(token_file))


def _method_path(mock, index=-1):
    rec = mock.records[index]
    return rec["method"], rec["path"]


# ── 构造与令牌 ───────────────────────────────────────────────────────────────

def test_默认只连回环17990():
    assert dsh_http.DEFAULT_BASE_URL == "http://127.0.0.1:17990"
    assert dsh_http.DEFAULT_TOKEN_FILE.endswith(".config/claude-dsh-console/adapter.token")
    a = dsh_http.DshHttpAdapter(base_url="http://127.0.0.1:17990", token_file="/nonexistent")
    assert (a._host, a._port) == ("127.0.0.1", 17990)


@pytest.mark.parametrize("bad", [
    "http://192.0.2.10:17990",          # TEST-NET，非回环
    "https://127.0.0.1:17990",          # 只许 http
    "http://127.0.0.1:17990/api",       # 不许带路径
    "http://127.0.0.1:17990?x=1",       # 不许带查询串
    "http://user@127.0.0.1:17990",      # 不许带用户信息
])
def test_构造拒绝非回环或带路径的基址(bad):
    with pytest.raises(ValueError):
        dsh_http.DshHttpAdapter(base_url=bad, token_file="/nonexistent")


def test_未配置令牌映射503(mock, tmp_path):
    missing = tmp_path / "missing.token"
    a = dsh_http.DshHttpAdapter(base_url=mock.base_url, token_file=str(missing))
    with pytest.raises(AdapterUnavailable) as ei:
        a.meta()
    assert ei.value.status == "unconfigured"
    assert registry.unavailable_http(ei.value.status) == (503, "backend_unconfigured")
    assert mock.records == []          # 没读令牌就不发请求

    empty = tmp_path / "empty.token"
    empty.write_text("  \n", encoding="utf-8")
    b = dsh_http.DshHttpAdapter(base_url=mock.base_url, token_file=str(empty))
    with pytest.raises(AdapterUnavailable) as ei:
        b.meta()
    assert ei.value.status == "unconfigured"
    assert mock.records == []


def test_令牌文件内容不合规不泄露内容(mock, tmp_path):
    path = tmp_path / "bad.token"
    path.write_text("SECRET-TOKEN-abc\nsecond-line", encoding="utf-8")
    a = dsh_http.DshHttpAdapter(base_url=mock.base_url, token_file=str(path))
    with pytest.raises(AdapterUnavailable) as ei:
        a.meta()
    assert ei.value.status == "error"
    assert "SECRET-TOKEN-abc" not in str(ei.value)
    assert mock.records == []


def test_401映射backend_unconfigured(adapter, mock):
    mock.expected_token = "some-other-token"
    with pytest.raises(AdapterUnavailable) as ei:
        adapter.meta()
    assert ei.value.status == "unauthorized"
    assert registry.unavailable_http(ei.value.status) == (503, "backend_unconfigured")
    assert TOKEN not in str(ei.value) + repr(ei.value)


# ── meta / bots ─────────────────────────────────────────────────────────────

def test_meta来自mock且带令牌(adapter, mock):
    mock.route("GET", "/adapter/v1/meta", META)
    assert adapter.meta() == META
    rec = mock.records[-1]
    assert (rec["method"], rec["path"]) == ("GET", "/adapter/v1/meta")
    assert rec["headers"]["Authorization"] == "Bearer " + TOKEN


def test_meta形状不对映射502(adapter, mock):
    mock.route("GET", "/adapter/v1/meta",
               {"backend": "dsh", "capabilities": [], "effort_levels": []})
    with pytest.raises(AdapterUnavailable) as ei:
        adapter.meta()
    assert ei.value.status == "error"
    assert registry.unavailable_http(ei.value.status) == (502, "backend_down")


def test_bots同时接受包装与裸数组(adapter, mock):
    items = [{"bot_id": BOT, "person": BOT, "enabled": True, "state": "running", "polling": True}]
    mock.route("GET", "/adapter/v1/bots", {"bots": items})
    assert adapter.bots() == items
    mock.route("GET", "/adapter/v1/bots", items)   # INTERFACE §7 的裸数组写法
    assert adapter.bots() == items


# ── 新建 / 切换 / 启停 ───────────────────────────────────────────────────────

def test_create只把令牌放进请求体(adapter, mock):
    mock.route("POST", "/adapter/v1/bots",
               {"bot_id": "newbot", "person": "newbot", "username": "new_bot",
                "enabled": True, "running": True, "message": "建好了", "steps": []})
    token = SecretToken(TELEGRAM_TOKEN)
    result = adapter.create({"bot_id": "newbot", "purpose": "switch_in",
                             "telegram_username": "new_bot", "op_id": "op-1"}, token)
    rec = mock.records[-1]
    assert (rec["method"], rec["path"]) == ("POST", "/adapter/v1/bots")
    assert rec["body"]["telegram_token"] == TELEGRAM_TOKEN
    assert "op_id" not in rec["body"]
    assert rec["body"]["person"] == "newbot" and rec["body"]["enabled"] is True
    assert result["bot_id"] == "newbot" and result["running"] is True
    assert TELEGRAM_TOKEN not in repr(result) + str(result)


def test_切换出与切进时带moved_to和import_from(adapter, mock):
    mock.route("POST", "/adapter/v1/bots/%s/enabled" % BOT,
               {"enabled": False, "running": False, "message": "已停用"})
    out = adapter.set_enabled(BOT, False, op_id="op-1", moved_to="pending")
    assert out["enabled"] is False and out["running"] is False
    rec = mock.records[-1]
    assert rec["body"] == {"enabled": False, "op_id": "op-1", "moved_to": "pending"}

    mock.route("POST", "/adapter/v1/bots/beta/enabled",
               {"enabled": True, "running": True, "message": "已启用"})
    got = adapter.set_enabled("beta", True, import_from="alpha")
    assert got["enabled"] is True and got["running"] is True
    assert mock.records[-1]["body"] == {"enabled": True, "import_from": "alpha"}


def test_停用后仍在收消息报still_running(adapter, mock):
    mock.route("POST", "/adapter/v1/bots/%s/enabled" % BOT,
               {"enabled": False, "running": True, "message": "还在跑"})
    with pytest.raises(AdapterError) as ei:
        adapter.set_enabled(BOT, False)
    assert (ei.value.code, ei.value.http_status) == ("still_running", 503)
    assert ei.value.message == dsh_http.STILL_RUNNING_MESSAGE


def test_启停结果未知时重读名单(adapter, mock):
    mock.route("POST", "/adapter/v1/bots/%s/enabled" % BOT, b"boom", status=500)
    mock.route("GET", "/adapter/v1/bots",
               {"bots": [{"bot_id": BOT, "enabled": False, "state": "stopped", "polling": False}]})
    out = adapter.set_enabled(BOT, False)
    assert out["enabled"] is False and out["running"] is False
    assert [p for _, p in (_method_path(mock, i) for i in range(len(mock.records)))] == [
        "/adapter/v1/bots/%s/enabled" % BOT, "/adapter/v1/bots"]


def test_启停结果未知且名单仍是原值时报未生效(adapter, mock):
    mock.route("POST", "/adapter/v1/bots/%s/enabled" % BOT, b"boom", status=500)
    mock.route("GET", "/adapter/v1/bots",
               {"bots": [{"bot_id": BOT, "enabled": True, "state": "running", "polling": True}]})
    with pytest.raises(AdapterUnavailable) as ei:
        adapter.set_enabled(BOT, False)
    assert ei.value.status == "down"
    assert ei.value.message == dsh_http.ENABLE_NOT_APPLIED


# ── 其余转发端点 ─────────────────────────────────────────────────────────────

def test_供应商重启生活人设语音显示名(adapter, mock):
    mock.route("POST", "/adapter/v1/bots/%s/provider" % BOT,
               {"applied": True, "queued": False, "new_segment": True, "message": "换了"})
    assert adapter.set_provider(BOT, {"provider_id": "p1", "model": "m1"})["applied"] is True
    assert mock.records[-1]["body"] == {"provider_id": "p1", "model": "m1"}

    mock.route("POST", "/adapter/v1/bots/%s/restart" % BOT, {"status": "ok", "message": "重启了"})
    assert adapter.restart(BOT, True)["status"] == "ok"
    assert mock.records[-1]["body"] == {"force": True}

    mock.route("GET", "/adapter/v1/bots/%s/life" % BOT, {"fields": ["city"], "values": {"city": "X"}})
    assert adapter.get_life(BOT)["values"] == {"city": "X"}
    mock.route("PUT", "/adapter/v1/bots/%s/life" % BOT,
               {"applied": True, "queued": False, "message": "ok"})
    assert adapter.set_life(BOT, {"city": "Y"})["applied"] is True
    assert mock.records[-1]["body"] == {"values": {"city": "Y"}}

    voice = {"voice_id": "v1", "tts_model": "s1", "text_language": "zh", "voice_language": "zh"}
    mock.route("GET", "/adapter/v1/bots/%s/voice" % BOT, voice)
    assert adapter.get_voice(BOT) == voice
    mock.route("PUT", "/adapter/v1/bots/%s/voice" % BOT, dict(voice, message="保存了"))
    assert adapter.set_voice(BOT, {"voice_id": "v1"})["message"] == "保存了"

    mock.route("GET", "/adapter/v1/bots/%s/profile" % BOT, {"display_name": "Alpha"})
    assert adapter.get_profile(BOT) == {"display_name": "Alpha"}
    mock.route("PUT", "/adapter/v1/bots/%s/profile" % BOT, {"display_name": "New", "message": "已保存"})
    assert adapter.set_profile(BOT, "New")["display_name"] == "New"
    with pytest.raises(AdapterError):
        adapter.set_profile(BOT, "x" * 33)


def test_人设读取写入与版本冲突(adapter, mock):
    mock.route("GET", "/adapter/v1/bots/%s/persona" % BOT,
               {"text": "hi", "version": "0123456789abcdef"})
    assert adapter.get_persona(BOT)["version"] == "0123456789abcdef"

    mock.route("PUT", "/adapter/v1/bots/%s/persona" % BOT,
               {"version": "fedcba9876543210", "message": "已保存"})
    body = {"text": "new", "version": "0123456789abcdef"}
    assert adapter.set_persona(BOT, body)["version"] == "fedcba9876543210"
    assert mock.records[-1]["body"] == body

    mock.route("PUT", "/adapter/v1/bots/%s/persona" % BOT,
               {"code": "stale", "message": "版本过期"}, status=409)
    with pytest.raises(AdapterError) as ei:
        adapter.set_persona(BOT, body)
    assert (ei.value.code, ei.value.http_status) == ("stale", 409)


# ── 功能开关 / 群设置 ─────────────────────────────────────────────────────────

def test_功能开关先读keys再写并拒绝未知键(adapter, mock):
    state = {"keys": [{"key": "voice", "label": "发语音"}, {"key": "groups", "label": "群聊"}],
             "features": {"voice": True, "groups": False},
             "requirements": {"voice": ["tts"]}, "warnings": []}
    mock.route("GET", "/adapter/v1/bots/%s/features" % BOT, state)
    mock.route("PUT", "/adapter/v1/bots/%s/features" % BOT,
               {"features": {"voice": False, "groups": False}, "changed": ["voice"],
                "applied": True, "queued": False, "message": "已生效"})
    assert adapter.get_features(BOT)["features"]["voice"] is True
    got = adapter.set_features(BOT, {"voice": False})
    assert got["applied"] is True and got["changed"] == ["voice"]
    assert mock.records[-1]["body"] == {"features": {"voice": False}}

    before = len(mock.records)
    with pytest.raises(AdapterError) as ei:
        adapter.set_features(BOT, {"bogus": True})
    assert (ei.value.code, ei.value.http_status) == ("invalid", 422)
    assert ei.value.fields == {"bogus": "没有这个功能"}
    assert len(mock.records) == before + 1          # 只多了一次 GET，没有发出 PUT


def test_群设置读单群写退群与整组回落(adapter, mock):
    mock.route("GET", "/adapter/v1/bots/%s/groups" % BOT,
               {"username": "my_bot", "groups": {"-1001234567890": {"requireMention": True}}})
    assert adapter.groups(BOT)["groups"]["-1001234567890"]["requireMention"] is True

    chat = "-1001234567890"
    mock.route("POST", "/adapter/v1/bots/%s/groups/%s" % (BOT, chat),
               {"in_group": True, "policy": {"requireMention": True}})
    got = adapter.set_groups(BOT, {"chat_id": chat, "join": True,
                                   "policy": {"requireMention": True}})
    assert got == {"in_group": True, "policy": {"requireMention": True}}
    assert mock.records[-1]["body"] == {"group": {"requireMention": True}}

    mock.route("DELETE", "/adapter/v1/bots/%s/groups/%s" % (BOT, chat), {"ok": True})
    got = adapter.set_groups(BOT, {"chat_id": chat, "join": False})
    assert got == {"in_group": False}
    assert (mock.records[-1]["method"], mock.records[-1]["body"]) == ("DELETE", None)

    # 单群路由不被支持时回落到整组 POST /groups（INTERFACE §7）
    mock.route("POST", "/adapter/v1/bots/beta/groups", {"in_group": True, "policy": {}})
    got = adapter.set_groups("beta", {"chat_id": chat, "join": True})
    assert got["in_group"] is True
    assert _method_path(mock)[1] == "/adapter/v1/bots/beta/groups"


def test_令牌转交只允许切换任务的取令牌步骤(adapter, mock):
    mock.route("POST", "/adapter/v1/bots/%s/token_handoff" % BOT,
               {"telegram_token": TELEGRAM_TOKEN})

    class Op:
        def __init__(self):
            self.rec = {"kind": "switch", "status": "running", "bot_id": BOT, "backend": "dsh",
                        "op_id": "op-1", "target": {"backend": "claude"},
                        "steps": [{"step": "take_token", "status": "running"}]}
            self.saved = False

        def save(self):
            self.saved = True

    op = Op()
    token = adapter.take_token(BOT, op)
    assert token.reveal() == TELEGRAM_TOKEN
    assert TELEGRAM_TOKEN not in repr(token) + str(token)
    assert op.saved is True
    assert op.rec["_internal"]["data"]["token_taken"] is True
    assert mock.records[-1]["body"] == {"op_id": "op-1", "to": "claude", "purpose": "switch"}

    with pytest.raises(AdapterError) as ei:
        adapter.take_token(BOT, op)          # 同一任务只能取一次
    assert ei.value.code == "step_failed"

    denied = Op()
    denied.rec["status"] = "done"
    with pytest.raises(AdapterError) as ei:
        adapter.take_token(BOT, denied)
    assert ei.value.code == "step_failed"


# ── 归档 / 模板 ──────────────────────────────────────────────────────────────

def test_归档清单恢复删除(adapter, mock):
    mock.route("POST", "/adapter/v1/bots/%s/archive" % BOT,
               {"archive_id": "a1", "message": "归档了", "steps": []})
    assert adapter.archive(BOT, {"display_name": "Alpha"})["archive_id"] == "a1"
    assert mock.records[-1]["body"] == {"shell_meta": {"display_name": "Alpha"}}

    mock.route("GET", "/adapter/v1/archives",
               {"archives": [{"archive_id": "a1", "bot_id": BOT, "display_name": "Alpha"}]})
    item = adapter.list_archives()[0]
    assert item["archive_id"] == "a1" and item["person"] is None and item["status"] is None

    mock.route("POST", "/adapter/v1/archives/a1/restore",
               {"bot_id": BOT, "enabled": False, "running": False, "message": "恢复了", "steps": []})
    assert adapter.restore("a1")["enabled"] is False

    mock.route("POST", "/adapter/v1/bots/%s/delete" % BOT,
               {"message": "删了", "trash": None, "trash_path": None, "steps": []})
    assert adapter.delete(BOT, BOT)["message"] == "删了"
    with pytest.raises(AdapterError):
        adapter.delete(BOT, "other")


def test_模板同时接受两种形状并过滤坏项(adapter, mock):
    items = [{"id": "blank", "label": "空白"}, {"id": "persona", "label": "人设"}, {"id": 1}]
    mock.route("GET", "/adapter/v1/bots/templates", items)
    assert adapter.templates() == [{"id": "blank", "label": "空白"}, {"id": "persona", "label": "人设"}]
    mock.route("GET", "/adapter/v1/bots/templates", {"templates": items})
    assert len(adapter.templates()) == 2


# ── 错误路径 ─────────────────────────────────────────────────────────────────

def test_200非JSON映射502(adapter, mock):
    mock.route("GET", "/adapter/v1/meta", b"<html>not json</html>")
    with pytest.raises(AdapterUnavailable) as ei:
        adapter.meta()
    assert ei.value.status == "error"
    assert ei.value.message == "底层回的不是 JSON"
    assert registry.unavailable_http(ei.value.status) == (502, "backend_down")


def test_503非JSON映射502(adapter, mock):
    mock.route("GET", "/adapter/v1/meta", b"Service Unavailable", status=503)
    with pytest.raises(AdapterUnavailable) as ei:
        adapter.meta()
    assert ei.value.status == "error"
    assert registry.unavailable_http(ei.value.status) == (502, "backend_down")
    assert TOKEN not in str(ei.value) + repr(ei.value)


def test_读超时映射down(adapter, mock, monkeypatch):
    def slow(_rec):
        time.sleep(0.4)
        return 200, META

    mock.routes[("GET", "/adapter/v1/meta")] = slow
    monkeypatch.setattr(dsh_http, "READ_TIMEOUT", 0.05)
    with pytest.raises(AdapterUnavailable) as ei:
        adapter.meta()
    assert ei.value.status == "down"
    assert registry.unavailable_http(ei.value.status) == (502, "backend_down")


def test_连不上映射down(token_file):
    import socket
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    a = dsh_http.DshHttpAdapter(base_url="http://127.0.0.1:%d" % port, token_file=str(token_file))
    with pytest.raises(AdapterUnavailable) as ei:
        a.meta()
    assert ei.value.status == "down"
    assert registry.unavailable_http(ei.value.status) == (502, "backend_down")


def test_令牌不落日志与异常(adapter, mock, caplog):
    mock.route("GET", "/adapter/v1/meta", b"boom", status=500)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(AdapterUnavailable) as ei:
            adapter.meta()
    combined = caplog.text + str(ei.value) + repr(ei.value)
    assert TOKEN not in combined
    assert TELEGRAM_TOKEN not in combined


# ── Windows ──────────────────────────────────────────────────────────────────

@pytest.mark.skipif(os.name != "nt",
                    reason="Windows 令牌 ACL 与启动链路已由 T02 启动实验验证；本机非 Windows 跳过")
def test_windows令牌文件可读(mock, tmp_path):
    path = tmp_path / "adapter.token"
    path.write_text(TOKEN + "\n", encoding="utf-8")
    a = dsh_http.DshHttpAdapter(base_url=mock.base_url, token_file=str(path))
    mock.route("GET", "/adapter/v1/meta", META)
    assert a.meta()["backend"] == "dsh"
