# -*- coding: utf-8 -*-
"""T07 生命周期 HTTP 接口：成功/错误码/op 形状。

全部用假适配器与临时 HOME：不连 Telegram、不连 dsh、不碰真实 ~/.claude。
"""
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import bots_registry  # noqa: E402
from adapters import lifecycle_flow, ops, registry  # noqa: E402
from adapters.base import AdapterError, CAPABILITY_KEYS  # noqa: E402
from moments import lifecycle_routes, panel_auth, web  # noqa: E402

PASSWORD = "test-panel-pw-12345"
TOKEN = "123456789:" + "A" * 35
OP_ID = "20261020T000000Z-crea-abcdef12"


def bot(bot_id="mybot", backend="claude", polling=True, enabled=True):
    return {"bot_id": bot_id, "person": bot_id, "display_name": bot_id.title(), "enabled": enabled,
            "state": "running" if polling else ("idle" if enabled else "stopped"), "state_text": "",
            "polling": polling, "username": bot_id + "_bot", "chat_id": "12345",
            "provider": None, "provider_source": "default", "pending": None, "detail": {},
            "moved_to": None, "backend": backend}


class FakeAdapter:
    backend = "claude"

    def __init__(self, backend="claude", bots=(), archives=(), templates=None, caps=None):
        self.backend = backend
        self._bots = [dict(b) for b in bots]
        self.archives = [dict(a) for a in archives]
        self._templates = list(templates or [{"id": "persona", "label": "Persona"}])
        self.caps = dict(caps if caps is not None else {k: True for k in CAPABILITY_KEYS})
        self.calls = []

    def meta(self):
        return {"backend": self.backend, "backend_version": "fake", "adapter": "fake",
                "capabilities": dict(self.caps), "effort_levels": ["low", "medium", "high", "xhigh", "max"]}

    def bots(self):
        return [dict(b) for b in self._bots]

    def providers(self):
        return {"providers": [], "default": None}

    def templates(self):
        return [dict(t) for t in self._templates]

    def chat_id_default(self):
        return "12345"

    def _find(self, bot_id):
        return next((b for b in self._bots if b.get("bot_id") == bot_id), None)

    def get_profile(self, bot_id):
        me = self._find(bot_id)
        if me is None:
            raise AdapterError("unknown_bot", 404, "这个底层没有这只 bot")
        return {"display_name": me.get("display_name") or bot_id}

    def set_profile(self, bot_id, display_name):
        me = self._find(bot_id)
        if me is None:
            raise AdapterError("unknown_bot", 404, "这个底层没有这只 bot")
        me["display_name"] = display_name
        return {"display_name": display_name, "message": "已保存"}

    def set_enabled(self, bot_id, enabled, *, op_id=None, moved_to=..., import_from=None):
        self.calls.append(("set_enabled", bot_id, enabled))
        me = self._find(bot_id)
        if me is None:
            raise AdapterError("unknown_bot", 404, "这个底层没有这只 bot")
        me["enabled"] = enabled
        me["polling"] = bool(enabled)
        me["state"] = "running" if enabled else "stopped"
        return {"enabled": enabled, "running": bool(enabled), "message": "已启用" if enabled else "已停用"}

    def list_archives(self):
        return [dict(a) for a in self.archives]

    def archive(self, bot_id, meta, *, op_id=None):
        me = self._find(bot_id)
        if me is None:
            raise AdapterError("unknown_bot", 404, "这个底层没有这只 bot")
        self._bots.remove(me)
        item = {"archive_id": "arc-1", "bot_id": bot_id, "display_name": me.get("display_name"),
                "archived_at": "2026-10-20T00:00:00Z", "status": "complete", "shell_meta": dict(meta)}
        self.archives.append(item)
        return {"archive_id": "arc-1", "steps": []}

    def restore(self, archive_id, *, op_id=None):
        item = next((a for a in self.archives if a.get("archive_id") == archive_id), None)
        if item is None:
            raise AdapterError("unknown_archive", 404, "没有这份归档")
        self.archives.remove(item)
        self._bots.append(bot(item["bot_id"], backend=self.backend, polling=False, enabled=False))
        return {"bot_id": item["bot_id"], "steps": []}

    def delete(self, bot_id, confirm, *, op_id=None):
        me = self._find(bot_id)
        if me is None:
            raise AdapterError("unknown_bot", 404, "这个底层没有这只 bot")
        self._bots.remove(me)
        return {"trash": "system", "trash_path": "/tmp/trash/mybot", "steps": []}

    def resolve(self, rec):
        return None

    def create(self, spec, token):
        self.calls.append(("create", spec["bot_id"]))
        me = bot(spec["bot_id"], backend=self.backend, polling=True, enabled=spec.get("enabled", True))
        me["display_name"] = spec.get("display_name") or spec["bot_id"]
        me["username"] = "newbot_bot"
        self._bots.append(me)
        return {"bot_id": me["bot_id"], "person": me["bot_id"], "username": "newbot_bot",
                "enabled": me["enabled"], "running": me["enabled"], "steps": []}


def wait_op(op_id, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        op = ops.get_op(op_id)
        if op is not None and op["state"] in ("done", "failed", "needs_attention"):
            return op
        time.sleep(0.01)
    raise AssertionError("操作没有在 %.1f 秒内结束：%r" % (timeout, ops.get_op(op_id)))


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("CLAUDEBOT_CONFIG_DIR", str(tmp_path / "configs"))
    monkeypatch.setenv("CLAUDEBOT_CHANNELS_ROOT", str(tmp_path / "channels"))
    monkeypatch.setattr(panel_auth, "SECRET_PATH", str(tmp_path / "secrets" / "panel.env"))
    monkeypatch.setattr(panel_auth, "SESSIONS_PATH", str(tmp_path / "secrets" / "sessions.json"))
    monkeypatch.setattr(bots_registry, "reload", lambda: None)
    monkeypatch.setattr(lifecycle_flow, "use_moments_purge", lambda fn: None, raising=False)
    old = lifecycle_flow._telegram_get_me
    lifecycle_flow.use_telegram_get_me(
        lambda token, *, rate_key: {"username": "newbot_bot", "bot_user_id": 123456789})
    panel_auth._fail.clear()
    yield
    lifecycle_flow.use_telegram_get_me(old)
    panel_auth._fail.clear()


@pytest.fixture
def client():
    panel_auth.set_password(PASSWORD)
    web.app.config["TESTING"] = True
    c = web.app.test_client()
    r = c.post("/login", data={"password": PASSWORD, "next": "/"})
    assert r.status_code == 302
    return c


def install(monkeypatch, adapters):
    monkeypatch.setattr(registry, "enabled_backends", lambda: list(adapters))
    monkeypatch.setattr(registry, "adapter", lambda name: adapters[name])
    monkeypatch.setattr(registry, "label", lambda name: {"claude": "Claude", "dsh": "dsh"}.get(name, name))
    return adapters


def create_body(**over):
    body = {"backend": "claude", "bot_id": "newbot", "telegram_token": TOKEN,
            "chat_id": "12345", "enabled": True}
    body.update(over)
    return body


# ── 显示名 ─────────────────────────────────────────────────────────────────

def test_显示名读写与非法值(client, monkeypatch):
    install(monkeypatch, {"claude": FakeAdapter("claude", bots=[bot("mybot")])})
    r = client.get("/api/console/bots/claude/mybot/profile")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "display_name": "Mybot"}

    r = client.put("/api/console/bots/claude/mybot/profile", json={"display_name": "新名字"})
    assert r.status_code == 200
    assert r.get_json()["ok"] is True
    assert r.get_json()["display_name"] == "新名字"

    r = client.put("/api/console/bots/claude/mybot/profile", json={"display_name": ""})
    assert r.status_code == 422
    body = r.get_json()
    assert body["code"] == "invalid" and "display_name" in body["fields"]

    r = client.put("/api/console/bots/claude/mybot/profile", json={"display_name": "x", "extra": 1})
    assert r.status_code == 422


def test_显示名未知bot与不支持(client, monkeypatch):
    install(monkeypatch, {"claude": FakeAdapter("claude")})
    r = client.get("/api/console/bots/claude/mybot/profile")
    assert r.status_code == 404 and r.get_json()["code"] == "unknown_bot"

    caps = {k: True for k in CAPABILITY_KEYS}
    caps["bot.profile"] = False
    install(monkeypatch, {"claude": FakeAdapter("claude", bots=[bot("mybot")], caps=caps)})
    r = client.get("/api/console/bots/claude/mybot/profile")
    assert r.status_code == 501 and r.get_json()["code"] == "unsupported"


# ── 启停 ───────────────────────────────────────────────────────────────────

def test_启停成功与非法值(client, monkeypatch):
    fake = FakeAdapter("claude", bots=[bot("mybot", polling=True)])
    install(monkeypatch, {"claude": fake})
    r = client.post("/api/console/bots/claude/mybot/enabled", json={"enabled": False})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["enabled"] is False and body["running"] is False
    assert fake._find("mybot")["polling"] is False

    r = client.post("/api/console/bots/claude/mybot/enabled", json={"enabled": "yes"})
    assert r.status_code == 422 and r.get_json()["code"] == "invalid"


# ── 表单、令牌校验 ───────────────────────────────────────────────────────────

def test_新建表单形状(client, monkeypatch):
    install(monkeypatch, {"claude": FakeAdapter("claude")})
    r = client.get("/api/console/create_form")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["backends"][0]["backend"] == "claude" and body["backends"][0]["can_create"] is True
    assert body["templates"]["claude"][0]["id"] == "persona"
    assert "s2-pro" in body["tts_models"]
    assert body["id_rule"]
    assert body["defaults"]["chat_id"] == "12345"


def test_校验令牌成功与格式错误(client, monkeypatch):
    install(monkeypatch, {"claude": FakeAdapter("claude")})
    r = client.post("/api/console/telegram/check", json={"telegram_token": TOKEN})
    assert r.status_code == 200
    assert r.get_json()["username"] == "newbot_bot"
    assert r.get_json()["in_use_by"] is None

    r = client.post("/api/console/telegram/check", json={"telegram_token": "bad"})
    assert r.status_code == 422
    assert r.get_json()["code"] == "invalid"
    assert "telegram_token" in r.get_json()["fields"]


# ── 新建：202 + op 形状 ─────────────────────────────────────────────────────

def test_新建受理并完成op(client, monkeypatch):
    install(monkeypatch, {"claude": FakeAdapter("claude")})
    r = client.post("/api/console/bots", json=create_body())
    assert r.status_code == 202, r.get_data(as_text=True)
    body = r.get_json()
    assert body["ok"] is True and body["op_id"]
    assert body["op_url"] == "/api/console/ops/" + body["op_id"]

    op = wait_op(body["op_id"])
    assert op["state"] == "done"
    assert op["kind"] == "create"
    assert op["backend"] == "claude" and op["bot_id"] == "newbot"
    assert op["result"]["bot_id"] == "newbot"
    assert op["result"]["username"] == "newbot_bot"
    assert op["result"]["enabled"] is True
    assert [s["step"] for s in op["steps"]] == ["check_unique", "verify_token", "check_token_unique", "create", "voice"]
    assert all(s["status"] in ("done", "skipped") for s in op["steps"])

    got = client.get("/api/console/ops/" + body["op_id"])
    assert got.status_code == 200
    assert got.get_json()["op"]["op_id"] == body["op_id"]
    assert got.get_json()["op"]["state"] == "done"


def test_新建字段错误与未知底层(client, monkeypatch):
    install(monkeypatch, {"claude": FakeAdapter("claude")})
    r = client.post("/api/console/bots", json=create_body(bot_id="Bad-ID", chat_id="x", telegram_token="bad"))
    assert r.status_code == 422
    body = r.get_json()
    assert body["code"] == "invalid"
    assert set(body["fields"]) >= {"bot_id", "chat_id", "telegram_token"}

    r = client.post("/api/console/bots", json=create_body(backend="nope"))
    assert r.status_code == 404 and r.get_json()["code"] == "unknown_backend"


# ── 切换：HTTP 翻译与参数错误 ────────────────────────────────────────────────

def test_切换参数错误(client, monkeypatch):
    install(monkeypatch, {"claude": FakeAdapter("claude", bots=[bot("mybot")]),
                          "dsh": FakeAdapter("dsh")})
    r = client.post("/api/console/bots/claude/mybot/switch", json={"to": "claude"})
    assert r.status_code == 422 and "to" in r.get_json()["fields"]
    r = client.post("/api/console/bots/claude/mybot/switch", json={"to": "nope"})
    assert r.status_code == 422 and "to" in r.get_json()["fields"]
    r = client.post("/api/console/bots/claude/mybot/switch", json={"to": "dsh", "telegram_token": "bad"})
    assert r.status_code == 422 and "telegram_token" in r.get_json()["fields"]


def test_切换受理形状(client, monkeypatch):
    install(monkeypatch, {"claude": FakeAdapter("claude"), "dsh": FakeAdapter("dsh")})
    monkeypatch.setattr(lifecycle_routes, "start_switch", lambda backend, bot_id, body, rate_key: OP_ID)
    r = client.post("/api/console/bots/claude/mybot/switch", json={"to": "dsh"})
    assert r.status_code == 202
    assert r.get_json() == {"ok": True, "op_id": OP_ID, "op_url": "/api/console/ops/" + OP_ID}


# ── 归档、恢复、删除：完整后台任务 ───────────────────────────────────────────

def test_归档恢复删除op形状(client, monkeypatch):
    install(monkeypatch, {"claude": FakeAdapter("claude", bots=[bot("mybot", polling=True)])})

    r = client.post("/api/console/bots/claude/mybot/archive", json={})
    assert r.status_code == 202
    op = wait_op(r.get_json()["op_id"])
    assert op["state"] == "done" and op["kind"] == "archive"
    assert op["result"]["archive_id"] == "arc-1"
    assert [s["step"] for s in op["steps"]] == ["stop", "archive", "refresh"]

    r = client.get("/api/console/archives")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["archives"][0]["archive_id"] == "arc-1"
    assert body["archives"][0]["backend"] == "claude"
    assert body["backends"][0]["status"] == "ok"

    r = client.post("/api/console/archives/claude/arc-1/restore", json={})
    assert r.status_code == 202
    op = wait_op(r.get_json()["op_id"])
    assert op["state"] == "done" and op["kind"] == "restore"
    assert op["result"]["bot_id"] == "mybot"
    assert op["result"]["enabled"] is False and op["result"]["running"] is False

    r = client.post("/api/console/bots/claude/mybot/delete", json={"confirm": "wrong"})
    assert r.status_code == 422 and "confirm" in r.get_json()["fields"]

    r = client.post("/api/console/bots/claude/mybot/delete", json={"confirm": "mybot"})
    assert r.status_code == 202
    op = wait_op(r.get_json()["op_id"])
    assert op["state"] == "done" and op["kind"] == "delete"
    assert op["result"]["trash"] == "system"
    assert op["result"]["trash_path"] == "/tmp/trash/mybot"


# ── 操作记录与错误码 ─────────────────────────────────────────────────────────

def test_操作列表与单条错误(client, monkeypatch):
    install(monkeypatch, {"claude": FakeAdapter("claude", bots=[bot("mybot")])})
    r = client.get("/api/console/ops?limit=0")
    assert r.status_code == 422 and r.get_json()["code"] == "invalid"
    r = client.get("/api/console/ops?limit=21")
    assert r.status_code == 422
    r = client.get("/api/console/ops")
    assert r.status_code == 200 and r.get_json()["ops"] == []

    # 格式不合法的编号与查无此编号对只读查询没有区别：一律 404 op_not_found。
    r = client.get("/api/console/ops/bad-id")
    assert r.status_code == 404 and r.get_json()["code"] == "op_not_found"
    r = client.get("/api/console/ops/" + OP_ID)
    assert r.status_code == 404 and r.get_json()["code"] == "op_not_found"


def test_重新收拾不能收拾已完成任务(client, monkeypatch):
    install(monkeypatch, {"claude": FakeAdapter("claude", bots=[bot("mybot", polling=False)])})
    r = client.post("/api/console/bots/claude/mybot/archive", json={})
    op = wait_op(r.get_json()["op_id"])
    assert op["state"] == "done"
    r = client.post("/api/console/ops/" + op["op_id"] + "/resolve", json={})
    assert r.status_code == 422 and r.get_json()["code"] == "not_resolvable"
    r = client.post("/api/console/ops/" + OP_ID + "/resolve", json={})
    assert r.status_code == 404 and r.get_json()["code"] == "op_not_found"


# ── 请求体限制与形状 ─────────────────────────────────────────────────────────

def test_请求体不是json对象(client, monkeypatch):
    install(monkeypatch, {"claude": FakeAdapter("claude", bots=[bot("mybot")])})
    r = client.post("/api/console/bots/claude/mybot/archive", data="[]",
                    content_type="application/json")
    assert r.status_code == 422
    assert r.get_json() == {"ok": False, "code": "invalid", "message": "请求体必须是 JSON 对象"}


def test_请求体超限413(client, monkeypatch):
    install(monkeypatch, {"claude": FakeAdapter("claude", bots=[bot("mybot")])})
    big = b'{"x":"' + b"a" * (64 * 1024) + b'"}'
    r = client.post("/api/console/bots/claude/mybot/archive", data=big,
                    content_type="application/json")
    assert r.status_code == 413
    assert r.get_json()["code"] == "too_large"
