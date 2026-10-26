# -*- coding: utf-8 -*-
"""pub-draft 白盒：积温 marker 的写入与解析、起草脚本的纯判定函数、dsh 人设取文本。

全部落在 tmp_path；不起网络。
"""
import importlib.util
from pathlib import Path

import pytest
import yaml

import jiwen.tick as tick
from adapters.base import AdapterUnavailable
from adapters.dsh_http import DshHttpAdapter

ROOT = Path(__file__).resolve().parents[2]
STATE_REL = Path(".claude") / "dispatcher" / ".self-initiate-state"


def _load(path_name, mod_name):
    spec = importlib.util.spec_from_file_location(mod_name, ROOT / path_name)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def self_initiate():
    return _load("scripts/self_initiate.py", "pd_self_initiate")


@pytest.fixture(scope="module")
def draft_mod():
    return _load("scripts/draft_proactive_config.py", "pd_draft_proactive")


def _marker(tmp_path, monkeypatch, content, bot="b1", chat="9"):
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / STATE_REL
    d.mkdir(parents=True, exist_ok=True)
    p = d / ("%s-%s.last-sent" % (bot, chat))
    if content is not None:
        p.write_text(content, encoding="utf-8")
    return p


NOW = 1758000300


@pytest.mark.parametrize("content,expected", [
    ("1758000000", 1758000000),
    ("1758000000\n", 1758000000),
    ("12.3", 0),
    (" +1758000000", 0),
    ("-1", 0),
    ("０００１", 0),          # 全角数字
    ("", 0),
    (str(NOW + 3600), 0),    # 超前超过 60 秒按坏值
    (str(NOW + 30), NOW + 30),
], ids=["整数", "行尾换行", "小数", "正号", "负数", "全角", "空", "未来太远", "未来容忍内"])
def test_读active_marker(tmp_path, monkeypatch, content, expected):
    _marker(tmp_path, monkeypatch, content)
    assert tick._marker_initiate_ts("b1", "9", NOW) == expected


def test_marker缺失或读不了按0(tmp_path, monkeypatch):
    _marker(tmp_path, monkeypatch, None)
    assert tick._marker_initiate_ts("b1", "9", NOW) == 0
    # marker 被写成目录：读失败只按缺失处理，不抛
    p = _marker(tmp_path, monkeypatch, None)
    p.mkdir()
    assert tick._marker_initiate_ts("b1", "9", NOW) == 0


def test_写送达marker_纯ASCII与原子落地(tmp_path, self_initiate):
    state = tmp_path / "state"
    state.mkdir()
    self_initiate._write_sent_marker(str(state), "b1", "9", 1758000000.75)
    p = state / "b1-9.last-sent"
    assert p.read_text(encoding="utf-8") == "1758000000"
    assert p.read_text(encoding="utf-8").isascii() and p.read_text(encoding="utf-8").isdigit()
    assert not (state / "b1-9.last-sent.tmp").exists()


def test_写送达marker_失败不抛(tmp_path, self_initiate):
    self_initiate._write_sent_marker(str(tmp_path / "no-such-dir"), "b1", "9", 1758000000)


def test_sleep_ok判定表(draft_mod):
    ok = [[], ["00:00-00:00"], ["00:30-06:30", "22:00-01:00"]]
    bad = [["0:30-06:30"], ["00:3-06:30"], ["00:30-6:30"], ["24:00-06:00"], ["00:60-01:00"],
           ["00:30-06:30\n"], ["００:３０-０６:３０"], [1], ["00:00-01:00"] * 7]
    for value in ok:
        assert draft_mod._sleep_ok(value), value
    for value in bad:
        assert not draft_mod._sleep_ok(value), value


def test_base_url校验(draft_mod):
    for url in ("http://127.0.0.1:1", "http://localhost:9", "https://api.deepseek.com/anthropic"):
        assert draft_mod._base_url_ok(url), url
    for url in ("http://192.168.1.5:1", "ftp://example.com", "http://user@127.0.0.1:1",
                "http://127.0.0.1:1\\x", "http://127.0.0.1:1 /x", "http://[::1].evil:1"):
        assert not draft_mod._base_url_ok(url), url


def test_texts三段都非空才行(draft_mod):
    good = {"persona_summary": "a", "speaking_threshold": "b", "jiwen": {"delta_hints": "c"}}
    assert draft_mod._texts(good) == ("a", "b", "c")
    assert draft_mod._texts(dict(good, persona_summary="  ")) is None
    assert draft_mod._texts({"persona_summary": "a", "speaking_threshold": "b"}) is None
    assert draft_mod._texts({"persona_summary": "a", "speaking_threshold": "b", "jiwen": "x"}) is None


def test_dsh_get_persona_text只看text形状(monkeypatch):
    ad = DshHttpAdapter(base_url="http://127.0.0.1:1", token_file="/nonexistent")
    monkeypatch.setattr(ad, "_request", lambda *_a, **_k: {"text": "人设", "version": 1})
    assert ad.get_persona_text("b1") == "人设"
    monkeypatch.setattr(ad, "_request", lambda *_a, **_k: {"text": 123})
    with pytest.raises(AdapterUnavailable):
        ad.get_persona_text("b1")
