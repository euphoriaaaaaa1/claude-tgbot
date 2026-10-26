# -*- coding: utf-8 -*-
"""pub-draft 验收公共夹具：临时 HOME / configs / channels，绝不碰真实 ~/.claude。

只按 .devflow/pub-draft-接口.md 写，不读实现代码。真实适配器（ClaudeLocalAdapter）
只经公开 HTTP 路由驱动；模型与 dsh 一律用回环假服务。
"""
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

BOT = "pd_bot"
CHAT = "10001"
STATE_REL = Path(".claude") / "dispatcher" / ".self-initiate-state"


@pytest.fixture
def pd_env(tmp_path, monkeypatch):
    """隔离环境；返回路径字典。所有用例只在这里面读写。"""
    root = tmp_path
    home = root / "home"
    configs = root / "configs"
    channels = root / "channels"
    secrets = home / ".claude" / "secrets"
    for p in (home, configs, channels, secrets, root / "work"):
        p.mkdir(parents=True, exist_ok=True)
    (configs / "_global.yml").write_text("jiwen:\n  tick_interval_min: 5\n", encoding="utf-8")
    provider_path = root / "providers.json"
    provider_path.write_text('{"providers": [], "bindings": {}, "proxy": {}}', encoding="utf-8")
    monkeypatch.setenv("CLAUDEBOTLIFE_TEST", "1")
    monkeypatch.setenv("CLAUDEBOTLIFE_TEST_ROOT", str(root))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(configs))
    monkeypatch.setenv("CLAUDEBOT_CONFIG_DIR", str(configs))
    monkeypatch.setenv("CLAUDEBOT_CHANNELS_ROOT", str(channels))
    monkeypatch.setenv("CLAUDEBOT_GLOBAL_CFG", str(configs / "_global.yml"))
    monkeypatch.setenv("CLAUDEBOT_SECRETS_FILE", str(configs / "_secrets.yml"))
    monkeypatch.setenv("CLAUDEBOT_PROVIDER_PATH", str(provider_path))
    monkeypatch.setenv("DIRECTOR_NO_SPAWN", "1")
    monkeypatch.setenv("CONSOLE_DSH_ADAPTER_URL", "")
    return {"root": root, "home": home, "configs": configs, "channels": channels,
            "secrets": secrets, "work": root / "work", "state": home / STATE_REL}


def write_bot_cfg(env, bot=BOT, extra="", enabled=True):
    """给一只 bot 写最小配置（含 channel 路径与 chat_id）。"""
    channel = env["channels"] / bot
    channel.mkdir(parents=True, exist_ok=True)
    text = "id: %s\nenabled: %s\nchat_id: \"%s\"\nbot_channel_path: %s\n%s" % (
        bot, "true" if enabled else "false", CHAT, channel, extra)
    (env["configs"] / ("%s.yml" % bot)).write_text(text, encoding="utf-8")
    return channel


def write_life_cfg(configs, bot=BOT, body=None):
    """覆盖写 bot 的生活设定文件（即 bot 配置），返回其路径。"""
    path = Path(configs) / ("%s.yml" % bot)
    path.write_text(body if body is not None else "id: %s\n" % bot, encoding="utf-8")
    return path
