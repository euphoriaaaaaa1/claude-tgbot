# -*- coding: utf-8 -*-
"""§5 切回 Claude 原生订阅（固定端点，四键全删，不经 cliproxy）。BRIEF S2 的一半。"""
import os

import pytest

from conftest import write_settings, read_settings

FOUR = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL", "ANTHROPIC_API_KEY")


def test_切回官方订阅四键全删且其余键保留(api, settings_path):
    write_settings(settings_path, {"model": "sonnet", "env": {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "ANTHROPIC_AUTH_TOKEN": "sk-test-x",
        "ANTHROPIC_MODEL": "claude-sonnet-4-5-20250929", "ANTHROPIC_API_KEY": "sk-test-y",
        "OTHER_KEY": "keep"}})
    code, body = api.post("/hub/api/claude-native/activate")
    assert code == 200, body
    assert body == {"ok": True, "active_kind": "claude_native"}
    data = read_settings(settings_path)
    env = data["env"]
    for k in FOUR:
        assert k not in env, "%s 没被删掉，claude CLI 仍会走第三方" % k
    assert env["OTHER_KEY"] == "keep"
    assert data["model"] == "sonnet"


def test_cliproxy挂着也能切回官方订阅(api, settings_path, stub):
    """§5：这正是它最有用的场景（没有 A4 探活步骤）。"""
    write_settings(settings_path, {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317"}})
    stub.set_down(True)
    code, body = api.post("/hub/api/claude-native/activate")
    assert code == 200, body


def test_切回官方订阅不碰cliproxy(api, stub):
    """反向用例：§5 明写不经过 cliproxy。"""
    n = len(stub.requests)
    assert api.post("/hub/api/claude-native/activate")[0] == 200
    assert len(stub.requests) == n


def test_切回官方订阅是幂等的(api, settings_path):
    write_settings(settings_path, {"env": {"ANTHROPIC_MODEL": "x"}})
    assert api.post("/hub/api/claude-native/activate")[0] == 200
    first = settings_path.read_text(encoding="utf-8")
    assert api.post("/hub/api/claude-native/activate")[0] == 200
    assert settings_path.read_text(encoding="utf-8") == first


def test_切回后列表判定为claude_native(api, settings_path):
    write_settings(settings_path, {"env": {"ANTHROPIC_MODEL": "x",
                                           "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317"}})
    assert api.post("/hub/api/claude-native/activate")[0] == 200
    code, body = api.get("/hub/api/provider")
    assert body["active"]["kind"] == "claude_native"
    assert body["active"]["id"] is None


@pytest.mark.parametrize("raw", ["{ 坏", '{"env": "x"}', "[]"])
def test_settings不可解析时切回返回409(api, settings_path, raw):
    settings_path.write_text(raw, encoding="utf-8")
    code, body = api.post("/hub/api/claude-native/activate")
    assert code == 409
    assert body["error"] == "settings_unparsable"
    assert settings_path.read_text(encoding="utf-8") == raw


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root 无视目录权限")
def test_目录不可写时切回返回500(api, settings_path):
    write_settings(settings_path, {"env": {}})
    os.chmod(settings_path.parent, 0o500)
    try:
        code, body = api.post("/hub/api/claude-native/activate")
    finally:
        os.chmod(settings_path.parent, 0o700)
    assert code == 500
    assert body["error"] == "settings_write_failed"


def test_settings不存在时切回也算成功(api, settings_path):
    assert not settings_path.exists()
    code, body = api.post("/hub/api/claude-native/activate")
    assert code == 200, body
