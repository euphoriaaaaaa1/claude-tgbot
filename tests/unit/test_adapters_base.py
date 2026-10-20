# -*- coding: utf-8 -*-
"""adapters.base 单测：能力矩阵、错误契约、令牌、输入校验。

纯函数层，不起 Flask、不连服务、不碰真实配置；令牌全用占位值。
"""
from __future__ import annotations

import copy
import json
import pickle

import pytest

from adapters import base


# ── 能力矩阵 ─────────────────────────────────────────────────────────────────

def test_能力键无重复且两边都填全():
    assert len(base.CAPABILITY_KEYS) == len(set(base.CAPABILITY_KEYS))
    for name, table in (("claude", base.CLAUDE_CAPABILITIES), ("dsh", base.DSH_CAPABILITIES)):
        assert set(table) == set(base.CAPABILITY_KEYS), name
        assert all(isinstance(v, bool) for v in table.values()), name


def test_缺键或非布尔都按不支持():
    assert base.capability_on({}, "bot.features") is False
    assert base.capability_on({"capabilities": {}}, "bot.features") is False
    assert base.capability_on({"capabilities": {"bot.features": False}}, "bot.features") is False
    assert base.capability_on({"capabilities": {"bot.features": 1}}, "bot.features") is False
    assert base.capability_on({"capabilities": {"bot.features": "true"}}, "bot.features") is False
    assert base.capability_on({"capabilities": {"bot.features": True}}, "bot.features") is True
    assert base.capability_on(None, "bot.features") is False
    assert base.capability_on({"capabilities": []}, "bot.features") is False


def test_规范化只留已知键且只留严格true():
    raw = {"bot.features": True, "bot.notify": False, "unknown.key": True, "bot.voice": 1}
    assert base.normalize_capabilities(raw) == {"bot.features": True}
    assert base.normalize_capabilities(None) == {}


def test_不支持的能力抛501():
    with pytest.raises(base.AdapterError) as ei:
        base.require_capability({"capabilities": {}}, "bot.persona")
    assert ei.value.code == "unsupported"
    assert ei.value.http_status == 501
    assert ei.value.message == base.UNSUPPORTED_MESSAGE
    # 支持时什么都不抛
    base.require_capability({"capabilities": {"bot.persona": True}}, "bot.persona")


def test_占位适配器的meta形状():
    from adapters.claude_local import ClaudeLocalAdapter
    from adapters.dsh_http import DshHttpAdapter

    for cls, backend in ((ClaudeLocalAdapter, "claude"), (DshHttpAdapter, "dsh")):
        meta = cls().meta()
        assert meta["backend"] == backend
        assert meta["adapter"] == "placeholder"
        assert isinstance(meta["capabilities"], dict)
        assert set(meta["capabilities"]) == set(base.CAPABILITY_KEYS)
        assert tuple(meta["effort_levels"]) == base.EFFORT_LEVELS


# ── 错误类型 ─────────────────────────────────────────────────────────────────

def test_AdapterError带字段副本():
    fields = {"voice": "只能是 true 或 false"}
    err = base.AdapterError("invalid", 422, "入参不合规", fields)
    fields["voice"] = "被改掉"
    assert err.fields == {"voice": "只能是 true 或 false"}
    assert err.code == "invalid" and err.http_status == 422


def test_LifecycleError的state兜底():
    err = base.LifecycleError("internal", 500, "坏了", state="what")
    assert err.state == "needs_attention"
    assert err.steps == []
    ok = base.LifecycleError("internal", 500, "坏了", steps=[{"name": "x"}], state="rolled_back")
    assert ok.state == "rolled_back" and ok.steps == [{"name": "x"}]


def test_AdapterUnavailable未知状态按error():
    assert base.AdapterUnavailable("not_installed").message == "未安装"
    err = base.AdapterUnavailable("nonsense")
    assert err.status == "error" and err.message == "出错了"
    assert base.AdapterUnavailable("down", "自定义").message == "自定义"


# ── 令牌 ─────────────────────────────────────────────────────────────────────

TOKEN = "123456789:" + "A" * 35


def test_SecretToken不输出原文():
    token = base.SecretToken(TOKEN)
    assert token.reveal() == TOKEN
    assert repr(token) == base.HIDDEN
    assert str(token) == base.HIDDEN
    assert f"{token}" == base.HIDDEN
    assert format(token, ">20") == base.HIDDEN
    assert TOKEN not in repr(token) + str(token)


def test_SecretToken不能序列化():
    token = base.SecretToken(TOKEN)
    with pytest.raises(TypeError):
        json.dumps({"t": token})
    with pytest.raises(TypeError):
        pickle.dumps(token)
    with pytest.raises(TypeError):
        copy.copy(token)
    with pytest.raises(TypeError):
        copy.deepcopy(token)


def test_SecretToken只收合形状且报错不带原值():
    for bad in ("", "abc", "123456789:" + "A" * 200, None, 123):
        with pytest.raises(ValueError) as ei:
            base.SecretToken(bad)
        assert str(ei.value) == "令牌格式不对"


# ── bot_id / 新建 ────────────────────────────────────────────────────────────

def test_valid_bot_id():
    assert base.valid_bot_id("mybot")
    assert base.valid_bot_id("my-bot_2")
    assert base.valid_bot_id("a" * 64)
    for bad in ("", ".", "..", "a" * 65, "../x", "my bot", "机器人", None, 3, "a/b"):
        assert not base.valid_bot_id(bad), bad


def test_new_bot_id_error():
    assert base.new_bot_id_error("mybot") is None
    assert base.new_bot_id_error("ab") is None
    assert base.new_bot_id_error("a") == base.NEW_BOT_ID_MESSAGE
    assert base.new_bot_id_error("Mybot") == base.NEW_BOT_ID_MESSAGE
    assert base.new_bot_id_error("my-bot") == base.NEW_BOT_ID_MESSAGE
    assert base.new_bot_id_error("a" * 33) == base.NEW_BOT_ID_MESSAGE
    for reserved in ("jiwen", "yasuna", "bot2"):
        assert base.new_bot_id_error(reserved) == base.RESERVED_BOT_MESSAGE


def test_chat_id_error():
    assert base.chat_id_error("123456789") is None
    for bad in ("", "0123", "-1", "1.5", 123, None):
        assert base.chat_id_error(bad) == base.CHAT_ID_MESSAGE


# ── 显示名 ───────────────────────────────────────────────────────────────────

def test_display_name_error():
    assert base.display_name_error("小明") is None
    assert base.display_name_error("  小明  ") is None
    assert base.display_name_error("a" * 32) is None
    for bad in ("", "   ", "a" * 33, "小\n明", "小\t明", "\ud800", "<!--x-->"):
        assert base.display_name_error(bad) == base.DISPLAY_NAME_RULE_MESSAGE, repr(bad)
    assert base.display_name_error(3) == base.DISPLAY_NAME_TYPE_MESSAGE
    assert base.display_name_error(None) == base.DISPLAY_NAME_TYPE_MESSAGE


# ── 功能开关 ─────────────────────────────────────────────────────────────────

def test_check_features_body只查形状():
    assert base.check_features_body({"features": {"voice": True}}, None) == {"voice": True}
    with pytest.raises(base.AdapterError) as ei:
        base.check_features_body({"features": {"voice": True}, "x": 1}, None)
    assert ei.value.code == "invalid" and ei.value.http_status == 422
    for body in ([], {"features": {}}, {"features": []}, {}, {"features": {"a": 1}}):
        with pytest.raises(base.AdapterError):
            base.check_features_body(body, None)


def test_check_features_body逐键报错():
    with pytest.raises(base.AdapterError) as ei:
        base.check_features_body({"features": {"nope": True, "voice": "yes"}}, {"voice", "image"})
    assert ei.value.fields == {"nope": base.FEATURE_UNKNOWN, "voice": base.FEATURE_NOT_BOOL}
    assert ei.value.message == base.FEATURES_INVALID


# ── 群聊进退 ─────────────────────────────────────────────────────────────────

def test_check_group_body退群与进群():
    assert base.check_group_body({"chat_id": "-1001234567890", "join": False}) == {
        "chat_id": "-1001234567890", "join": False}
    out = base.check_group_body({"chat_id": "-1001234567890", "join": True,
                                 "policy": {"requireMention": True, "allowFrom": ["123"]}})
    assert out["join"] is True and out["policy"]["requireMention"] is True
    # 多余的键不进结果
    out = base.check_group_body({"chat_id": "-1001234567890", "join": True, "extra": 1})
    assert out == {"chat_id": "-1001234567890", "join": True, "policy": {}}


def test_check_group_body拒绝坏值():
    for body in (None, [], {"chat_id": "100", "join": True},
                 {"chat_id": "-1001234567890", "join": "yes"},
                 {"chat_id": "-1001234567890", "join": True, "policy": {"x": 1}},
                 {"chat_id": "-1001234567890", "join": True, "policy": {"requireMention": 1}},
                 {"chat_id": "-1001234567890", "join": True, "policy": {"allowFrom": "123"}}):
        with pytest.raises(base.AdapterError) as ei:
            base.check_group_body(body)
        assert ei.value.code == "invalid" and ei.value.http_status == 422


def test_valid_group_chat_id():
    assert base.valid_group_chat_id("-1001234567890") == "-1001234567890"
    assert base.valid_group_chat_id(-1001234567890) == "-1001234567890"
    for bad in ("-0", "-01", "100", True, False, 0, None, [], "-12345678901234567", "@group"):
        assert base.valid_group_chat_id(bad) is None, repr(bad)


# ── 投递通知 ─────────────────────────────────────────────────────────────────

def test_check_notify():
    base.check_notify("user_moment", "你好", "k1")
    for args in (("nope", "你好", "k1"), ("user_moment", "", "k1"),
                 ("user_moment", "a" * (16 * 1024 + 1), "k1"),
                 ("user_moment", "你好", ""), ("user_moment", "你好", "k" * 201),
                 ("user_moment", None, "k1")):
        with pytest.raises(base.AdapterError) as ei:
            base.check_notify(*args)
        assert ei.value.code == "invalid" and ei.value.http_status == 422


# ── 语言 / 语音 / 人设 / 归档 ────────────────────────────────────────────────

def test_language_error():
    assert base.language_error("中文") is None
    assert base.language_error(" zh ") is None
    for bad in ("", "   ", "zh\nen", "a" * 21, "[x]", "a|b", 3, None):
        assert base.language_error(bad) is not None, repr(bad)


def test_check_voice_body():
    assert base.check_voice_body({"voice_id": "v-1", "text_language": " zh "}) == {
        "voice_id": "v-1", "text_language": "zh"}
    assert base.check_voice_body({"tts_model": "s2-pro"}) == {"tts_model": "s2-pro"}
    for body in ({}, {"x": 1}, {"voice_id": "有 空格"}, {"tts_model": "nope"},
                 {"text_language": "zh\nen"}):
        with pytest.raises(base.AdapterError) as ei:
            base.check_voice_body(body)
        assert ei.value.http_status == 422


def test_check_persona_body():
    text, version = base.check_persona_body({"text": "人设", "version": "A" * 16})
    assert text == "人设" and version == "a" * 16
    for body in ({}, {"text": "", "version": "a" * 16},
                 {"text": "x", "version": "short"}, {"text": "x", "version": "a" * 16, "x": 1},
                 {"text": "a" * (256 * 1024 + 1), "version": "a" * 16}):
        with pytest.raises(base.AdapterError) as ei:
            base.check_persona_body(body)
        assert ei.value.http_status == 422


def test_shell_meta_and_archive_id():
    assert base.shell_meta_ok({"a": 1})
    assert not base.shell_meta_ok([])
    assert not base.shell_meta_ok({"a": float("nan")})
    assert not base.shell_meta_ok({"a": "x" * (64 * 1024)})
    assert base.valid_archive_id("arc-1")
    for bad in ("", ".", "..", "a" * 101, "a/b", None):
        assert not base.valid_archive_id(bad), repr(bad)
