# -*- coding: utf-8 -*-
"""``ClaudeLocalAdapter.create`` 的 features 分支：切换建目标不落盘、新建落八项文档。

用假 ``claude_create.create`` 截住落盘前那一步，只看交给它的 spec；不碰磁盘、不连服务。
"""
from __future__ import annotations

import pytest

import adapters.claude_create as claude_create
import adapters.claude_local as claude_local
import bot_features
from adapters.base import AdapterError
from adapters.claude_local import ClaudeLocalAdapter
from adapters.guard import SecretToken

TELEGRAM_TOKEN = "123456789:" + "A" * 35


def _spec(**over):
    spec = {"bot_id": "switchbot", "display_name": "切换 Bot", "telegram_username": "sw_bot",
            "telegram_user_id": 12345, "chat_id": "987654321", "persona_template": "blank",
            "enabled": True, "op_id": "create-switch-1", "telegram_token": TELEGRAM_TOKEN}
    spec.update(over)
    return spec


@pytest.fixture
def captured(monkeypatch):
    """把落盘那一步换成记录器，并把新建后补技能变成无副作用。"""
    calls = []

    def fake_create(bot_id, spec, token, *, op_id, ctx):
        calls.append({"bot_id": bot_id, "spec": spec, "token": token, "op_id": op_id})
        return {"bot_id": bot_id, "steps": [], "warnings": []}

    monkeypatch.setattr(claude_create, "create", fake_create)
    monkeypatch.setattr(bot_features, "sync_skills", lambda bot_id: {})
    return calls


def test_switch_in_无features时传给create的spec不含features键(captured):
    ClaudeLocalAdapter().create(_spec(purpose="switch_in"), SecretToken(TELEGRAM_TOKEN))

    assert len(captured) == 1
    assert "features" not in captured[0]["spec"]


def test_new_无features时落成八项全关的文档(captured):
    ClaudeLocalAdapter().create(_spec(purpose="new"), SecretToken(TELEGRAM_TOKEN))

    doc = captured[0]["spec"]["features"]
    assert doc["version"] == 1 and doc["default"] is False
    assert list(doc["features"]) == list(bot_features.KEYS)
    assert all(value is False for value in doc["features"].values())


def test_features字典只开voice其余全关(captured):
    # 语音要有 voice_id 才算真的开（与新建页同一口径）
    spec = _spec(purpose="new",
                 voice={"voice_id": "v-1", "tts_model": "s2-pro"},
                 features={"voice": True, "image": False})
    ClaudeLocalAdapter().create(spec, SecretToken(TELEGRAM_TOKEN))

    state = captured[0]["spec"]["features"]["features"]
    assert state["voice"] is True
    assert all(state[key] is False for key in bot_features.KEYS if key != "voice")


def test_features含未知键时422且不调create(captured):
    with pytest.raises(AdapterError) as ei:
        ClaudeLocalAdapter().create(_spec(purpose="new", features=["unknown"]),
                                    SecretToken(TELEGRAM_TOKEN))

    assert ei.value.http_status == 422
    assert ei.value.message == claude_local.FEATURES_FIELD_MESSAGE
    assert captured == []
