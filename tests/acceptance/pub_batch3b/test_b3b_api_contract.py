# -*- coding: utf-8 -*-
"""pub-batch3b · 后端读接口契约：overview 的 moved_to 原样返回；读接口 404 是 unknown_bot；
后端不可达（502）不冒充 unknown_bot。

用 Flask 测试客户端起真实 moments.web（照 pub_memo_restart 的写法）：登录门、路由、错误码
都是真的，只把「适配器」这一层换成返回固定行的替身（不读实现逻辑）。
"""
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.append(str(REPO))

from b3b_helpers import CAPS_ALL  # noqa: E402

PASSWORD = "b3b-api-panel-pw-12345"
READ_ENDPOINTS = ["profile", "life", "persona", "voice", "features"]


class FakeAdapter:
    """只提供路由会用到的方法；未知 bot 统一抛 AdapterError(unknown_bot)。"""

    def __init__(self, rows, down=False):
        self.rows = rows
        self.down = down

    def meta(self):
        if self.down:
            from adapters.base import AdapterUnavailable
            raise AdapterUnavailable("down")
        return {"capabilities": dict(CAPS_ALL), "effort_levels": []}

    def providers(self):
        return {"providers": [], "default": None}

    def bots(self):
        if self.down:
            from adapters.base import AdapterUnavailable
            raise AdapterUnavailable("down")
        return list(self.rows)

    def _known(self, bot_id):
        for row in self.rows:
            if row.get("bot_id") == bot_id:
                return row
        from adapters.base import AdapterError
        raise AdapterError("unknown_bot", 404, "这个底层没有这只 bot")

    def get_persona(self, bot_id):
        self._known(bot_id)
        return {"text": "人设-" + bot_id, "version": "1"}

    def get_life(self, bot_id):
        self._known(bot_id)
        return {"fields": [], "values": {}}

    def get_voice(self, bot_id):
        self._known(bot_id)
        return {"voice_id": "voice-" + bot_id, "tts_model": "s1",
                "text_language": "zh", "voice_language": "zh"}

    def get_features(self, bot_id):
        self._known(bot_id)
        return {"keys": [], "features": {}, "requirements": {}}

    def get_profile(self, bot_id):
        self._known(bot_id)
        return {"display_name": bot_id}


@pytest.fixture
def panel(tmp_path, monkeypatch):
    """隔离 HOME/配置目录后登录的测试客户端；返回 (client, 注册适配器的函数)。"""
    for name in ("home", "configs", "channels"):
        (tmp_path / name).mkdir()
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(tmp_path / "configs"))
    monkeypatch.setenv("CLAUDEBOT_CONFIG_DIR", str(tmp_path / "configs"))
    monkeypatch.setenv("HUB_CHANNELS_DIR", str(tmp_path / "channels"))
    monkeypatch.setenv("CLAUDEBOT_CHANNELS_ROOT", str(tmp_path / "channels"))
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("HUB_ENV_FILE", str(tmp_path / "hub.env"))
    monkeypatch.setenv("CLAUDEBOT_GLOBAL_CFG", str(tmp_path / "configs" / "_global.yml"))
    monkeypatch.setenv("CLAUDEBOT_SECRETS_FILE", str(tmp_path / "configs" / "_secrets.yml"))
    monkeypatch.setenv("CONSOLE_DSH_ADAPTER_URL", "")
    (tmp_path / "configs" / "_global.yml").write_text("jiwen:\n  tick_interval_min: 5\n", encoding="utf-8")
    provider_path = tmp_path / "providers.json"
    provider_path.write_text('{"providers": [], "bindings": {}, "proxy": {}}', encoding="utf-8")
    monkeypatch.setenv("CLAUDEBOTLIFE_TEST", "1")
    monkeypatch.setenv("CLAUDEBOTLIFE_TEST_ROOT", str(tmp_path))
    monkeypatch.setenv("CLAUDEBOT_PROVIDER_PATH", str(provider_path))
    for key in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("NO_PROXY", "*")

    from adapters import registry
    from moments import panel_auth, web

    monkeypatch.setattr(panel_auth, "SECRET_PATH", str(secrets / "panel.env"))
    monkeypatch.setattr(panel_auth, "SESSIONS_PATH", str(secrets / "sessions.json"))
    panel_auth._fail.clear()
    panel_auth.set_password(PASSWORD)
    web.app.config["TESTING"] = True

    def register(adapters):
        monkeypatch.setattr(registry, "enabled_backends", lambda: list(adapters))
        monkeypatch.setattr(registry, "adapter", lambda name: adapters[name])
        monkeypatch.setattr(registry, "label", lambda name: {"claude": "Claude", "dsh": "dsh"}.get(name, name))

    client = web.app.test_client()
    assert client.post("/login", data={"password": PASSWORD, "next": "/"}).status_code == 302
    return client, register


def test_overview_把moved_to原样返回_另一个底层名_pending_null(panel):  # noqa: E901
    client, register = panel
    register({
        "claude": FakeAdapter([
            {"bot_id": "alpha", "display_name": "Alpha", "moved_to": "dsh"},
            {"bot_id": "beta", "display_name": "Beta", "moved_to": "pending"},
            {"bot_id": "gamma", "display_name": "Gamma", "moved_to": None},
        ]),
        "dsh": FakeAdapter([{"bot_id": "delta", "display_name": "Delta"}]),
    })
    r = client.get("/api/console/overview")
    assert r.status_code == 200
    bots = {b["bot_id"]: b for b in r.get_json()["bots"]}
    assert bots["alpha"]["moved_to"] == "dsh"
    assert bots["beta"]["moved_to"] == "pending"
    assert bots["gamma"].get("moved_to") is None
    assert bots["delta"].get("moved_to") is None


@pytest.mark.parametrize("endpoint", READ_ENDPOINTS)
def test_读接口未知bot_返回404且code为unknown_bot(panel, endpoint):
    client, register = panel
    register({"claude": FakeAdapter([{"bot_id": "alpha", "display_name": "Alpha"}])})
    r = client.get("/api/console/bots/claude/ghost/" + endpoint)
    assert r.status_code == 404, r.get_data(as_text=True)
    body = r.get_json()
    assert body["ok"] is False
    assert body["code"] == "unknown_bot"


@pytest.mark.parametrize("endpoint", READ_ENDPOINTS)
def test_后端不可达_读接口502且响应体不出现unknown_bot(panel, endpoint):
    client, register = panel
    register({"claude": FakeAdapter([{"bot_id": "alpha", "display_name": "Alpha"}], down=True)})
    r = client.get("/api/console/bots/claude/alpha/" + endpoint)
    assert r.status_code == 502, r.get_data(as_text=True)
    assert "unknown_bot" not in r.get_data(as_text=True)
    assert r.get_json()["code"] == "backend_down"
