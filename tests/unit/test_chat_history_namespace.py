# -*- coding: utf-8 -*-
"""chat_history 的 bot→namespace 收敛：读注册表，env BOT_NAMESPACE 优先级不变。"""
import uuid

import pytest

import chat_history

CHENLULU_NS = "550e8400-e29b-41d4-a716-446655440001"


@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
    d = tmp_path / "configs"
    d.mkdir()
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(d))
    monkeypatch.delenv("BOT_NAMESPACE", raising=False)
    return d


def write_bot(d, bot_id, ns=None):
    body = f"id: {bot_id}\n" + (f"namespace_uuid: {ns}\n" if ns else "")
    (d / f"{bot_id}.yml").write_text(body, encoding="utf-8")


def test_新bot只写yml就能算出会话uuid(cfgdir):
    ns = "22222222-2222-2222-2222-222222222222"
    write_bot(cfgdir, "xiaoyu", ns)
    assert chat_history.unified_session_uuid("xiaoyu") == \
        str(uuid.uuid5(uuid.UUID(ns), "unified"))


def test_legacy兜底不变_会话历史不丢(cfgdir):
    """整表被换掉/漏迁 = worker 丢整段会话历史，所以这里硬断言那个定死的常量。"""
    write_bot(cfgdir, "chenlulu")
    assert chat_history.unified_session_uuid("chenlulu") == \
        str(uuid.uuid5(uuid.UUID(CHENLULU_NS), "unified"))


def test_env_BOT_NAMESPACE优先级不变(cfgdir, monkeypatch):
    ns = "33333333-3333-3333-3333-333333333333"
    write_bot(cfgdir, "chenlulu")
    monkeypatch.setenv("BOT_NAMESPACE", ns)
    assert chat_history.unified_session_uuid("chenlulu") == \
        str(uuid.uuid5(uuid.UUID(ns), "unified"))


@pytest.mark.parametrize("bot,ns", [("查无此bot", None), ("bad", "不是个uuid")])
def test_查不到或值不合法一律回None而不是猜(cfgdir, bot, ns):
    if ns:
        write_bot(cfgdir, bot, ns)
    assert chat_history.unified_session_uuid(bot) is None
