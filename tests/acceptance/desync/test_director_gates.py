"""缺陷②（公开 director.py）：_scene_gates 忙碌闸、_check_triggers busy 参数、decide_* 排除集合。
LLM 调用点 director.call_claude_json 用属性覆盖法 mock；不碰真实模型。
"""
import pytest

from _helpers import NOW, human

ALL = {"bot1", "bot2", "bot3"}
HIST = [human(NOW - 3 * 3600, "昨天的话")]


def _mock(director, monkeypatch, who="bot1"):
    prompts = []

    def fake(prompt, **kw):
        prompts.append(prompt)
        return {"speak": True, "who": who, "done": False, "dangling": False, "heat": 3, "reason": "r"}
    monkeypatch.setattr(director, "call_claude_json", fake)
    return prompts


# ---------- _scene_gates ----------
def test_gates_无忙碌_空状态白天_放行ok(director):
    assert director._scene_gates({}, [], NOW, "jiwen") == (True, "ok")


def test_gates_两bot忙_可用不足2_拒绝并列出忙bot(director, capsys):
    ok, reason = director._scene_gates({}, [], NOW, "jiwen", busy={"bot2", "bot1"})
    assert (ok, reason) == (False, "私聊忙碌(bot1,bot2)可用bot<2")
    out = capsys.readouterr().out
    assert "[director] busy_skip where=scene_gate bot=bot1" in out
    assert "[director] busy_skip where=scene_gate bot=bot2" in out


def test_gates_一bot忙_可用仍2_放行(director):
    assert director._scene_gates({}, [], NOW, "moment", busy={"bot3"}) == (True, "ok")


def test_gates_三bot全忙_拒绝_按BOTS顺序列全部(director):
    ok, reason = director._scene_gates({}, [], NOW, "jiwen", busy=("bot3", "bot1", "bot2"))
    assert (ok, reason) == (False, "私聊忙碌(bot1,bot2,bot3)可用bot<2")


def test_gates_真人5分钟内说过话_先于忙碌闸拒绝_理由不含私聊忙碌(director):
    hist = [human(NOW - 5 * 60)]
    base = director._scene_gates({}, hist, NOW, "jiwen")
    with_busy = director._scene_gates({}, hist, NOW, "jiwen", busy=ALL)
    assert base[0] is False and with_busy == base
    assert "私聊忙碌" not in with_busy[1]


@pytest.mark.parametrize("busy", [None, (), {}, {"bot1": 3.0, "bot2": 1.0}, ["bot1", "bot2"]])
def test_gates_busy类型dict_tuple_None_list都不抛_dict取键(director, busy):
    ok, reason = director._scene_gates({}, [], NOW, "jiwen", busy=busy)
    if busy and len(busy) >= 2:
        assert (ok, reason) == (False, "私聊忙碌(bot1,bot2)可用bot<2")
    else:
        assert (ok, reason) == (True, "ok")


# ---------- _check_triggers ----------
def test_triggers_cold_gap_忙碌不影响_与无busy逐字相同(director):
    base = director._check_triggers({}, [], NOW)
    assert base == {"kind": "cold_gap", "opener": None, "context": ""}
    assert director._check_triggers({}, [], NOW, busy=ALL) == base


@pytest.mark.parametrize("busy", [None, {"bot1": 0.0}, ("bot2",), ["bot3"]])
def test_triggers_busy各种类型不抛(director, busy):
    assert director._check_triggers({}, [], NOW, busy=busy) is not None


# ---------- decide_initiate ----------
def test_initiate_LLM选中忙bot_改选BOTS顺序下一个(director, monkeypatch):
    _mock(director, monkeypatch, who="bot1")
    r = director.decide_initiate(HIST, exclude={"bot1"})
    assert r["speak"] is True and r["who"] == "bot2"


def test_initiate_提示词含别选忙bot那一行(director, monkeypatch):
    prompts = _mock(director, monkeypatch, who="bot2")
    director.decide_initiate(HIST, exclude={"bot1"})
    assert prompts and "别选 角色一(bot1)（正在忙）" in prompts[-1]


def test_initiate_exclude为空_提示词无正在忙_who原样透传(director, monkeypatch):
    prompts = _mock(director, monkeypatch, who="bot1")
    r = director.decide_initiate(HIST)
    assert r["who"] == "bot1" and "正在忙" not in prompts[-1]


def test_initiate_全部忙_返回speak_False_可用bot不足(director, monkeypatch):
    _mock(director, monkeypatch, who="bot1")
    assert director.decide_initiate(HIST, exclude=ALL) == {"speak": False, "reason": "可用bot不足"}


# ---------- decide_scene ----------
def test_scene_排除集合为exclude并busy_LLM选中被排除者改选(director, monkeypatch):
    _mock(director, monkeypatch, who="bot1")
    r = director.decide_scene(HIST, 3, exclude="bot1", busy={"bot2"})
    assert r["who"] == "bot3"


def test_scene_全部被排除_who为None(director, monkeypatch):
    _mock(director, monkeypatch, who="bot2")
    r = director.decide_scene(HIST, 3, exclude="bot1", busy={"bot2", "bot3"})
    assert r["who"] is None


def test_scene_busy为空_与改前相同_只排除exclude(director, monkeypatch):
    _mock(director, monkeypatch, who="bot1")
    assert director.decide_scene(HIST, 3, "bot1")["who"] == "bot2"


# ---------- decide（真人新消息分支）----------
def test_decide_exclude集合_LLM选中停止bot_改选其它(director, monkeypatch):
    _mock(director, monkeypatch, who="bot2")
    r = director.decide("-100", HIST, 3, exclude={"bot2"})
    assert r["speak"] is True and r["who"] in {"bot1", "bot3"}


def test_decide_exclude仍接受str(director, monkeypatch):
    _mock(director, monkeypatch, who="bot1")
    assert director.decide("-100", HIST, 3, exclude="bot1")["who"] != "bot1"


def test_decide_全排除_speak_False_可用bot不足(director, monkeypatch):
    _mock(director, monkeypatch, who="bot1")
    assert director.decide("-100", HIST, 3, exclude=ALL) == {"speak": False, "reason": "可用bot不足"}
