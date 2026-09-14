"""需求⑤ r5（公开 director.py）：启用宽限 _prev_stopped/_resumed_at/RESUME_GRACE_MIN（§9.2、§10.2）
与 decide_dm 的排除集合（§10.3）。停止用 configs/<bot>.yml enabled:false。
"""
from types import SimpleNamespace

import pytest

from _helpers import NOW, gt_line, human, inbox, switch_on, write_cfg

CHAT = "-100"
ALL = {"bot1", "bot2", "bot3"}


def _idle(d, iso, jiwen_bot=None):
    switch_on(iso, CHAT)
    gt_line(iso, CHAT, NOW - 3 * 3600, "昨天的话", mid=1)
    d._save_state(CHAT, {"last_ts": NOW - 3 * 3600, "last_mid": 1, "last_initiate_ts": NOW - 60})
    d._global_cfg = lambda: {}
    d._jiwen_reader = SimpleNamespace(read=lambda bot, human_id, cfg: (
        {"forced": True, "description": "已经撑不住了"} if bot == jiwen_bot else None))
    d._db = SimpleNamespace(list_moments=lambda limit, since_ts: [])
    d._holiday = SimpleNamespace(is_holiday=lambda date: None)
    d.call_claude_json = lambda prompt, **kw: {"speak": True, "who": "bot2", "done": False, "dangling": False,
                                               "heat": 3, "reason": "r", "dm": True, "snippet": "合成"}


def test_上tick停止本tick启用_记resumed_at为now_打resumed_skip(director, iso, capsys):
    _idle(director, iso)
    write_cfg(iso["cfg"], "bot2", enabled=True)
    director._prev_stopped = {"bot2"}
    director.tick(CHAT, NOW)
    assert director._resumed_at.get("bot2") == NOW
    assert "[director] resumed_skip bot=bot2 since_min=0" in capsys.readouterr().out


def test_首个tick_prev_stopped为None_不判定转换(director, iso):
    _idle(director, iso)
    write_cfg(iso["cfg"], "bot2", enabled=True)
    director._prev_stopped = None
    director.tick(CHAT, NOW)
    assert "bot2" not in director._resumed_at


def test_宽限内_jiwen触发刚启用的bot_不开场_无inbox(director, iso):
    _idle(director, iso, jiwen_bot="bot2")
    director._prev_stopped = set()
    director._resumed_at = {"bot2": NOW - 1799}
    r = director.tick(CHAT, NOW)
    assert r == {"action": "idle"}
    assert inbox(iso, "bot2") == []
    assert "bot2" in director._resumed_at


@pytest.mark.parametrize("age", [1800, 1801])  # r7：边界 1800 秒不含（now - t < 1800 才算宽限）
def test_宽限过期_恰1800与1801秒_项被删除_jiwen触发照常开场(director, iso, age):
    _idle(director, iso, jiwen_bot="bot2")
    director._prev_stopped = set()
    director._resumed_at = {"bot2": NOW - age}
    director.tick(CHAT, NOW)
    assert "bot2" not in director._resumed_at
    assert len(inbox(iso, "bot2")) == 1


def test_宽限期内再次被停用_从resumed_at删除(director, iso):
    _idle(director, iso)
    director._prev_stopped = set()
    director._resumed_at = {"bot2": NOW - 10}
    write_cfg(iso["cfg"], "bot2", enabled=False)
    director.tick(CHAT, NOW)
    assert "bot2" not in director._resumed_at


def test_宽限内的bot_真人新消息分支也被排除(director, iso):
    _idle(director, iso)
    gt_line(iso, CHAT, NOW - 10, "刚说的话", mid=2)
    director._prev_stopped = set()
    director._resumed_at = {"bot2": NOW - 10}  # LLM 固定选 bot2
    director.tick(CHAT, NOW)
    assert inbox(iso, "bot2") == []
    assert len(inbox(iso, "bot1")) + len(inbox(iso, "bot3")) == 1


def test_RESUME_GRACE_MIN可改_设为0则不再宽限(director, iso, monkeypatch):
    _idle(director, iso, jiwen_bot="bot2")
    monkeypatch.setattr(director, "RESUME_GRACE_MIN", 0, raising=False)
    director._prev_stopped = set()
    director._resumed_at = {"bot2": NOW}
    director.tick(CHAT, NOW)
    assert len(inbox(iso, "bot2")) == 1


# ---------- decide_dm ----------
def _dm(director):
    director.call_claude_json = lambda prompt, **kw: {"dm": True, "who": "bot2", "snippet": "合成"}
    return [human(NOW - 10, "合成")], [human(NOW - 600, "早些")]


def test_decide_dm_LLM选中被排除bot_改选BOTS顺序下一个(director):
    msgs, hist = _dm(director)
    r = director.decide_dm(msgs, hist, exclude={"bot2"})
    assert r["dm"] is True and r["who"] == "bot1"


def test_decide_dm_全部排除_dm_False_可用bot不足(director):
    msgs, hist = _dm(director)
    assert director.decide_dm(msgs, hist, exclude=ALL) == {"dm": False, "reason": "可用bot不足"}


@pytest.mark.parametrize("exclude", [None, (), {"bot1": 1.0}, ["bot3"], "bot1"])
def test_decide_dm_exclude各种类型不抛_who不在排除集(director, exclude):
    msgs, hist = _dm(director)
    r = director.decide_dm(msgs, hist, exclude=exclude)
    ex = set(exclude or ()) if not isinstance(exclude, str) else {exclude}
    assert r["who"] not in ex


def test_decide_dm_exclude为空_who原样透传(director):
    msgs, hist = _dm(director)
    assert director.decide_dm(msgs, hist)["who"] == "bot2"
