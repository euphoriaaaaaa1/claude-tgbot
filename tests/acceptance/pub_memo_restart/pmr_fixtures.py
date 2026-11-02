# -*- coding: utf-8 -*-
"""pub-memo-restart 验收夹具：临时 HOME / 配置 / 频道目录，管理台以 Flask 测试客户端起，绝不碰真实 ~/.claude。

特意不叫 conftest.py：同名 conftest 会顶掉 sys.modules["conftest"]，让 pub_draft 用例里
`from conftest import ...` 导到本目录。夹具由各测试文件显式导入（见文件头的 import 行）。
"""
import os
import socket
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.append(str(REPO))          # 只追加不插队，避免顶掉别的目录的同名模块

PROD_PORTS = range(17801, 17809)        # 生产 dispatcher 端口 17801-17808：测试进程一次都不许连

PASSWORD = "pmr-test-panel-pw-12345"


@pytest.fixture(autouse=True)
def no_prod_port_connect(monkeypatch):
    """进程内任何 socket 连向 17801-17808 都记下并按拒连处理；用例结束时只要记过就判失败（被测代码吞异常也逃不掉）。"""
    hits = []
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def _watch(addr):
        if isinstance(addr, tuple) and len(addr) >= 2 and addr[1] in PROD_PORTS:
            hits.append(addr)
            return True
        return False

    def guarded_connect(self, addr):
        if _watch(addr):
            raise ConnectionRefusedError("测试护栏：不许连生产端口 %s" % (addr,))
        return real_connect(self, addr)

    def guarded_connect_ex(self, addr):
        if _watch(addr):
            return 111
        return real_connect_ex(self, addr)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    yield
    assert hits == [], "测试连了生产端口: %s" % hits


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
