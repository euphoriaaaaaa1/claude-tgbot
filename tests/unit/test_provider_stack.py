# -*- coding: utf-8 -*-
"""T09 供应商栈单测：CRUD、绑定→settings→重启、local_conflict、锁忙、代理、脱敏。

全部落盘都在 tmp_path：临时 HOME + 临时 configs/channels/providers.json，
不连真实上游、不启动 8767/8770。
"""
import importlib
import json
import sys
from pathlib import Path

import pytest
from flask import Flask

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

PROVIDER_BODY = {
    "name": "example",
    "base_url": "https://api.example.com",
    "api_key": "sk-test-1234567890",
    "protocol": "anthropic",
    "models": [{"id": "model-a", "label": "Model A"}],
}


@pytest.fixture
def stack(tmp_path, monkeypatch):
    """重定向全部接缝后重载供应商模块；返回 (pc, pp, routes, bot_dir, client)。"""
    configs = tmp_path / "configs"
    channels = tmp_path / "channels"
    bot_dir = channels / "mybot"
    (bot_dir / ".claude").mkdir(parents=True)
    configs.mkdir()
    (configs / "mybot.yml").write_text(
        "display_name: My Bot\nbot_channel_path: %s\n" % bot_dir, encoding="utf-8")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(configs))
    monkeypatch.setenv("CLAUDEBOT_CONFIG_DIR", str(configs))
    monkeypatch.setenv("CLAUDEBOT_CHANNELS_ROOT", str(channels))
    monkeypatch.setenv("CLAUDEBOT_PROVIDER_PATH", str(configs / "providers.json"))
    monkeypatch.setenv("CLAUDEBOT_GLOBAL_CFG", str(configs / "_global.yml"))
    # 代理接缝：端口 0 + LAUNCHCTL=off，任何路径都不会真的起进程。
    monkeypatch.setenv("CLAUDEBOT_PROXY_DIR", str(tmp_path / "proxy"))
    monkeypatch.setenv("CLAUDEBOT_PROXY_PORT", "0")
    monkeypatch.setenv("CLAUDEBOT_PROXY_BIN", str(tmp_path / "missing-proxy"))
    monkeypatch.setenv("CLAUDEBOT_PROXY_LAUNCHCTL", "off")

    import provider_config as pc
    import provider_proxy as pp
    import moments.provider_routes as routes
    importlib.reload(pp)
    importlib.reload(pc)
    importlib.reload(routes)

    app = Flask("provider_test", template_folder=str(_ROOT / "moments" / "templates"))
    app.register_blueprint(routes.provider_bp)
    client = app.test_client()
    return pc, pp, routes, bot_dir, client


def _create(client, **over):
    body = dict(PROVIDER_BODY)
    body.update(over)
    r = client.post("/api/providers", json=body)
    assert r.status_code == 201, r.get_data(as_text=True)
    return r.get_json()["provider"]


def test_crud_十四键且不返回密钥(stack):
    pc, _pp, _routes, _bot_dir, client = stack
    provider = _create(client)
    assert list(provider) == [
        "id", "name", "base_url", "models", "default_model", "key_masked", "has_key",
        "created_at", "updated_at", "proxy", "proxy_auto", "protocol", "anthropic_base_url",
        "openai_base_url"]
    assert "api_key" not in provider
    assert provider["has_key"] is True and provider["key_masked"].endswith("7890")

    listing = client.get("/api/providers")
    assert listing.status_code == 200
    raw = listing.get_data(as_text=True)
    assert "sk-test-1234567890" not in raw
    assert listing.get_json()["providers"][0]["has_key"] is True

    r = client.put("/api/providers/%s" % provider["id"],
                   json={"name": "renamed", "base_url": "https://api.example.com"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["provider"]["name"] == "renamed"
    # 省略 api_key = 保留原密钥
    assert pc.load_providers()["providers"][0]["api_key"] == "sk-test-1234567890"

    r = client.delete("/api/providers/%s" % provider["id"])
    assert r.status_code == 200
    assert client.get("/api/providers").get_json()["providers"] == []


def test_拒绝非法_url_与非法模型(stack):
    _pc, _pp, _routes, _bot_dir, client = stack
    r = client.post("/api/providers", json=dict(PROVIDER_BODY, base_url="ftp://example.com"))
    assert r.status_code == 400
    r = client.post("/api/providers", json=dict(PROVIDER_BODY, name="a b"))
    assert r.status_code == 400
    provider = _create(client)
    pid = provider["id"]
    for bad in (123, "", "x" * 201):
        r = client.post("/api/bots/mybot/provider", json={"provider_id": pid, "model": bad})
        assert r.status_code == 400, r.get_data(as_text=True)
    r = client.post("/api/bots/mybot/provider", json={"provider_id": "p_missing", "model": "m"})
    assert r.status_code == 404


def test_绑定写_settings_并重启_worker(stack, monkeypatch):
    pc, _pp, routes, bot_dir, client = stack
    provider = _create(client)
    calls = []

    def fake_restart(bot_id, port=None, *, force=False):
        calls.append((bot_id, force))
        return {"status": "ok", "message": "已重启"}

    monkeypatch.setattr(routes.claude_proc, "restart_worker", fake_restart)

    r = client.post("/api/bots/mybot/provider",
                    json={"provider_id": provider["id"], "model": "model-a"})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["ok"] is True and body["applied"] is True and body["queued"] is False
    assert calls == [("mybot", False)]

    doc = json.loads((bot_dir / ".claude" / "settings.json").read_text())
    env = doc["env"]
    assert env["ANTHROPIC_BASE_URL"] == "https://api.example.com"
    assert env["ANTHROPIC_MODEL"] == "model-a"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-test-1234567890"
    for key in ("ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL"):
        assert key in env
    assert pc.binding_status("mybot")["binding"]["model"] == "model-a"

    # 思考强度：独立写者，落 settings 顶层 effortLevel
    r = client.post("/api/bots/mybot/effort", json={"effort": "high"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert json.loads((bot_dir / ".claude" / "settings.json").read_text())["effortLevel"] == "high"
    r = client.post("/api/bots/mybot/effort", json={"effort": "ultra"})
    assert r.status_code == 400


def test_local_conflict_拒绝且零写入(stack, monkeypatch):
    pc, _pp, routes, bot_dir, client = stack
    provider = _create(client)
    local = bot_dir / ".claude" / "settings.local.json"
    local.write_text(json.dumps({"env": {"ANTHROPIC_BASE_URL": "https://local.example"}}),
                     encoding="utf-8")
    monkeypatch.setattr(routes.claude_proc, "restart_worker",
                        lambda *a, **k: pytest.fail("local_conflict 不得触发重启"))

    r = client.post("/api/bots/mybot/provider",
                    json={"provider_id": provider["id"], "model": "model-a"})
    assert r.status_code == 409, r.get_data(as_text=True)
    body = r.get_json()
    assert body["code"] == "local_conflict"
    assert body["keys"] == ["ANTHROPIC_BASE_URL"]
    assert not (bot_dir / ".claude" / "settings.json").exists()
    assert pc.get_binding_raw("mybot") is None


def test_settings_锁忙不写入(stack, monkeypatch):
    pc, _pp, _routes, bot_dir, client = stack
    provider = _create(client)
    entry = pc.load_providers()["providers"][0]
    monkeypatch.setattr(pc, "SETTINGS_LOCK_WAIT_SEC", 0.1)
    lock_path = bot_dir / ".claude" / "settings.json.lock"
    from platform_lock import file_lock
    with file_lock(str(lock_path)):
        result, err = pc.set_bot_provider("mybot", entry, "model-a")
    assert result is None
    assert err == pc.SETTINGS_BUSY_MESSAGE
    assert not (bot_dir / ".claude" / "settings.json").exists()
    assert pc.get_binding_raw("mybot") is None


def test_代理状态与空清单同步(stack):
    _pc, pp, _routes, _bot_dir, _client = stack
    status = pp.proxy_status({"providers": []})
    assert status["ok"] is True
    assert status["running"] is False
    assert status["port"] == 0
    assert status["has_key"] is False
    assert "api_key" not in status

    snap = {"proxy": {"instance_key": "sk-inst"}, "providers": [
        {"id": "p_a", "base_url": "https://a.example/v1", "api_key": "sk-secret",
         "proxy": True, "protocol": "anthropic", "models": [{"id": "c1", "label": "c1"}]},
        {"id": "p_b", "base_url": "https://b.example", "api_key": "sk-direct",
         "proxy": False, "protocol": "anthropic", "models": [{"id": "c2", "label": "c2"}]},
    ]}
    import yaml
    cfg = yaml.safe_load(pp.build_config(snap))
    assert cfg["api-keys"] == ["sk-inst"]
    assert [x["prefix"] for x in cfg["claude-api-key"]] == ["p_a"]
    assert "p_b" not in yaml.safe_dump(cfg)

    res = pp.sync({"providers": []}, explicit=True)
    assert res.get("ok") is True
    assert res.get("reloaded") is False


def test_适配器只转发不直写(stack, monkeypatch):
    pc, _pp, _routes, bot_dir, client = stack
    from adapters.claude_local import ClaudeLocalAdapter
    provider = _create(client, name="adapter")
    calls = []

    def fake_set(bot_id, entry, model):
        calls.append((bot_id, entry["id"], model))
        return {"env_written": ["ANTHROPIC_BASE_URL"], "backup_path": None}, None

    monkeypatch.setattr(pc, "set_bot_provider", fake_set)
    adapter = ClaudeLocalAdapter()
    out = adapter.set_provider("mybot", {"provider_id": provider["id"], "model": "model-a"})
    assert calls == [("mybot", provider["id"], "model-a")]
    assert out["applied"] is True
    assert not (bot_dir / ".claude" / "settings.json").exists()


def test_页面无站外资源(stack):
    html = (_ROOT / "moments" / "templates" / "provider.html").read_text(encoding="utf-8")
    assert "<script src=" not in html
    assert '<link rel="stylesheet"' not in html
    assert "@import" not in html
