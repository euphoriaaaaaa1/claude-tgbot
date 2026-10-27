# -*- coding: utf-8 -*-
"""T07 总览页与总览接口：页面静态检查、重定向、总览形状与底层故障隔离。"""
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters import registry  # noqa: E402
from adapters.base import AdapterError, AdapterUnavailable, CAPABILITY_KEYS  # noqa: E402
from moments import panel_auth, web  # noqa: E402

PASSWORD = "test-panel-pw-12345"


class FakeAdapter:
    backend = "claude"

    def __init__(self, backend="claude", bots=(), providers=None, meta_error=None, providers_error=None):
        self.backend = backend
        self._bots = [dict(b) for b in bots]
        self._providers = providers if providers is not None else {"providers": [], "default": None}
        self._meta_error = meta_error
        self._providers_error = providers_error
        self.caps = {k: True for k in CAPABILITY_KEYS}

    def meta(self):
        if self._meta_error is not None:
            raise self._meta_error
        return {"backend": self.backend, "backend_version": "fake", "adapter": "fake",
                "capabilities": dict(self.caps), "effort_levels": ["low", "medium", "high", "xhigh", "max"]}

    def bots(self):
        return [dict(b) for b in self._bots]

    def providers(self):
        if self._providers_error is not None:
            raise self._providers_error
        return dict(self._providers)


def bot(bot_id="mybot", backend="claude", polling=True, enabled=True, provider=None, source="default"):
    return {"bot_id": bot_id, "person": bot_id, "display_name": bot_id.title(), "enabled": enabled,
            "state": "running" if polling else ("idle" if enabled else "stopped"), "state_text": "",
            "polling": polling, "username": bot_id + "_bot", "provider": provider, "provider_source": source,
            "pending": None, "detail": {}, "moved_to": None, "backend": backend}


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("CLAUDEBOT_CONFIG_DIR", str(tmp_path / "configs"))
    monkeypatch.setenv("CLAUDEBOT_CHANNELS_ROOT", str(tmp_path / "channels"))
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


def install(monkeypatch, adapters):
    monkeypatch.setattr(registry, "enabled_backends", lambda: list(adapters))
    monkeypatch.setattr(registry, "adapter", lambda name: adapters[name])
    monkeypatch.setattr(registry, "label", lambda name: {"claude": "Claude", "dsh": "dsh"}.get(name, name))


def test_总览形状与字段(client, monkeypatch):
    provider = {"id": "p1", "name": "Provider", "model": "m1", "effort": "high"}
    install(monkeypatch, {
        "claude": FakeAdapter("claude", bots=[bot("mybot", provider=provider, source="override")]),
        "dsh": FakeAdapter("dsh", bots=[]),
    })
    r = client.get("/api/console/overview")
    assert r.status_code == 200
    data = r.get_json()
    assert [b["backend"] for b in data["backends"]] == ["claude", "dsh"]
    claude = data["backends"][0]
    assert claude["label"] == "Claude"
    assert claude["status"] == "ok"
    assert claude["effort_levels"] == ["low", "medium", "high", "xhigh", "max"]
    assert "providers_message" in claude
    item = data["bots"][0]
    for key in ("backend", "bot_id", "person", "display_name", "username", "enabled", "polling",
                "state", "state_text", "provider", "provider_source", "pending", "detail",
                "moved_to", "conflict"):
        assert key in item
    assert item["provider"] == provider
    assert item["provider_source"] == "override"
    assert item["conflict"] is False


def test_底层不可用只影响自己(client, monkeypatch):
    install(monkeypatch, {
        "claude": FakeAdapter("claude", bots=[bot("mybot")]),
        "dsh": FakeAdapter("dsh", meta_error=AdapterUnavailable("down")),
    })
    data = client.get("/api/console/overview").get_json()
    dsh = [b for b in data["backends"] if b["backend"] == "dsh"][0]
    assert dsh["status"] == "down"
    assert dsh["message"]
    assert [b["bot_id"] for b in data["bots"]] == ["mybot"]


def test_供应商读取失败不连累bot(client, monkeypatch):
    install(monkeypatch, {
        "claude": FakeAdapter("claude", bots=[bot("mybot")],
                              providers_error=AdapterError("backend_down", 502, "供应商清单读不出来")),
    })
    data = client.get("/api/console/overview").get_json()
    claude = data["backends"][0]
    assert claude["status"] == "ok"
    assert claude["providers"] == []
    assert claude["providers_message"] == "供应商清单读不出来"
    assert data["bots"][0]["bot_id"] == "mybot"


def test_两边同时在收消息标conflict(client, monkeypatch):
    install(monkeypatch, {
        "claude": FakeAdapter("claude", bots=[bot("mybot", polling=True)]),
        "dsh": FakeAdapter("dsh", bots=[bot("mybot", backend="dsh", polling=True)]),
    })
    data = client.get("/api/console/overview").get_json()
    assert all(b["conflict"] is True for b in data["bots"])


def test_总览内部错误500形状(client, monkeypatch):
    from moments import console_routes
    monkeypatch.setattr(console_routes, "_probe_all", lambda names: (_ for _ in ()).throw(RuntimeError("boom")))
    r = client.get("/api/console/overview")
    assert r.status_code == 500
    assert r.get_json() == {"ok": False, "code": "internal", "message": "服务器内部错误"}


def test_console页面静态检查(client):
    r = client.get("/console")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    for needle in ('data-role="new-bot"', 'data-role="create-dialog"',
                   'data-role="delete-confirm-input"', 'data-role="op-progress"',
                   'data-role="backend-section"', 'data-role="restore"',
                   "/api/console/overview"):
        assert needle in html
    for bad in ('src="http://', 'src="https://', 'href="http://', 'href="https://',
                "@import", "url(http"):
        assert bad not in html
    assert "http://" not in html and "https://" not in html


def test_console_bots重定向(client):
    r = client.get("/console/bots?b=claude/mybot")
    assert r.status_code == 302
    assert r.headers["Location"] == "/console?b=claude/mybot"
    r = client.get("/console/bots?b=../bad")
    assert r.status_code == 302
    assert r.headers["Location"] == "/console"


def test_life旧地址重定向与404(client):
    r = client.get("/console/bots/claude/mybot/life")
    assert r.status_code == 302
    assert r.headers["Location"] == "/console?b=claude/mybot"
    assert client.get("/console/bots/claude/../life").status_code == 404
