# -*- coding: utf-8 -*-
"""T16：生活设定 / 人设 / 语音 / 群设置 / 系统参数的接口单测。

全部用临时 HOME、临时 configs 与假适配器：不连真实服务、不碰真实 ~/.claude。
"""
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import life_settings  # noqa: E402
import platform_lock  # noqa: E402
from adapters import registry  # noqa: E402
from adapters.base import (AdapterError, CAPABILITY_KEYS, check_persona_body,  # noqa: E402
                           check_voice_body, persona_version)
from moments import panel_auth, params_config, web  # noqa: E402

PASSWORD = "test-panel-pw-12345"
BOT = "mybot"
CHAT = "-1001234567890"


class FakeAdapter:
    backend = "claude"

    def __init__(self, caps=None):
        self.caps = {k: True for k in CAPABILITY_KEYS}
        if caps:
            self.caps.update(caps)
        self.life = {"bio": "旧简介", "city": "上海"}
        self.persona = {"text": "旧人设", "version": persona_version("旧人设".encode("utf-8"))}
        self.voice = {"voice_id": "v1", "tts_model": "s2-pro", "text_language": "zh", "voice_language": "zh"}
        self.group = {}
        self.calls = []

    def meta(self):
        return {"backend": "claude", "backend_version": "fake", "adapter": "fake",
                "capabilities": dict(self.caps), "effort_levels": ["low", "medium", "high"]}

    def bots(self):
        return [{"bot_id": BOT, "person": BOT, "display_name": "My Bot", "chat_id": "123456789"}]

    def get_life(self, bot_id):
        return {"fields": [{"key": "bio", "label": "一句话简介", "type": "text"}],
                "values": dict(self.life), "display_name": "My Bot"}

    def set_life(self, bot_id, values):
        self.calls.append(("set_life", dict(values)))
        self.life.update(values)
        return {"applied": True, "queued": False, "message": "已保存"}

    def get_persona(self, bot_id):
        return dict(self.persona)

    def set_persona(self, bot_id, body):
        text, version = check_persona_body(body)
        if version != self.persona["version"]:
            raise AdapterError("stale", 409, "人设刚被别处改过，请刷新后再改")
        self.persona = {"text": text, "version": persona_version(text.encode("utf-8"))}
        return {"version": self.persona["version"], "message": "已保存"}

    def get_voice(self, bot_id):
        return dict(self.voice)

    def set_voice(self, bot_id, body):
        changes = check_voice_body(body)
        self.voice.update({k: v for k, v in changes.items() if v is not None})
        for k, v in changes.items():
            if v is None:
                self.voice.pop(k, None)
        return dict(self.voice)

    def groups(self, bot_id):
        return {"username": "mybot_bot", "groups": dict(self.group)}

    def set_groups(self, bot_id, body):
        if body["join"]:
            self.group[body["chat_id"]] = dict(body.get("policy") or {})
            return {"in_group": True, "policy": dict(body.get("policy") or {})}
        self.group.pop(body["chat_id"], None)
        return {"in_group": False, "policy": None}

    def group_chat_enabled(self, bot_id):
        return True

    def discovered_groups(self, bot_id, include_joined=False):
        return {"can_read_all": True, "can_join_groups": True,
                "groups": [{"chat_id": CHAT, "title": "测试群"}], "warnings": []}

    def dm_allow_from(self):
        return {"ids": ["123456789"], "unreadable": []}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("CLAUDEBOT_CONFIG_DIR", str(tmp_path / "configs"))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(tmp_path / "configs"))
    monkeypatch.setenv("CLAUDEBOT_GROUP_CHAT_FILE", str(tmp_path / "configs" / "_group_chat.yml"))
    monkeypatch.setenv("DISPATCHER_PARTY_DIR", str(tmp_path / "party"))
    (tmp_path / "configs").mkdir()
    monkeypatch.setattr(panel_auth, "SECRET_PATH", str(tmp_path / "secrets" / "panel.env"))
    monkeypatch.setattr(panel_auth, "SESSIONS_PATH", str(tmp_path / "secrets" / "sessions.json"))
    panel_auth._fail.clear()
    yield
    panel_auth._fail.clear()


@pytest.fixture
def adapter():
    return FakeAdapter()


@pytest.fixture
def client(adapter, monkeypatch):
    monkeypatch.setattr(registry, "enabled_backends", lambda: ["claude"])
    monkeypatch.setattr(registry, "adapter", lambda name: adapter)
    monkeypatch.setattr(registry, "label", lambda name: "Claude")
    panel_auth.set_password(PASSWORD)
    web.app.config["TESTING"] = True
    c = web.app.test_client()
    r = c.post("/login", data={"password": PASSWORD, "next": "/"})
    assert r.status_code == 302
    return c


def bot_url(tail):
    return "/api/console/bots/claude/%s/%s" % (BOT, tail)


# ── 生活设定 ───────────────────────────────────────────────────────────────

def test_生活设定读取(client):
    r = client.get(bot_url("life"))
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["values"]["city"] == "上海"
    assert body["fields"][0]["key"] == "bio"


def test_生活设定保存(client, adapter):
    r = client.put(bot_url("life"), json={"values": {"bio": "新简介"}})
    assert r.status_code == 200
    assert r.get_json()["applied"] is True
    assert adapter.calls == [("set_life", {"bio": "新简介"})]


def test_生活设定缺values与非对象422(client):
    assert client.put(bot_url("life"), json={"x": 1}).status_code == 422
    assert client.put(bot_url("life"), json=[1]).status_code == 422


def test_生活设定超过256KiB回413(client):
    r = client.put(bot_url("life"), json={"values": {"bio": "x" * (257 * 1024)}})
    assert r.status_code == 413
    assert r.get_json()["code"] == "too_large"


def test_生活设定锁忙透传409(client, adapter):
    def busy(bot_id, values):
        raise AdapterError("busy", 409, "别的操作正在改这只 bot 的设定")
    adapter.set_life = busy
    r = client.put(bot_url("life"), json={"values": {"bio": "x"}})
    assert r.status_code == 409
    assert r.get_json()["code"] == "busy"


def test_生活设定能力不支持501(client, adapter):
    adapter.caps["bot.life_settings"] = False
    assert client.get(bot_url("life")).status_code == 501


def test_life_settings模块按块写回并保留其他键(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDEBOT_CONFIG_DIR", str(tmp_path / "configs"))
    path = tmp_path / "configs" / ("%s.yml" % BOT)
    path.write_text("id: %s\nchat_id: 123\nbio: 旧简介\ncity: 上海\n" % BOT, encoding="utf-8")
    got = life_settings.get_life(BOT)
    assert got["values"]["bio"] == "旧简介"
    out = life_settings.set_life(BOT, {"bio": "新简介"})
    assert out["applied"] is True
    text = path.read_text(encoding="utf-8")
    assert "bio: 新简介" in text and "city: 上海" in text and "chat_id: 123" in text


def test_life_settings锁忙(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDEBOT_CONFIG_DIR", str(tmp_path / "configs"))
    path = tmp_path / "configs" / ("%s.yml" % BOT)
    path.write_text("id: %s\nbio: 旧\n" % BOT, encoding="utf-8")
    monkeypatch.setattr(life_settings, "LOCK_WAIT_SEC", 0.05)
    with platform_lock.file_lock(str(path) + ".life.lock", timeout=1):
        with pytest.raises(life_settings.LifeSettingsError) as err:
            life_settings.set_life(BOT, {"bio": "新"})
    assert err.value.code == "busy"


def test_生活设定预置清单(client, tmp_path):
    (tmp_path / "configs" / "life_presets.json").write_text(
        '{"version": 1, "fields": {"interest_keywords": {"items": [{"label": "默认", "value": "音乐"}]}}}',
        encoding="utf-8")
    r = client.get("/api/console/life_presets")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["status"] == "ok"
    assert body["presets"]["interest_keywords"]["items"][0]["value"] == "音乐"


# ── 人设 / 语音 ──────────────────────────────────────────────────────────────

def test_人设读取与保存(client, adapter):
    body = client.get(bot_url("persona")).get_json()
    assert body["text"] == "旧人设" and len(body["version"]) == 16
    r = client.put(bot_url("persona"), json={"text": "新人设", "version": body["version"]})
    assert r.status_code == 200
    assert r.get_json()["version"] == persona_version("新人设".encode("utf-8"))


def test_人设版本冲突409_stale(client, adapter):
    r = client.put(bot_url("persona"), json={"text": "新人设", "version": "0" * 16})
    assert r.status_code == 409
    assert r.get_json()["code"] == "stale"


def test_人设空与超限422(client):
    version = client.get(bot_url("persona")).get_json()["version"]
    assert client.put(bot_url("persona"), json={"text": "  ", "version": version}).status_code == 422
    r = client.put(bot_url("persona"), json={"text": "x" * (256 * 1024 + 1), "version": version})
    assert r.status_code == 422


def test_人设请求体超过1MiB回413(client):
    r = client.put(bot_url("persona"), data=b"x" * (1024 * 1024 + 1),
                   content_type="application/json")
    assert r.status_code == 413


def test_语音读写与非法模型(client, adapter):
    body = client.get(bot_url("voice")).get_json()
    assert body["voice_id"] == "v1"
    r = client.put(bot_url("voice"), json={"voice_id": "v2"})
    assert r.status_code == 200 and r.get_json()["voice_id"] == "v2"
    r = client.put(bot_url("voice"), json={"tts_model": "bad-model"})
    assert r.status_code == 422 and r.get_json()["ok"] is False


# ── 群设置 ──────────────────────────────────────────────────────────────────

def _write_group():
    from adapters import group_config
    configs = Path(__import__("os").environ["CLAUDEBOT_CONFIG_DIR"])
    (configs / "_group_chat.yml").write_text(
        "groups:\n  - chat_id: \"%s\"\n    allowFrom: [\"123456789\"]\n    requireMention: false\n"
        % CHAT, encoding="utf-8")


def test_群清单读取(client):
    _write_group()
    r = client.get("/api/console/groups")
    assert r.status_code == 200
    body = r.get_json()
    assert body["groups"][0]["chat_id"] == CHAT
    assert body["groups"][0]["director"] == "off"
    assert body["groups"][0]["party"] == {"on": False}


def test_群发现(client, adapter):
    r = client.get(bot_url("groups/discovered"))
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["groups"][0]["chat_id"] == CHAT
    adapter.caps["bot.groups_discovered"] = False
    assert client.get(bot_url("groups/discovered")).status_code == 501


def test_群清单新增与非法群号(client):
    r = client.post("/api/console/groups", json={"chat_id": CHAT, "allowFrom": ["123456789"]})
    assert r.status_code == 200
    assert r.get_json()["created"] is True
    r = client.post("/api/console/groups", json={"chat_id": CHAT, "allowFrom": ["123456789"]})
    assert r.status_code == 200 and r.get_json()["created"] is False
    r = client.post("/api/console/groups", json={"chat_id": "123", "allowFrom": ["1"]})
    assert r.status_code == 422
    r = client.post("/api/console/groups", json={"chat_id": "-1009999999999"})
    assert r.status_code == 200
    assert r.get_json()["group"]["allowFrom"] == ["123456789"]


def test_导演开关(client):
    _write_group()
    r = client.put("/api/console/groups/%s/director" % CHAT, json={"director": "on"})
    assert r.status_code == 200 and r.get_json()["director"] == "on"
    assert client.get("/api/console/groups").get_json()["groups"][0]["director"] == "on"
    r = client.put("/api/console/groups/%s/director" % CHAT, json={"director": "on"})
    assert r.status_code == 200
    r = client.put("/api/console/groups/%s/director" % CHAT, json={"director": "maybe"})
    assert r.status_code == 422
    r = client.put("/api/console/groups/-100999/director", json={"director": "on"})
    assert r.status_code == 404


def test_聚会开关(client):
    _write_group()
    r = client.post("/api/console/groups/%s/party" % CHAT, json={"on": True, "note": "一起看电影"})
    assert r.status_code == 200 and r.get_json()["party"]["on"] is True
    r = client.post("/api/console/groups/%s/party" % CHAT, json={"on": False})
    assert r.status_code == 200 and r.get_json()["party"]["on"] is False


def test_成员进群退群与能力(client, adapter):
    _write_group()
    body = {"backend": "claude", "bot_id": BOT, "join": True, "intro": "你好"}
    r = client.post("/api/console/group/%s/members" % CHAT, json=body)
    assert r.status_code == 200 and r.get_json()["in_group"] is True
    assert adapter.group[CHAT]["intro"] == "你好"
    r = client.post("/api/console/group/%s/members" % CHAT,
                    json={"backend": "claude", "bot_id": BOT, "join": False})
    assert r.status_code == 200 and r.get_json()["in_group"] is False
    adapter.caps["bot.group_chat"] = False
    r = client.post("/api/console/group/%s/members" % CHAT, json=body)
    assert r.status_code == 501
    adapter.caps["bot.group_chat"] = True
    r = client.post("/api/console/group/-100999/members", json=body)
    assert r.status_code == 404


# ── 系统参数 ────────────────────────────────────────────────────────────────

def _write_global(tmp_path):
    path = tmp_path / "configs" / "_global.yml"
    path.write_text(
        "user_display_name: 测试\nmax_calls_per_5h: 30\n"
        "moments:\n  enabled: true\n  daily_post_limit_per_bot: 3\n"
        "jiwen:\n  tick_interval_min: 5\n",
        encoding="utf-8")
    return path


def test_系统参数读取与保存(client, tmp_path):
    path = _write_global(tmp_path)
    r = client.get("/api/params")
    assert r.status_code == 200
    body = r.get_json()
    assert body["values"]["max_calls_per_5h"] == 30
    assert body["version"] == params_config.version_of()
    r = client.post("/api/params", json={"values": {"max_calls_per_5h": 40}, "version": body["version"]})
    assert r.status_code == 200
    assert r.get_json()["changed"] == ["max_calls_per_5h"]
    assert "max_calls_per_5h: 40" in path.read_text(encoding="utf-8")


def test_系统参数版本冲突409(client, tmp_path):
    _write_global(tmp_path)
    r = client.post("/api/params", json={"values": {"max_calls_per_5h": 40}, "version": "stale"})
    assert r.status_code == 409
    assert r.get_json()["error"] == "config_stale"


def test_系统参数非法值400(client, tmp_path):
    _write_global(tmp_path)
    version = client.get("/api/params").get_json()["version"]
    r = client.post("/api/params", json={"values": {"max_calls_per_5h": 0}, "version": version})
    assert r.status_code == 400
    assert r.get_json()["error"] == "bad_body"


def test_系统参数原文遮蔽敏感值(client, tmp_path):
    path = _write_global(tmp_path)
    path.write_text(path.read_text(encoding="utf-8") + "openai_api_key: sk-secret-value\n",
                    encoding="utf-8")
    r = client.get("/api/params/raw")
    assert r.status_code == 200
    body = r.get_json()
    assert "sk-secret-value" not in body["text"]
    assert params_config.PLACEHOLDER in body["text"]
    assert "openai_api_key" in body["sensitive_paths"]


# ── 页面与入口 ─────────────────────────────────────────────────────────────

def test_参数页可打开且含导航(client):
    r = client.get("/params")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "系统参数" in html and "管理台" in html


def test_管理台含生活人设语音入口(client):
    r = client.get("/console")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "人设与生活" in html
    assert "生活设定" in html and "保存人设" in html and "保存语音" in html
    assert "tts_models" not in html or "s2-pro" in html


def test_生活页旧地址302到管理台(client):
    r = client.get("/console/bots/claude/%s/life" % BOT)
    assert r.status_code == 302
    assert r.headers["Location"] == "/console?b=claude/%s" % BOT


def test_未登录访问参数接口401(adapter, monkeypatch):
    monkeypatch.setattr(registry, "enabled_backends", lambda: ["claude"])
    monkeypatch.setattr(registry, "adapter", lambda name: adapter)
    panel_auth.set_password(PASSWORD)
    c = web.app.test_client()
    assert c.get("/api/params").status_code == 401
    assert c.get("/console/bots/claude/%s/life" % BOT).status_code == 302
