# -*- coding: utf-8 -*-
"""T15：生命周期编排（新建 / 切换 / 启停 / 归档 / 恢复 / 删除、令牌转交、回退与恢复）。"""
import json
import os
import time

import pytest

import bots_registry
from adapters import lifecycle_flow, ops
from adapters.base import AdapterError, AdapterUnavailable, CAPABILITY_KEYS, LifecycleError, SecretToken

TOKEN = "123456789:" + "A" * 35


def wait_op(op_id, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        op = ops.get_op(op_id)
        if op is not None and op["state"] in ("done", "failed", "needs_attention"):
            return op
        time.sleep(0.01)
    raise AssertionError("操作没有在 %.1f 秒内结束：%r" % (timeout, ops.get_op(op_id)))


def bot(bot_id="mybot", backend="claude", polling=True, enabled=True, moved_to=None,
        username="mybot_bot", chat_id="123456789"):
    return {"bot_id": bot_id, "person": bot_id, "display_name": bot_id, "enabled": enabled,
            "state": "running" if polling else ("idle" if enabled else "stopped"),
            "polling": polling, "username": username, "moved_to": moved_to, "chat_id": chat_id,
            "backend": backend}


class FakeAdapter:
    """只实现生命周期引擎用到的调用面；所有动作都在内存里，不碰真实服务。"""

    def __init__(self, backend, bots=(), archives=(), templates=(), caps=None):
        self.backend = backend
        self._bots = [dict(b) for b in bots]
        self.archives = [dict(a) for a in archives]
        self._templates = list(templates)
        self.caps = dict(caps if caps is not None else {k: True for k in CAPABILITY_KEYS})
        self.calls = []
        self.behaviors = {}

    # ── 调用面 ──
    def meta(self):
        return {"backend": self.backend, "backend_version": "fake", "adapter": "fake",
                "capabilities": dict(self.caps), "effort_levels": ["low", "high"]}

    def bots(self):
        fn = self._behavior("bots")
        if fn is not None:
            out = fn(self)
            if out is not None:
                return out
        return [dict(b) for b in self._bots]

    def _find(self, bot_id):
        return next((b for b in self._bots if b.get("bot_id") == bot_id), None)

    def _behavior(self, name):
        return self.behaviors.get(name)

    def get_profile(self, bot_id):
        me = self._find(bot_id)
        if me is None:
            raise AdapterError("unknown_bot", 404, "没有这只 bot")
        return {"display_name": me.get("display_name") or bot_id}

    def set_profile(self, bot_id, display_name):
        me = self._find(bot_id)
        if me is None:
            raise AdapterError("unknown_bot", 404, "没有这只 bot")
        me["display_name"] = display_name
        return {"display_name": display_name, "message": "已保存"}

    def set_enabled(self, bot_id, enabled, *, op_id=None, moved_to=..., import_from=None):
        self.calls.append(("set_enabled", bot_id, enabled, moved_to, import_from))
        fn = self._behavior("set_enabled")
        if fn is not None:
            out = fn(self, bot_id, enabled, moved_to=moved_to, import_from=import_from)
            if out is not None:
                return out
        return self._set_enabled_default(bot_id, enabled, moved_to=moved_to)

    def _set_enabled_default(self, bot_id, enabled, *, moved_to=...):
        me = self._find(bot_id)
        if me is None:
            raise AdapterError("unknown_bot", 404, "没有这只 bot")
        if enabled and me.get("moved_to"):
            raise AdapterError("moved", 409, "这只 bot 已迁出，不能再启用")
        if moved_to is not ...:
            me["moved_to"] = moved_to
        me["enabled"] = enabled
        me["polling"] = enabled
        me["state"] = "running" if enabled else "stopped"
        return {"enabled": enabled, "running": enabled, "message": "已启用" if enabled else "已停用"}

    def archive(self, bot_id, shell_meta, *, op_id=None):
        self.calls.append(("archive", bot_id, shell_meta, op_id))
        fn = self._behavior("archive")
        if fn is not None:
            return fn(self, bot_id, shell_meta, op_id)
        me = self._find(bot_id)
        if me is None:
            raise AdapterError("unknown_bot", 404, "没有这只 bot")
        self._bots.remove(me)
        archive_id = bot_id + "-20260101-000000"
        self.archives.append({"archive_id": archive_id, "bot_id": bot_id, "person": bot_id,
                              "display_name": bot_id, "archived_at": "2026-01-01T00:00:00Z",
                              "status": "complete", "shell_meta": shell_meta})
        return {"archive_id": archive_id, "message": "已归档",
                "steps": [{"step": "archive", "status": "done"}]}

    def list_archives(self):
        return [dict(a) for a in self.archives]

    def restore(self, archive_id, *, op_id=None):
        self.calls.append(("restore", archive_id, op_id))
        fn = self._behavior("restore")
        if fn is not None:
            return fn(self, archive_id, op_id)
        item = next((a for a in self.archives if a.get("archive_id") == archive_id), None)
        if item is None:
            raise AdapterError("unknown_archive", 404, "没有这份归档")
        self.archives.remove(item)
        me = bot(item["bot_id"], polling=False, enabled=False, username=None)
        self._bots.append(me)
        return {"bot_id": item["bot_id"], "enabled": False, "running": False,
                "message": "已恢复", "steps": [{"step": "restore", "status": "done"}]}

    def delete(self, bot_id, confirm, *, op_id=None):
        self.calls.append(("delete", bot_id, confirm, op_id))
        fn = self._behavior("delete")
        if fn is not None:
            return fn(self, bot_id, confirm, op_id)
        me = self._find(bot_id)
        if me is None:
            raise AdapterError("unknown_bot", 404, "没有这只 bot")
        self._bots.remove(me)
        return {"message": "已删除", "trash": "trash", "trash_path": "~/.claude/bot-trash/x",
                "steps": [{"step": "delete", "status": "done"}]}

    def create(self, spec, token):
        self.calls.append(("create", dict(spec), token))
        fn = self._behavior("create")
        if fn is not None:
            return fn(self, spec, token)
        if not isinstance(token, SecretToken):
            raise AdapterError("invalid", 422, "令牌格式不对")
        bot_id = spec["bot_id"]
        enabled = spec.get("enabled") is True
        self._bots.append(bot(bot_id, backend=self.backend, polling=enabled, enabled=enabled,
                              username=spec.get("telegram_username") or "mybot_bot",
                              chat_id=spec.get("chat_id")))
        return {"bot_id": bot_id, "person": spec.get("person") or bot_id,
                "username": spec.get("telegram_username") or "mybot_bot",
                "enabled": enabled, "running": enabled, "message": "",
                "steps": [{"step": "create", "status": "done"}]}

    def take_token(self, bot_id, op):
        self.calls.append(("take_token", bot_id))
        fn = self._behavior("take_token")
        if fn is not None:
            return fn(self, bot_id, op)
        return SecretToken(TOKEN)

    def resolve(self, op):
        fn = self._behavior("resolve")
        if fn is not None:
            return fn(self, op)
        raise NotImplementedError

    def templates(self):
        return list(self._templates)

    def chat_id_default(self):
        return "123456789"

    def set_voice(self, bot_id, body):
        self.calls.append(("set_voice", bot_id, dict(body)))
        return dict(body)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("CLAUDEBOT_CONFIG_DIR", str(tmp_path / "configs"))
    monkeypatch.setenv("CLAUDEBOT_CHANNELS_ROOT", str(tmp_path / "channels"))
    (tmp_path / "configs").mkdir()
    (tmp_path / "channels").mkdir()
    lifecycle_flow.use_telegram_get_me(
        lambda token, rate_key=None: {"username": "mybot_bot", "bot_user_id": 42})
    monkeypatch.setattr(bots_registry, "reload", lambda: None)
    yield tmp_path
    lifecycle_flow.use_telegram_get_me(None)


@pytest.fixture
def reg(monkeypatch):
    adapters = {}

    def adapter(name):
        if name not in adapters:
            raise AdapterUnavailable("not_installed")
        return adapters[name]

    monkeypatch.setattr(lifecycle_flow.registry, "adapter", adapter)
    monkeypatch.setattr(lifecycle_flow.registry, "enabled_backends", lambda: list(adapters))
    monkeypatch.setattr(lifecycle_flow.registry, "label", lambda name: name)
    return adapters


def body(**over):
    data = {"backend": "claude", "bot_id": "mybot", "telegram_token": TOKEN, "chat_id": "123456789",
            "persona_text": "你是一只测试 bot", "enabled": True}
    data.update(over)
    return data


# ── 新建 ────────────────────────────────────────────────────────────────────

def test_create_success(reg):
    reg["claude"] = FakeAdapter("claude")
    op_id = lifecycle_flow.start_create(body(), "rk")
    op = wait_op(op_id)
    assert op["state"] == "done", op
    result = op["result"]
    assert result["bot_id"] == "mybot"
    assert result["person"] == "mybot"
    assert result["username"] == "mybot_bot"
    assert result["enabled"] is True and result["running"] is True
    assert result["steps"]
    assert {s["step"] for s in op["steps"]} == {"check_unique", "verify_token", "check_token_unique",
                                                "create", "voice"}
    assert reg["claude"]._find("mybot") is not None


def test_create_invalid_token_is_rejected(reg):
    reg["claude"] = FakeAdapter("claude")
    with pytest.raises(lifecycle_flow.Reject) as exc:
        lifecycle_flow.start_create(body(telegram_token="not-a-token"), "rk")
    assert exc.value.code == "invalid"
    assert exc.value.http_status == 422
    assert "telegram_token" in exc.value.fields
    assert ops.list_ops(active=True) == []


def test_create_existing_bot_fails(reg):
    reg["claude"] = FakeAdapter("claude", bots=[bot()])
    # INTERFACE §2.4：编号已被占用在受理期就 409，不再生成注定失败的任务。
    with pytest.raises(lifecycle_flow.Reject) as exc:
        lifecycle_flow.start_create(body(), "rk")
    assert exc.value.code == "exists" and exc.value.http_status == 409


def test_create_adapter_rollback_is_reported(reg):
    fake = FakeAdapter("claude")
    fake.behaviors["create"] = lambda self, spec, token: (_ for _ in ()).throw(
        LifecycleError("step_failed", 500, "新建没做完", state="rolled_back"))
    reg["claude"] = fake
    op = wait_op(lifecycle_flow.start_create(body(), "rk"))
    assert op["state"] == "failed"
    assert op["code"] == "step_failed"
    rec = ops._load(op["op_id"])
    assert rec["state"] == "rolled_back"


def test_create_check_token_rejects_username_in_use(reg):
    reg["claude"] = FakeAdapter("claude", bots=[bot("other", username="mybot_bot")])
    op = wait_op(lifecycle_flow.start_create(body(), "rk"))
    assert op["state"] == "failed"
    assert op["code"] == "token_in_use"


def test_create_needs_attention_then_resolve(reg):
    fake = FakeAdapter("claude")
    fake.behaviors["create"] = lambda self, spec, token: (_ for _ in ()).throw(
        LifecycleError("step_failed", 500, "新建停在中间", state="needs_attention"))
    fake.behaviors["resolve"] = lambda self, rec: {
        "state": "committed", "bot_id": "mybot", "person": "mybot",
        "username": "mybot_bot", "enabled": True, "running": True}
    reg["claude"] = fake
    op = wait_op(lifecycle_flow.start_create(body(), "rk"))
    assert op["state"] == "needs_attention"
    ops.resolve(op["op_id"])
    op = wait_op(op["op_id"])
    assert op["state"] == "done"
    assert op["result"]["bot_id"] == "mybot"


# ── 归档 ────────────────────────────────────────────────────────────────────

def test_archive_success(reg):
    fake = FakeAdapter("claude", bots=[bot(polling=True)])
    reg["claude"] = fake
    op = wait_op(lifecycle_flow.start_archive("claude", "mybot"))
    assert op["state"] == "done", op
    assert op["result"]["archive_id"] == "mybot-20260101-000000"
    assert op["result"]["steps"] == [{"step": "archive", "status": "done"}]
    assert fake._find("mybot") is None
    assert fake.archives[0]["shell_meta"]["was_enabled"] is True
    assert fake.archives[0]["shell_meta"]["op_id"] == op["op_id"]


def test_archive_failure_restarts_source(reg):
    fake = FakeAdapter("claude", bots=[bot(polling=True)])
    fake.behaviors["archive"] = lambda self, bot_id, meta, op_id: (_ for _ in ()).throw(
        AdapterError("internal", 500, "归档写不进去"))
    reg["claude"] = fake
    op = wait_op(lifecycle_flow.start_archive("claude", "mybot"))
    assert op["state"] == "failed"
    assert op["code"] == "internal"
    assert fake._find("mybot")["polling"] is True


def test_archive_needs_attention_then_resolve(reg):
    fake = FakeAdapter("claude", bots=[bot(polling=True)])
    fake.behaviors["archive"] = lambda self, bot_id, meta, op_id: (_ for _ in ()).throw(
        LifecycleError("step_failed", 500, "停在中间", state="needs_attention"))
    fake.behaviors["resolve"] = lambda self, rec: {"state": "rolled_back", "message": "已撤回"}
    reg["claude"] = fake
    op = wait_op(lifecycle_flow.start_archive("claude", "mybot"))
    assert op["state"] == "needs_attention"
    assert op["needs_attention"] is True
    ops.resolve(op["op_id"])
    op = wait_op(op["op_id"])
    assert op["state"] == "failed"
    assert fake._find("mybot")["polling"] is True


# ── 恢复 ────────────────────────────────────────────────────────────────────

def _archive_item(bot_id="mybot", was_enabled=True):
    return {"archive_id": bot_id + "-20260101-000000", "bot_id": bot_id, "person": bot_id,
            "display_name": bot_id, "archived_at": "2026-01-01T00:00:00Z", "status": "complete",
            "shell_meta": {"op_id": "20260101T000000Z-archive-deadbeef", "was_enabled": was_enabled,
                           "username": "mybot_bot"}}


def test_restore_success_stays_stopped(reg):
    fake = FakeAdapter("claude", archives=[_archive_item()])
    reg["claude"] = fake
    op = wait_op(lifecycle_flow.start_restore("claude", "mybot-20260101-000000"))
    assert op["state"] == "done", op
    result = op["result"]
    assert result["bot_id"] == "mybot"
    assert result["enabled"] is False and result["running"] is False
    assert result["steps"] == [{"step": "restore", "status": "done"}]
    assert fake._find("mybot") is not None
    assert fake._find("mybot")["polling"] is False


def test_restore_unknown_archive_fails(reg):
    reg["claude"] = FakeAdapter("claude")
    op = wait_op(lifecycle_flow.start_restore("claude", "ghost-20260101-000000"))
    assert op["state"] == "failed"
    assert op["code"] == "unknown_archive"


def test_restore_needs_attention_then_resolve(reg):
    fake = FakeAdapter("claude", archives=[_archive_item()])
    fake.behaviors["restore"] = lambda self, archive_id, op_id: (_ for _ in ()).throw(
        LifecycleError("step_failed", 500, "恢复停在中间", state="needs_attention"))
    fake.behaviors["resolve"] = lambda self, rec: {"state": "committed", "bot_id": "mybot"}
    reg["claude"] = fake
    op = wait_op(lifecycle_flow.start_restore("claude", "mybot-20260101-000000"))
    assert op["state"] == "needs_attention"
    ops.resolve(op["op_id"])
    op = wait_op(op["op_id"])
    assert op["state"] == "done"
    assert op["result"]["enabled"] is False


# ── 删除 ────────────────────────────────────────────────────────────────────

def test_delete_requires_exact_confirm(reg):
    reg["claude"] = FakeAdapter("claude", bots=[bot()])
    with pytest.raises(lifecycle_flow.Reject) as exc:
        lifecycle_flow.start_delete("claude", "mybot", "mibot")
    assert exc.value.code == "invalid"
    assert exc.value.http_status == 422
    assert "confirm" in exc.value.fields
    assert ops.list_ops(active=True) == []
    assert reg["claude"]._find("mybot") is not None


def test_delete_success(reg):
    fake = FakeAdapter("claude", bots=[bot(polling=True)])
    reg["claude"] = fake
    op = wait_op(lifecycle_flow.start_delete("claude", "mybot", "mybot"))
    assert op["state"] == "done", op
    assert op["result"]["trash"] == "trash"
    assert op["result"]["trash_path"] == "~/.claude/bot-trash/x"
    assert op["result"]["steps"] == [{"step": "delete", "status": "done"}]
    assert fake._find("mybot") is None


def test_delete_failure_restarts_source(reg):
    fake = FakeAdapter("claude", bots=[bot(polling=True)])
    fake.behaviors["delete"] = lambda self, bot_id, confirm, op_id: (_ for _ in ()).throw(
        AdapterError("internal", 500, "删除没做成"))
    reg["claude"] = fake
    op = wait_op(lifecycle_flow.start_delete("claude", "mybot", "mybot"))
    assert op["state"] == "failed"
    assert fake._find("mybot")["polling"] is True


def test_delete_needs_attention_then_resolve(reg):
    fake = FakeAdapter("claude", bots=[bot(polling=True)])
    fake.behaviors["delete"] = lambda self, bot_id, confirm, op_id: (_ for _ in ()).throw(
        LifecycleError("step_failed", 500, "删除停在中间", state="needs_attention"))
    fake.behaviors["resolve"] = lambda self, rec: {
        "state": "committed", "wiped": True, "trash": "trash", "trash_path": "~/.claude/bot-trash/x"}
    reg["claude"] = fake
    op = wait_op(lifecycle_flow.start_delete("claude", "mybot", "mybot"))
    assert op["state"] == "needs_attention"
    ops.resolve(op["op_id"])
    op = wait_op(op["op_id"])
    assert op["state"] == "done"
    assert op["result"]["trash"] == "trash"


# ── 切换 ────────────────────────────────────────────────────────────────────

def test_switch_handoff_token_success(reg):
    src = FakeAdapter("claude", bots=[bot(polling=True)])
    dst = FakeAdapter("dsh")
    reg["claude"], reg["dsh"] = src, dst
    op = wait_op(lifecycle_flow.start_switch("claude", "mybot", {"to": "dsh"}, "rk"))
    assert op["state"] == "done", op
    assert op["result"]["from"] == {"backend": "claude", "bot_id": "mybot"}
    assert op["result"]["to"] == {"backend": "dsh", "bot_id": "mybot"}
    assert op["result"]["token_source"] == "handoff"
    assert op["result"]["imported"] == {"messages": 0, "promises": 0}
    assert ("take_token", "mybot") in src.calls
    assert src._find("mybot")["polling"] is False
    assert src._find("mybot")["moved_to"] == "dsh"
    assert dst._find("mybot")["polling"] is True
    assert any(c[0] == "set_enabled" and c[4] == "mybot" for c in dst.calls)
    raw = open(os.path.join(ops.ops_dir(), op["op_id"] + ".json"), encoding="utf-8").read()
    assert TOKEN not in raw


def test_switch_form_token_success(reg):
    src = FakeAdapter("claude", bots=[bot(polling=True)])
    dst = FakeAdapter("dsh")
    reg["claude"], reg["dsh"] = src, dst
    op = wait_op(lifecycle_flow.start_switch(
        "claude", "mybot", {"to": "dsh", "telegram_token": TOKEN}, "rk"))
    assert op["state"] == "done", op
    assert op["result"]["token_source"] == "form"
    assert not any(c[0] == "take_token" for c in src.calls)


def test_switch_target_existing_uses_no_token(reg):
    src = FakeAdapter("claude", bots=[bot(polling=True)])
    dst = FakeAdapter("dsh", bots=[bot(backend="dsh", polling=False, enabled=False)])
    reg["claude"], reg["dsh"] = src, dst
    op = wait_op(lifecycle_flow.start_switch("claude", "mybot", {"to": "dsh"}, "rk"))
    assert op["state"] == "done", op
    assert op["result"]["token_source"] == "none"
    assert not any(c[0] == "take_token" for c in src.calls)
    assert not any(c[0] == "create" for c in dst.calls)


def test_switch_moved_source_is_rejected(reg):
    reg["claude"] = FakeAdapter("claude", bots=[bot(polling=False, moved_to="dsh")])
    reg["dsh"] = FakeAdapter("dsh")
    op = wait_op(lifecycle_flow.start_switch("claude", "mybot", {"to": "dsh"}, "rk"))
    assert op["state"] == "failed"
    assert op["code"] == "moved"


def test_switch_other_side_running_is_rejected(reg):
    reg["claude"] = FakeAdapter("claude", bots=[bot(polling=False)])
    reg["dsh"] = FakeAdapter("dsh", bots=[bot(backend="dsh", polling=True)])
    op = wait_op(lifecycle_flow.start_switch("claude", "mybot", {"to": "dsh"}, "rk"))
    assert op["state"] == "failed"
    assert op["code"] == "other_side_running"


def test_switch_both_sides_running_is_conflict(reg):
    reg["claude"] = FakeAdapter("claude", bots=[bot(polling=True)])
    reg["dsh"] = FakeAdapter("dsh", bots=[bot(backend="dsh", polling=True)])
    op = wait_op(lifecycle_flow.start_switch("claude", "mybot", {"to": "dsh"}, "rk"))
    assert op["state"] == "failed"
    assert op["code"] == "conflict"


def test_switch_stop_source_failure_rolls_back(reg):
    src = FakeAdapter("claude", bots=[bot(polling=True)])
    dst = FakeAdapter("dsh")

    def stop(self, bot_id, enabled, *, moved_to=..., import_from=None):
        if enabled is False and moved_to == "pending":
            raise AdapterError("still_running", 503, "停不下来")
        return self._set_enabled_default(bot_id, enabled, moved_to=moved_to)

    src.behaviors["set_enabled"] = stop
    reg["claude"], reg["dsh"] = src, dst
    op = wait_op(lifecycle_flow.start_switch("claude", "mybot", {"to": "dsh"}, "rk"))
    assert op["state"] == "failed"
    assert op["code"] == "still_running"
    assert src._find("mybot")["polling"] is True
    assert dst._find("mybot")["polling"] is False


def test_switch_verify_failure_rolls_back(reg):
    src = FakeAdapter("claude", bots=[bot(polling=True)])
    dst = FakeAdapter("dsh")

    def start(self, bot_id, enabled, *, moved_to=..., import_from=None):
        out = self._set_enabled_default(bot_id, enabled, moved_to=moved_to)
        if enabled:
            me = self._find(bot_id)
            me["polling"] = False
            me["state"] = "idle"
            return {"enabled": True, "running": False, "message": "调度器还没起来"}
        return out

    dst.behaviors["set_enabled"] = start
    reg["claude"], reg["dsh"] = src, dst
    op = wait_op(lifecycle_flow.start_switch("claude", "mybot", {"to": "dsh"}, "rk"))
    assert op["state"] == "failed"
    assert src._find("mybot")["polling"] is True
    assert dst._find("mybot")["polling"] is False


def test_switch_needs_attention_then_resolve(reg):
    src = FakeAdapter("claude", bots=[bot(polling=True)])
    dst = FakeAdapter("dsh")
    attempts = {"mark": 0}

    def mark(self, bot_id, enabled, *, moved_to=..., import_from=None):
        if enabled is False and moved_to == "dsh" and attempts["mark"] == 0:
            attempts["mark"] += 1
            raise AdapterError("internal", 500, "已迁出标记没写成")
        return self._set_enabled_default(bot_id, enabled, moved_to=moved_to)

    src.behaviors["set_enabled"] = mark
    reg["claude"], reg["dsh"] = src, dst
    op = wait_op(lifecycle_flow.start_switch("claude", "mybot", {"to": "dsh"}, "rk"))
    assert op["state"] == "needs_attention"
    assert op["result"]["to"] == {"backend": "dsh", "bot_id": "mybot"}
    ops.resolve(op["op_id"])
    op = wait_op(op["op_id"])
    assert op["state"] == "done"
    assert src._find("mybot")["moved_to"] == "dsh"


def test_switch_crash_recovery_rolls_back(reg):
    src = FakeAdapter("claude", bots=[bot(polling=False, moved_to="pending")])
    dst = FakeAdapter("dsh", bots=[bot(backend="dsh", polling=False, enabled=False)])
    reg["claude"], reg["dsh"] = src, dst
    op_id = ops.new_op_id("switch")
    keys = ["check", "take_token", "create_target", "stop_source", "start_target",
            "verify", "mark_moved", "refresh"]
    rec = ops._new_record(op_id, "switch", "claude", "mybot", "mybot", None,
                          {"backend": "dsh", "bot_id": "mybot"}, keys)
    rec["status"] = "running"
    ops._set_step(rec, "check", "done")
    ops._set_step(rec, "take_token", "skipped")
    ops._set_step(rec, "create_target", "done")
    ops._set_step(rec, "stop_source", "running")
    inner = ops._inner(rec)
    inner["doubt"] = "stop_source"
    inner["data"].update(source_polling=True, target_bot_id="mybot", target_existing=False)
    ops._write(rec)
    handled = ops.recover_orphans(wait=0.1, poll=0.01)
    assert handled == [op_id]
    assert ops.get_op(op_id)["state"] == "failed"
    assert src._find("mybot")["polling"] is True
    assert src._find("mybot")["moved_to"] is None
    assert dst._find("mybot")["polling"] is False


# ── 启停 ────────────────────────────────────────────────────────────────────

def test_disable_success(reg):
    fake = FakeAdapter("claude", bots=[bot(polling=True)])
    reg["claude"] = fake
    out = lifecycle_flow.set_enabled("claude", "mybot", False)
    assert out == {"enabled": False, "running": False, "message": "已停用"}
    assert fake._find("mybot")["polling"] is False


def test_enable_blocked_by_other_side_running(reg):
    reg["claude"] = FakeAdapter("claude", bots=[bot(polling=False)])
    reg["dsh"] = FakeAdapter("dsh", bots=[bot(backend="dsh", polling=True)])
    with pytest.raises(lifecycle_flow.Reject) as exc:
        lifecycle_flow.set_enabled("claude", "mybot", True)
    assert exc.value.code == "other_side_running"
    assert exc.value.http_status == 409


def test_enable_other_side_unknown_needs_confirmation(reg):
    src = FakeAdapter("claude", bots=[bot(polling=False)])
    dst = FakeAdapter("dsh")

    def down(self):
        raise AdapterUnavailable("down", "连不上")

    dst.behaviors["bots"] = down
    reg["claude"], reg["dsh"] = src, dst
    with pytest.raises(lifecycle_flow.Reject) as exc:
        lifecycle_flow.set_enabled("claude", "mybot", True)
    assert exc.value.code == "other_side_unknown"
    out = lifecycle_flow.set_enabled("claude", "mybot", True, confirm_other_side_unknown=True)
    assert out["enabled"] is True


def test_enable_moved_bot_is_rejected(reg):
    reg["claude"] = FakeAdapter("claude", bots=[bot(polling=False, moved_to="dsh")])
    with pytest.raises(lifecycle_flow.Reject) as exc:
        lifecycle_flow.set_enabled("claude", "mybot", True)
    assert exc.value.code == "moved"


def test_lifecycle_lock_busy(reg):
    reg["claude"] = FakeAdapter("claude", bots=[bot(polling=True)])
    lock = ops.acquire(ops.new_op_id("probe"), persons=("someone",))
    try:
        with pytest.raises(lifecycle_flow.Reject) as exc:
            lifecycle_flow.start_archive("claude", "mybot")
        assert exc.value.code == "busy"
        with pytest.raises(lifecycle_flow.Reject) as exc:
            lifecycle_flow.set_enabled("claude", "mybot", False)
        assert exc.value.code == "busy"
    finally:
        lock.release()


# ── 归档清单与显示名 ─────────────────────────────────────────────────────────

def test_list_archives_merges_backends(reg):
    reg["claude"] = FakeAdapter("claude", archives=[_archive_item()])
    reg["dsh"] = FakeAdapter("dsh")
    out = lifecycle_flow.list_archives()
    assert [a["archive_id"] for a in out["archives"]] == ["mybot-20260101-000000"]
    assert out["archives"][0]["was_enabled"] is True
    assert [b["backend"] for b in out["backends"]] == ["claude", "dsh"]


def test_display_name_read_and_write(reg):
    fake = FakeAdapter("claude", bots=[bot()])
    reg["claude"] = fake
    assert lifecycle_flow.get_display_name("claude", "mybot") == "mybot"
    out = lifecycle_flow.save_display_name("claude", "mybot", "新名字")
    assert out == {"display_name": "新名字", "message": "已保存"}
    out = lifecycle_flow.save_display_name("claude", "mybot", "新名字")
    assert out == {"display_name": "新名字", "message": "没有改动"}
