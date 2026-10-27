# -*- coding: utf-8 -*-
"""T08 功能开关接口：GET/PUT 形状、422/409/413/501 与登录门。

不连真实服务：适配器是假的，目录全在临时 HOME 下。
"""
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import bot_features  # noqa: E402
from adapters import registry  # noqa: E402
from adapters.base import AdapterError, CAPABILITY_KEYS, check_features_body  # noqa: E402
from moments import panel_auth, web  # noqa: E402

PASSWORD = "test-panel-pw-12345"
BOT = "mybot"


class FakeFeaturesAdapter:
    backend = "claude"

    def __init__(self, caps=None, busy=False, unsupported=False):
        self.caps = {k: True for k in CAPABILITY_KEYS}
        if caps:
            self.caps.update(caps)
        self.busy = busy
        self.unsupported = unsupported
        self.got = None
        self.set_calls = []

    def meta(self):
        return {"backend": self.backend, "backend_version": "fake", "adapter": "fake",
                "capabilities": dict(self.caps), "effort_levels": ["low", "medium", "high"]}

    def bots(self):
        return [{"bot_id": BOT, "person": BOT}]

    def get_features(self, bot_id):
        return {"keys": bot_features.public_keys(),
                "features": dict.fromkeys(bot_features.KEYS, True),
                "requirements": {k: {"ready": True, "missing": [], "notes": []}
                                 for k in bot_features.KEYS},
                "warnings": [], "skills_synced": True}

    def set_features(self, bot_id, changes):
        check_features_body({"features": changes}, bot_features.KEYS)
        if self.busy:
            raise AdapterError("busy", 409, "别的操作正在改这只 bot 的设置，请稍后再试")
        self.set_calls.append(changes)
        feats = dict.fromkeys(bot_features.KEYS, True)
        feats.update(changes)
        return {"features": feats, "changed": sorted(changes), "applied": True, "queued": False,
                "message": "已保存", "skills_synced": True, "warnings": []}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("CLAUDEBOT_CONFIG_DIR", str(tmp_path / "configs"))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(tmp_path / "configs"))
    monkeypatch.setenv("HUB_CHANNELS_DIR", str(tmp_path / "channels"))
    monkeypatch.setattr(panel_auth, "SECRET_PATH", str(tmp_path / "secrets" / "panel.env"))
    monkeypatch.setattr(panel_auth, "SESSIONS_PATH", str(tmp_path / "secrets" / "sessions.json"))
    panel_auth._fail.clear()
    yield
    panel_auth._fail.clear()


@pytest.fixture
def client():
    panel_auth.set_password(PASSWORD)
    web.app.config["TESTING"] = True
    c = web.app.test_client()
    r = c.post("/login", data={"password": PASSWORD, "next": "/"})
    assert r.status_code == 302
    return c


def install(monkeypatch, adapter):
    monkeypatch.setattr(registry, "enabled_backends", lambda: ["claude"])
    monkeypatch.setattr(registry, "adapter", lambda name: adapter)
    monkeypatch.setattr(registry, "label", lambda name: "Claude")


def url(path=""):
    return "/api/console/bots/claude/%s/features%s" % (BOT, path)


def test_GET返回八项与前提(client, monkeypatch):
    ad = FakeFeaturesAdapter()
    install(monkeypatch, ad)
    r = client.get(url())
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert [k["key"] for k in body["keys"]] == list(bot_features.KEYS)
    assert set(body["features"]) == set(bot_features.KEYS)
    assert set(body["requirements"]) == set(bot_features.KEYS)
    assert body["skills_synced"] is True


def test_GET未登录401(client, monkeypatch):
    install(monkeypatch, FakeFeaturesAdapter())
    c = web.app.test_client()
    r = c.get(url())
    assert r.status_code == 401


def test_能力不支持回501(client, monkeypatch):
    install(monkeypatch, FakeFeaturesAdapter(caps={"bot.features": False}))
    r = client.get(url())
    assert r.status_code == 501
    assert r.get_json()["code"] == "unsupported"


def test_PUT保存成功(client, monkeypatch):
    ad = FakeFeaturesAdapter()
    install(monkeypatch, ad)
    r = client.put(url(), json={"features": {"voice": False}})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["changed"] == ["voice"]
    assert body["features"]["voice"] is False
    assert ad.set_calls == [{"voice": False}]


def test_PUT空对象422(client, monkeypatch):
    install(monkeypatch, FakeFeaturesAdapter())
    r = client.put(url(), json={"features": {}})
    assert r.status_code == 422
    assert r.get_json()["ok"] is False


def test_PUT未知键与非布尔422(client, monkeypatch):
    install(monkeypatch, FakeFeaturesAdapter())
    r = client.put(url(), json={"features": {"nsfw": True}})
    assert r.status_code == 422
    assert "nsfw" in r.get_json()["fields"]
    r = client.put(url(), json={"features": {"voice": "on"}})
    assert r.status_code == 422
    assert "voice" in r.get_json()["fields"]


def test_PUT多余键与非对象422(client, monkeypatch):
    install(monkeypatch, FakeFeaturesAdapter())
    r = client.put(url(), json={"features": {"voice": True}, "x": 1})
    assert r.status_code == 422
    r = client.put(url(), json=[1, 2])
    assert r.status_code == 422


def test_PUT超过16KiB回413(client, monkeypatch):
    install(monkeypatch, FakeFeaturesAdapter())
    body = {"features": {"voice": True}, "pad": "x" * (17 * 1024)}
    r = client.put(url(), json=body)
    assert r.status_code == 413
    assert r.get_json()["code"] == "too_large"


def test_PUT生命周期忙回409(client, monkeypatch):
    install(monkeypatch, FakeFeaturesAdapter(busy=True))
    r = client.put(url(), json={"features": {"voice": True}})
    assert r.status_code == 409
    assert r.get_json()["code"] == "busy"


def test_PUT缺前提回422并带字段(client, monkeypatch):
    class Missing(FakeFeaturesAdapter):
        def set_features(self, bot_id, changes):
            raise AdapterError("requirement_missing", 422, "打不开：缺少必需的设置",
                               {"voice": "缺语音编号"})
    install(monkeypatch, Missing())
    r = client.put(url(), json={"features": {"voice": True}})
    assert r.status_code == 422
    assert r.get_json()["code"] == "requirement_missing"
    assert "voice" in r.get_json()["fields"]


def test_底层不可用回502(client, monkeypatch):
    class Down(FakeFeaturesAdapter):
        def meta(self):
            from adapters.base import AdapterUnavailable
            raise AdapterUnavailable("down")
    install(monkeypatch, Down())
    r = client.get(url())
    assert r.status_code == 502
    assert r.get_json()["code"] == "backend_down"


def test_未知底层404(client, monkeypatch):
    install(monkeypatch, FakeFeaturesAdapter())
    r = client.get("/api/console/bots/nope/%s/features" % BOT)
    assert r.status_code == 404
    assert r.get_json()["code"] == "unknown_backend"
