# -*- coding: utf-8 -*-
"""adapters.guard 单测：打码、令牌对象、回环断言、日志不输出原文。

不解析真实域名（注入假解析器），不连任何服务。
"""
from __future__ import annotations

import logging

import pytest

from adapters import base, guard

TOKEN = "123456789:" + "B" * 35


# ── 打码 ─────────────────────────────────────────────────────────────────────

def test_scrub抹掉令牌原文():
    out = guard.scrub("请求失败 token=%s 请检查" % TOKEN)
    assert TOKEN not in out
    assert base.HIDDEN in out
    assert out == "请求失败 token=%s 请检查" % base.HIDDEN


def test_scrub多段与边界():
    text = "a %s b %s c" % (TOKEN, "987654321:" + "C" * 30)
    out = guard.scrub(text)
    assert "B" * 35 not in out and "C" * 30 not in out
    assert out.count(base.HIDDEN) == 2
    # 普通文字、短数字串不误伤
    assert guard.scrub("版本 12345:abc 与 12:34") == "版本 12345:abc 与 12:34"


def test_scrub非字符串与None():
    assert guard.scrub(None) == ""
    assert guard.scrub(12345) == "12345"
    assert TOKEN not in guard.scrub(ValueError("boom " + TOKEN))
    assert guard.scrub(base.SecretToken(TOKEN)) == base.HIDDEN


def test_guard转出同一份令牌原语():
    assert guard.SecretToken is base.SecretToken
    assert guard.TOKEN_RE is base.TOKEN_RE
    assert guard.TOKEN_FORMAT_MESSAGE == base.TOKEN_FORMAT_MESSAGE


def test_日志经打码后不含原文(caplog):
    logger = logging.getLogger("adapters.test.guard")
    with caplog.at_level(logging.INFO, logger=logger.name):
        logger.info("%s", guard.scrub("失败：%s" % TOKEN))
    assert TOKEN not in caplog.text
    assert base.HIDDEN in caplog.text


# ── 回环断言 ─────────────────────────────────────────────────────────────────

def _resolver(*infos):
    return lambda *a, **k: list(infos)


def _info(addr):
    return (2, 1, 6, "", (addr, 0))


def test_字面回环直接通过():
    guard.assert_loopback("127.0.0.1")
    guard.assert_loopback("::1")
    assert guard.loopback_address("127.0.0.1") == "127.0.0.1"
    assert guard.loopback_address("::1") == "::1"


def test_localhost解析全回环才通过(monkeypatch):
    monkeypatch.setattr(guard, "_getaddrinfo", _resolver(_info("::1"), _info("127.0.0.1")))
    guard.assert_loopback("localhost")
    # 优先 IPv4
    assert guard.loopback_address("localhost") == "127.0.0.1"


def test_localhost解析只有IPv6回环(monkeypatch):
    monkeypatch.setattr(guard, "_getaddrinfo", _resolver(_info("::1")))
    assert guard.loopback_address("localhost") == "::1"


def test_localhost解析出非回环拒绝(monkeypatch):
    monkeypatch.setattr(guard, "_getaddrinfo", _resolver(_info("127.0.0.1"), _info("10.0.0.5")))
    with pytest.raises(guard.NotLoopbackError):
        guard.assert_loopback("localhost")
    with pytest.raises(guard.NotLoopbackError):
        guard.loopback_address("localhost")


def test_localhost解析为空或失败都拒绝(monkeypatch):
    monkeypatch.setattr(guard, "_getaddrinfo", _resolver())
    with pytest.raises(guard.NotLoopbackError):
        guard.assert_loopback("localhost")

    def boom(*a, **k):
        raise OSError("no dns")

    monkeypatch.setattr(guard, "_getaddrinfo", boom)
    with pytest.raises(guard.NotLoopbackError):
        guard.assert_loopback("localhost")
    with pytest.raises(guard.NotLoopbackError):
        guard.loopback_address("localhost")


def test_其他写法一律拒绝(monkeypatch):
    # 就算解析器说它是回环，也不认非白名单写法
    monkeypatch.setattr(guard, "_getaddrinfo", _resolver(_info("127.0.0.1")))
    for bad in ("127.0.0.2", "LOCALHOST", "localhost.", "[::1]", "0.0.0.0", "", None, 127):
        with pytest.raises(guard.NotLoopbackError):
            guard.assert_loopback(bad)
    assert str(guard.NotLoopbackError()) == "对方不是本机回环地址，不发送"
