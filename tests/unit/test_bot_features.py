# -*- coding: utf-8 -*-
"""T08 功能开关（bot_features）：读写、空对象/缺失文件、前提、锁、技能显示设置与补放。

全部在临时 HOME / configs / channels 下，不碰真实服务与 ~/.claude。
"""
import json
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import bot_features  # noqa: E402
import platform_lock  # noqa: E402

BOT = "mybot"
KEYS = bot_features.KEYS


@pytest.fixture
def env(tmp_path, monkeypatch):
    home, configs, channels = tmp_path / "home", tmp_path / "configs", tmp_path / "channels"
    for p in (home, configs, channels):
        p.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(configs))
    monkeypatch.setenv("HUB_CHANNELS_DIR", str(channels))
    monkeypatch.setenv("NOVELAI_SKILL_ROOT", str(tmp_path / "novelai-skill"))
    return {"home": home, "configs": configs, "channels": channels, "tmp": tmp_path}


def make_bot(env, bot_id=BOT, voice_id=None, username="test_bot"):
    channel = env["channels"] / bot_id
    channel.mkdir(parents=True, exist_ok=True)
    lines = [
        "display_name: 测试 Bot",
        'bot_channel_path: "%s"' % channel,
        "chat_id: 987654321",
        "dispatcher_port: 17801",
        "namespace_uuid: 550e8400-e29b-41d4-a716-446655440001",
    ]
    if username:
        lines.append("telegram_username: %s" % username)
    (env["configs"] / (bot_id + ".yml")).write_text("\n".join(lines) + "\n", encoding="utf-8")
    if voice_id:
        (channel / "access.json").write_text(json.dumps({"voiceId": voice_id}), encoding="utf-8")
    return channel


def read_doc(channel):
    return json.loads((channel / "features.json").read_text(encoding="utf-8"))


def test_缺失文件读成八项全开(env):
    channel = make_bot(env)
    state = bot_features.read(str(channel))
    assert state["readable"] is True
    assert all(state["features"][k] is True for k in KEYS)
    assert not (channel / "features.json").exists()


def test_写缺失文件会创建且只开提交项(env):
    channel = make_bot(env)
    result = bot_features.write(BOT, str(channel), {"life": True})
    assert result["features"]["life"] is True
    assert all(result["features"][k] is False for k in KEYS if k != "life")
    doc = read_doc(channel)
    assert doc == {"version": 1, "default": False,
                   "features": {k: (k == "life") for k in KEYS}}
    assert (channel / "features.json").stat().st_mode & 0o777 == 0o600


def test_八项逐个读写与changed顺序(env):
    channel = make_bot(env, voice_id="v1")
    bot_features.init_features(str(channel), dict.fromkeys(KEYS, True))
    result = bot_features.write(BOT, str(channel), {"voice": False, "groups": False})
    assert result["changed"] == ["voice", "groups"]
    assert result["features"]["voice"] is False and result["features"]["groups"] is False
    again = bot_features.write(BOT, str(channel), {"voice": False})
    assert again["changed"] == []
    # 再打开 voice：有语音编号，前提满足
    on = bot_features.write(BOT, str(channel), {"voice": True})
    assert on["changed"] == ["voice"] and on["features"]["voice"] is True


def test_未知键与非布尔被拒(env):
    channel = make_bot(env)
    with pytest.raises(ValueError):
        bot_features.write(BOT, str(channel), {"nsfw": True})
    with pytest.raises(ValueError):
        bot_features.write(BOT, str(channel), {"voice": "on"})
    assert not (channel / "features.json").exists()


def test_打开语音缺编号422并给字段(env):
    channel = make_bot(env)   # 没有 access.json
    with pytest.raises(bot_features.FeaturesError) as ei:
        bot_features.write(BOT, str(channel), {"voice": True})
    e = ei.value
    assert e.code == "requirement_missing"
    assert "voice" in e.fields and "语音编号" in e.fields["voice"]
    assert not (channel / "features.json").exists()


def test_锁忙(env, monkeypatch):
    channel = make_bot(env)
    monkeypatch.setattr(bot_features, "LOCK_WAIT_SEC", 0.2)
    lock_path = bot_features.features_path(str(channel)) + ".lock"
    with platform_lock.file_lock(lock_path, timeout=1):
        with pytest.raises(bot_features.FeaturesError) as ei:
            bot_features.write(BOT, str(channel), {"life": True})
    assert ei.value.code == "busy"


def test_initial_features空数组全关(env):
    doc, skipped = bot_features.initial_features([])
    assert doc == {"version": 1, "default": False, "features": dict.fromkeys(KEYS, False)}
    assert skipped == []


def test_initial_features配voice_id自动开voice(env):
    doc, skipped = bot_features.initial_features([], "v1")
    assert doc["features"]["voice"] is True
    assert all(doc["features"][k] is False for k in KEYS if k != "voice")
    assert skipped == []


def test_initial_features勾voice但缺编号记skipped(env):
    doc, skipped = bot_features.initial_features(["voice", "groups"], None)
    assert doc["features"]["voice"] is False and doc["features"]["groups"] is True
    assert skipped[0]["key"] == "voice"
    assert skipped[0]["missing"][0]["code"] == "voice_id_missing"


def test_init_features全开与不覆盖(env):
    channel = make_bot(env)
    wrote, before = bot_features.init_features(str(channel), dict.fromkeys(KEYS, True))
    assert wrote is True
    doc = read_doc(channel)
    assert doc["default"] is True and all(doc["features"].values())
    wrote2, before2 = bot_features.init_features(str(channel), dict.fromkeys(KEYS, False))
    assert wrote2 is False and before2 == dict.fromkeys(KEYS, True)


def test_技能显示设置对齐并保留其它键(env):
    channel = make_bot(env, voice_id="v1")
    (channel / ".claude").mkdir()
    (channel / ".claude" / "settings.json").write_text(json.dumps({"other": 1}), encoding="utf-8")
    bot_features.init_features(str(channel), dict.fromkeys(KEYS, True))
    bot_features.write(BOT, str(channel), {"voice": False})
    changed = bot_features.sync_skill_visibility(BOT)
    assert "mybot-voice" in changed
    settings = json.loads((channel / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["other"] == 1
    assert settings["skillOverrides"] == {"mybot-voice": "off"}
    # 再打开：键删掉，不写无效值
    bot_features.write(BOT, str(channel), {"voice": True})
    bot_features.sync_skill_visibility(BOT)
    settings = json.loads((channel / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "skillOverrides" not in settings and settings["other"] == 1


def test_sync_skills补放缺的技能并对齐(env):
    channel = make_bot(env, voice_id="v1")
    bot_features.init_features(str(channel), dict.fromkeys(KEYS, True))
    bot_features.write(BOT, str(channel), {"groups": False})
    out = bot_features.sync_skills(BOT)
    assert sorted(out["placed"]) == ["gif", "image", "moments", "proactive", "voice"]
    skills = channel / ".claude" / "skills"
    for name in ("voice", "image", "gif", "moments", "proactive"):
        assert (skills / ("mybot-" + name) / "SKILL.md").is_file()
    assert not (skills / "mybot-group").exists()
    settings = json.loads((channel / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert settings["skillOverrides"] == {"mybot-group": "off"}
    # 再跑一次：技能齐全，不再补放
    again = bot_features.sync_skills(BOT)
    assert again["placed"] == []


def test_技能手管整只跳过补放(env):
    channel = make_bot(env, voice_id="v1")
    bot_features.init_features(str(channel), dict.fromkeys(KEYS, True))
    (channel / ".skills-manual").write_text("manual\n", encoding="utf-8")
    out = bot_features.sync_skills(BOT)
    assert out["placed"] == []
    assert "voice" in out["skipped_manual"]
    assert not (channel / ".claude" / "skills" / "mybot-voice" / "SKILL.md").exists()


def test_requirements形状(env):
    channel = make_bot(env)
    reqs = bot_features.requirements(BOT)
    assert set(reqs) == set(KEYS)
    assert reqs["voice"]["ready"] is False
    assert reqs["voice"]["missing"][0]["code"] == "voice_id_missing"
    for key in KEYS:
        assert set(reqs[key]) == {"ready", "missing", "notes"}
    (channel / "access.json").write_text(json.dumps({"voiceId": "v1"}), encoding="utf-8")
    assert bot_features.requirements(BOT)["voice"]["ready"] is True


def test_频道目录在根外拒绝写(env, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    channel = make_bot(env)
    (env["configs"] / (BOT + ".yml")).write_text(
        "display_name: 测试 Bot\nbot_channel_path: \"%s\"\ndispatcher_port: 17801\n" % outside,
        encoding="utf-8")
    with pytest.raises(bot_features.FeaturesError) as ei:
        bot_features.write(BOT, str(outside), {"life": True})
    assert ei.value.code == "internal"
    assert not (outside / "features.json").exists()
