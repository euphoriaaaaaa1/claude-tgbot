# -*- coding: utf-8 -*-
"""party 对外状态字段：INTERFACE §4.7 要求 ``started_at_ms``。

状态文件内部与 ``open_party`` 返回值仍用 ``opened_at_ms``（格式不变），
``public_state``（管理台 party 对象经过的那一层）必须额外给出同值的 ``started_at_ms``。
"""
import pytest

from adapters import party_state

CHAT = "-1001234567890"
NOW = 1_700_000_000_000


@pytest.fixture(autouse=True)
def _party_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("DISPATCHER_PARTY_DIR", str(tmp_path / "party"))


def test_public_state_has_started_at_ms_alias():
    party_state.open_party(CHAT, "console", note="一起看电影", now_ms_value=NOW)

    st = party_state.public_state(CHAT, now_ms_value=NOW)

    assert st["on"] is True
    assert st["started_at_ms"] == st["opened_at_ms"] == NOW


def test_public_state_closed_has_no_started_at_ms():
    st = party_state.public_state(CHAT, now_ms_value=NOW)

    assert st == {"on": False}
    assert "started_at_ms" not in st
