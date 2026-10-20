# -*- coding: utf-8 -*-
"""令牌读取：``channels/<bot>/.env`` 的 Telegram token 与 ``.api-token`` 的唯一读法。

全部在 tmp_path 里造 HOME，绝不读机主真实通道目录；断言只比对值，不打印令牌。
"""
import os
from pathlib import Path

import pytest

import bots_registry


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    return tmp_path


def chan(home, bot_id="beta"):
    d = home / ".claude" / "channels" / bot_id
    d.mkdir(parents=True)
    return d


# ------------------------------------------------------------ .env / Telegram token

def test_从env读token_支持export引号与后行覆盖(home):
    d = chan(home)
    (d / ".env").write_text(
        "# 注释\n"
        "OTHER=1\n"
        "export TELEGRAM_BOT_TOKEN='123456:first'\n"
        'TELEGRAM_BOT_TOKEN="789012:second"\n',
        encoding="utf-8")
    assert bots_registry.read_telegram_token("beta") == "789012:second"


def test_未加引号的token原样返回(home):
    d = chan(home)
    (d / ".env").write_text("TELEGRAM_BOT_TOKEN=123456789:AA-abc_DEF\n", encoding="utf-8")
    assert bots_registry.read_telegram_token("beta") == "123456789:AA-abc_DEF"


@pytest.mark.parametrize("content", [
    None,                                   # .env 不存在
    "",
    "# TELEGRAM_BOT_TOKEN=x\n",
    "TELEGRAM_BOT_TOKEN=\n",
    "TELEGRAM_BOT_TOKEN=''\n",
    "OTHER=1\n",
])
def test_令牌缺失或为空回None(home, content):
    d = chan(home)
    if content is not None:
        (d / ".env").write_text(content, encoding="utf-8")
    assert bots_registry.read_telegram_token("beta") is None


def test_env不是合法utf8回None而不是抛(home):
    d = chan(home)
    (d / ".env").write_bytes(b"TELEGRAM_BOT_TOKEN=\xff\xfe\n")
    assert bots_registry.read_telegram_token("beta") is None


def test_显式channel_dir优先(home):
    d = home / "custom-channel"
    d.mkdir()
    (d / ".env").write_text("TELEGRAM_BOT_TOKEN=custom\n", encoding="utf-8")
    assert bots_registry.read_telegram_token("beta", str(d)) == "custom"
    # 默认目录没有 .env，不会串读
    assert bots_registry.read_telegram_token("beta") is None


# ------------------------------------------------------------ .api-token

def test_读api_token并去掉行尾空白(home):
    d = chan(home)
    (d / ".api-token").write_text("local-secret-token\n", encoding="utf-8")
    assert bots_registry.read_dispatcher_token("beta") == "local-secret-token"


@pytest.mark.parametrize("content", [None, "", "   \n"])
def test_api_token缺失或为空回None(home, content):
    d = chan(home)
    if content is not None:
        (d / ".api-token").write_text(content, encoding="utf-8")
    assert bots_registry.read_dispatcher_token("beta") is None


def test_dispatcher_auth_headers有则带无则空(home):
    d = chan(home)
    (d / ".api-token").write_text("tok-123", encoding="utf-8")
    assert bots_registry.dispatcher_auth_headers("beta") == {"X-Api-Token": "tok-123"}
    assert bots_registry.dispatcher_auth_headers("nosuch") == {}


# ------------------------------------------------------------ 路径越界

@pytest.mark.parametrize("bad", ["", ".", "..", "../victim", "a/b", "a\\b", "\x00x"])
def test_越界bot_id被拒且不读诱饵文件(home, bad):
    # 若实现直接拼路径，../../victim 会落到 HOME/.claude/victim 或 HOME/victim
    for victim in (home / ".claude" / "victim", home / "victim"):
        victim.mkdir(parents=True, exist_ok=True)
        (victim / ".env").write_text("TELEGRAM_BOT_TOKEN=leaked\n", encoding="utf-8")
        (victim / ".api-token").write_text("leaked\n", encoding="utf-8")
    assert bots_registry.default_channel_dir(bad) is None
    assert bots_registry.read_telegram_token(bad) is None
    assert bots_registry.read_dispatcher_token(bad) is None


def test_默认目录固定在HOME下(home):
    assert bots_registry.default_channel_dir("beta") == \
        str(home / ".claude" / "channels" / "beta")


# ------------------------------------------------------------ 不泄露

def test_读取令牌不打印任何东西(home, capsys):
    d = chan(home)
    (d / ".env").write_text("TELEGRAM_BOT_TOKEN=secret-telegram\n", encoding="utf-8")
    (d / ".api-token").write_text("secret-local\n", encoding="utf-8")
    assert bots_registry.read_telegram_token("beta") == "secret-telegram"
    assert bots_registry.read_dispatcher_token("beta") == "secret-local"
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""
    assert "secret-telegram" not in captured.err
    assert "secret-local" not in captured.err
