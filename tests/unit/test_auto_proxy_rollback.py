# -*- coding: utf-8 -*-
"""只保存模型（PUT /api/providers/<id>/models）也会按条件自动开本机翻译代理；代理没能应用时要像保存供应商一样
撤回这次自动开启、保持直连，并在响应里带 warning。代理接缝：端口 0、LAUNCHCTL=off、代理程序指向不存在的路径，
所以同步一定失败，任何路径都不会真的起进程。
"""
import importlib
import sys
from pathlib import Path

import pytest
from flask import Flask

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture
def stack(tmp_path, monkeypatch):
    configs = tmp_path / "configs"
    channels = tmp_path / "channels"
    (channels / "mybot" / ".claude").mkdir(parents=True)
    configs.mkdir()
    (configs / "mybot.yml").write_text(
        "display_name: My Bot\nbot_channel_path: %s\n" % (channels / "mybot"), encoding="utf-8")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(configs))
    monkeypatch.setenv("CLAUDEBOT_CONFIG_DIR", str(configs))
    monkeypatch.setenv("CLAUDEBOT_CHANNELS_ROOT", str(channels))
    monkeypatch.setenv("CLAUDEBOT_PROVIDER_PATH", str(configs / "providers.json"))
    monkeypatch.setenv("CLAUDEBOT_GLOBAL_CFG", str(configs / "_global.yml"))
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
    app = Flask("auto_proxy_test", template_folder=str(_ROOT / "moments" / "templates"))
    app.register_blueprint(routes.provider_bp)
    return pc, app.test_client()


def _provider(client, base_url):
    r = client.post("/api/providers", json={"name": "oa", "base_url": base_url, "api_key": "sk-test-1234567890",
                                            "protocol": "openai", "models": []})
    assert r.status_code == 201, r.get_data(as_text=True)
    p = r.get_json()["provider"]
    assert p["proxy"] is False   # 没有模型：建的时候还不满足自动开代理的条件
    return p["id"]


def test_只保存模型触发自动开代理_代理没能应用_撤回并带警告(stack):
    pc, client = stack
    pid = _provider(client, "https://api.example.com")
    r = client.put("/api/providers/%s/models" % pid, json={"models": [{"id": "m1"}], "base": []})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json().get("warning") == pc._AUTO_PROXY_SYNC_FAILED
    entry = next(p for p in pc.load_providers()["providers"] if p["id"] == pid)
    assert entry.get("proxy") is False and "proxy_auto" not in entry
    assert [m["id"] for m in entry["models"]] == ["m1"]   # 模型照存，只撤回代理


def test_不满足自动开代理条件_不撤回也不带警告(stack):
    pc, client = stack
    pid = _provider(client, "https://api.example.com/anthropic")   # 地址带 anthropic 段：不自动开
    r = client.put("/api/providers/%s/models" % pid, json={"models": [{"id": "m1"}], "base": []})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert "warning" not in r.get_json()
    entry = next(p for p in pc.load_providers()["providers"] if p["id"] == pid)
    assert entry.get("proxy") is False
