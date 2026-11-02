# -*- coding: utf-8 -*-
"""pub-memo-restart 验收夹具：临时 HOME / 配置 / 频道目录，管理台以 Flask 测试客户端起，绝不碰真实 ~/.claude。"""
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

PASSWORD = "pmr-test-panel-pw-12345"


@pytest.fixture
def r_env(tmp_path, monkeypatch):
    """适配器 / 管理台公用的隔离环境；返回路径字典。"""
    home = tmp_path / "home"
    configs = tmp_path / "configs"
    channels = tmp_path / "channels"
    link_dir = tmp_path / "link-state"
    for p in (home, configs, channels, link_dir):
        p.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(configs))
    monkeypatch.setenv("CLAUDEBOT_CONFIG_DIR", str(configs))
    monkeypatch.setenv("HUB_CHANNELS_DIR", str(channels))
    monkeypatch.setenv("CLAUDEBOT_CHANNELS_ROOT", str(channels))
    monkeypatch.setenv("LINK_STATE_DIR", str(link_dir))
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("HUB_ENV_FILE", str(tmp_path / "hub.env"))
    monkeypatch.setenv("CLAUDE_TGBOT_HOME", str(tmp_path / "tgbot-home"))
    monkeypatch.setenv("HUB_RESTART_SCRIPT", str(tmp_path / "no-such-restart-bots.sh"))
    monkeypatch.setenv("HUB_RESTART_CMD", "/usr/bin/true")
    # provider_config 要求一组测试接缝成组设置（与 pub_draft 夹具同一做法），全部指向临时目录
    (configs / "_global.yml").write_text("jiwen:\n  tick_interval_min: 5\n", encoding="utf-8")
    provider_path = tmp_path / "providers.json"
    provider_path.write_text('{"providers": [], "bindings": {}, "proxy": {}}', encoding="utf-8")
    monkeypatch.setenv("CLAUDEBOTLIFE_TEST", "1")
    monkeypatch.setenv("CLAUDEBOTLIFE_TEST_ROOT", str(tmp_path))
    monkeypatch.setenv("CLAUDEBOT_GLOBAL_CFG", str(configs / "_global.yml"))
    monkeypatch.setenv("CLAUDEBOT_SECRETS_FILE", str(configs / "_secrets.yml"))
    monkeypatch.setenv("CLAUDEBOT_PROVIDER_PATH", str(provider_path))
    monkeypatch.setenv("DIRECTOR_NO_SPAWN", "1")
    monkeypatch.setenv("CONSOLE_DSH_ADAPTER_URL", "")
    for k in ("CLIPROXY_PORT", "CLIPROXY_MGMT_KEY", "CLIPROXY_API_KEY", "MOMENTS_WEB_HOST",
              "CLAUDEBOT_TEST", "CLAUDEBOT_RESTART_ONE_CMD", "HUB_ACCESS_PASSWORD", "HUB_ADMIN_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    # 后台服务命令一律走替身，不许往本机 launchd / systemd 真挂任务
    stub_dir = tmp_path / "fake-service-bin"
    stub_dir.mkdir()
    for name in ("launchctl", "systemctl", "schtasks"):
        s = stub_dir / name
        s.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        s.chmod(0o755)
    monkeypatch.setenv("PATH", str(stub_dir) + os.pathsep + os.environ.get("PATH", ""))
    return {"home": home, "configs": configs, "channels": channels, "link_dir": link_dir, "root": tmp_path}


@pytest.fixture
def adapter(r_env):
    from adapters.claude_local import ClaudeLocalAdapter
    return ClaudeLocalAdapter()


@pytest.fixture
def make_stub(tmp_path):
    """工厂：造整只重启替身；用例结束清掉替身与它起的假服务，不留孤儿。"""
    from pmr_helpers import RestartStub
    made = []

    def _make(bot, port, mode, hang_s=25):
        s = RestartStub(tmp_path, bot, port, mode, hang_s)
        made.append(s)
        return s
    yield _make
    for s in made:
        s.cleanup()


@pytest.fixture
def web_client(r_env, tmp_path, monkeypatch):
    """登录后的 Flask 测试客户端（只挂 claude 真实适配器）。返回 (client, web 模块, panel_auth 模块)。"""
    from adapters import registry
    from adapters.claude_local import ClaudeLocalAdapter
    from moments import panel_auth, web
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    monkeypatch.setattr(panel_auth, "SECRET_PATH", str(secrets / "panel.env"))
    monkeypatch.setattr(panel_auth, "SESSIONS_PATH", str(secrets / "sessions.json"))
    panel_auth._fail.clear()
    real = ClaudeLocalAdapter()
    monkeypatch.setattr(registry, "enabled_backends", lambda: ["claude"])
    monkeypatch.setattr(registry, "adapter", lambda name: real)
    monkeypatch.setattr(registry, "label", lambda name: "Claude")
    panel_auth.set_password(PASSWORD)
    web.app.config["TESTING"] = True
    c = web.app.test_client()
    assert c.post("/login", data={"password": PASSWORD, "next": "/"}).status_code == 302
    yield c, web, panel_auth
    panel_auth._fail.clear()
