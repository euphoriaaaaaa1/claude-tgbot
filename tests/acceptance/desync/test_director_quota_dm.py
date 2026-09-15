"""r7 §2.5/§10.2 配额闸正式注入点 d._quota_ok 与闸序；§10.3 maybe_dm 自身七道闸与 exclude 透传（公开 director.py）。
LLM 调用点 call_claude_json / decide_dm / inject_dm 全部属性覆盖，不碰真实模型、不写真实 inbox。
"""
import pytest

from _helpers import NOW, human

CHAT = "-100"


def _quota_counter(d, value=True):
    calls = []
    d._quota_ok = lambda: (calls.append(1), value)[1]
    return calls


# ---------- _scene_gates 配额闸（最后一闸）----------
def test_gates_quota_ok为False_空状态白天_返回quota不足(director):
    director._quota_ok = lambda: False
    assert director._scene_gates({}, [], NOW, "jiwen") == (False, "quota不足")


def test_gates_全部前闸通过_quota_ok恰调用1次(director):
    calls = _quota_counter(director)
    assert director._scene_gates({}, [], NOW, "jiwen") == (True, "ok")
    assert len(calls) == 1


def test_gates_忙碌闸不过_不调quota_ok(director):
    calls = _quota_counter(director, value=False)  # 即使配额会拒，也轮不到它
    ok, reason = director._scene_gates({}, [], NOW, "jiwen", busy={"bot1", "bot2"})
    assert (ok, reason) == (False, "私聊忙碌(bot1,bot2)可用bot<2") and calls == []


def test_gates_真人30分钟内说过话_不调quota_ok(director):
    calls = _quota_counter(director)
    ok, _ = director._scene_gates({}, [human(NOW - 5 * 60)], NOW, "jiwen")
    assert ok is False and calls == []


def test_gates_夜间闸不过_不调quota_ok(director):
    calls = _quota_counter(director)
    night = NOW - 12 * 3600  # 03:00 本地，落在 NIGHT_SKIP=(1,8)
    ok, _ = director._scene_gates({}, [], night, "jiwen")
    assert ok is False and calls == []


def test_gates_今日上限闸不过_不调quota_ok(director):
    calls = _quota_counter(director)
    st = {"scenes_today": {"date": director.today_str(NOW) if hasattr(director, "today_str") else "2026-09-14",
                           "count": 10 ** 6}}
    ok, _ = director._scene_gates(st, [], NOW, "jiwen")
    assert ok is False and calls == []


# ---------- maybe_dm ----------
def _hm(ts, text="合成"):
    return {**human(ts, text), "from_id": "5331715732", "from_username": "u", "message_id": 2, "chat_id": CHAT}


def _setup(d, who="bot2", dm=True, count=0, last_ts=0, last_bot=None):
    """§10.3 绕闸配方 + 记录 decide_dm / inject_dm 调用。"""
    d.DM_NIGHT_SKIP = (0, 0)
    d._quota_ok = lambda: True
    d.DM_DAILY_LIMIT = 10
    d._dm_state = lambda *a, **k: {"day": "x", "count": count, "last_ts": last_ts, "last_bot": last_bot}
    d.call_claude_json = lambda prompt, **kw: {"dm": dm, "who": who, "snippet": "合成"}
    rec = {"decide": [], "inject": []}
    real = d.decide_dm

    def decide(human_msgs, hist, exclude=(), *a, **kw):
        rec["decide"].append(set(exclude or ()) if not isinstance(exclude, str) else {exclude})
        return real(human_msgs, hist, exclude, *a, **kw)
    d.decide_dm = decide
    d.inject_dm = lambda bot, *a, **kw: (rec["inject"].append(bot), "path")[1]
    return rec


@pytest.mark.skip(reason="公开版无群聊信号主动私聊(DM)功能：INTERFACE §10.3『公开同签名』前提不成立，裁决 L-pub1")
def test_maybe_dm_绕闸配方_decide_dm必被调用_exclude透传stopped并resumed(director):
    rec = _setup(director)
    r = director.maybe_dm({}, [_hm(NOW - 10)], [human(NOW - 600)], NOW, exclude={"bot1", "bot3"})
    assert rec["decide"] == [{"bot1", "bot3"}]
    assert r is not None and rec["inject"] == ["bot2"]


@pytest.mark.skip(reason="公开版无群聊信号主动私聊(DM)功能：INTERFACE §10.3『公开同签名』前提不成立，裁决 L-pub1")
@pytest.mark.parametrize("exclude", [None, (), {"bot1": 1.0}, ["bot3"]])
def test_maybe_dm_exclude各种类型不抛_按集合透传(director, exclude):
    rec = _setup(director)
    director.maybe_dm({}, [_hm(NOW - 10)], [], NOW, exclude=exclude)
    assert rec["decide"] == [set(exclude or ())]


@pytest.mark.skip(reason="公开版无群聊信号主动私聊(DM)功能：INTERFACE §10.3『公开同签名』前提不成立，裁决 L-pub1")
def test_maybe_dm_闸1_新消息无真人_返回None不调LLM(director):
    rec = _setup(director)
    bot_row = {**_hm(NOW - 10), "is_bot": True, "from_username": "bot1"}
    assert director.maybe_dm({}, [bot_row], [], NOW) is None and rec["decide"] == []


@pytest.mark.skip(reason="公开版无群聊信号主动私聊(DM)功能：INTERFACE §10.3『公开同签名』前提不成立，裁决 L-pub1")
def test_maybe_dm_闸2_夜间区间命中_返回None(director):
    rec = _setup(director)
    director.DM_NIGHT_SKIP = (0, 24)
    assert director.maybe_dm({}, [_hm(NOW - 10)], [], NOW) is None and rec["decide"] == []


@pytest.mark.skip(reason="公开版无群聊信号主动私聊(DM)功能：INTERFACE §10.3『公开同签名』前提不成立，裁决 L-pub1")
def test_maybe_dm_闸3_日限已满_返回None(director):
    rec = _setup(director, count=10)
    assert director.maybe_dm({}, [_hm(NOW - 10)], [], NOW) is None and rec["decide"] == []


@pytest.mark.skip(reason="公开版无群聊信号主动私聊(DM)功能：INTERFACE §10.3『公开同签名』前提不成立，裁决 L-pub1")
def test_maybe_dm_闸4_冷却中_返回None(director):
    rec = _setup(director, last_ts=NOW - 1)
    assert director.maybe_dm({}, [_hm(NOW - 10)], [], NOW) is None and rec["decide"] == []


@pytest.mark.skip(reason="公开版无群聊信号主动私聊(DM)功能：INTERFACE §10.3『公开同签名』前提不成立，裁决 L-pub1")
def test_maybe_dm_闸5_quota_ok为False_返回None_不调decide_dm(director):
    rec = _setup(director)
    calls = _quota_counter(director, value=False)
    assert director.maybe_dm({}, [_hm(NOW - 10)], [], NOW) is None
    assert rec["decide"] == [] and len(calls) == 1


@pytest.mark.skip(reason="公开版无群聊信号主动私聊(DM)功能：INTERFACE §10.3『公开同签名』前提不成立，裁决 L-pub1")
def test_maybe_dm_闸6_LLM说不私聊_返回None_不inject(director):
    rec = _setup(director, dm=False)
    assert director.maybe_dm({}, [_hm(NOW - 10)], [], NOW) is None
    assert len(rec["decide"]) == 1 and rec["inject"] == []


@pytest.mark.skip(reason="公开版无群聊信号主动私聊(DM)功能：INTERFACE §10.3『公开同签名』前提不成立，裁决 L-pub1")
def test_maybe_dm_闸7_同一bot连续私聊_返回None_不inject(director):
    rec = _setup(director, who="bot2", count=1, last_bot="bot2")
    assert director.maybe_dm({}, [_hm(NOW - 10)], [], NOW) is None and rec["inject"] == []


@pytest.mark.skip(reason="公开版无群聊信号主动私聊(DM)功能：INTERFACE §10.3『公开同签名』前提不成立，裁决 L-pub1")
def test_maybe_dm_闸7_count为0时同bot仍放行(director):
    rec = _setup(director, who="bot2", count=0, last_bot="bot2")
    assert director.maybe_dm({}, [_hm(NOW - 10)], [], NOW) is not None and rec["inject"] == ["bot2"]


@pytest.mark.skip(reason="公开版无群聊信号主动私聊(DM)功能：INTERFACE §10.3『公开同签名』前提不成立，裁决 L-pub1")
def test_maybe_dm_LLM选中被排除bot_改选后inject的是非排除bot(director):
    rec = _setup(director, who="bot2")
    director.maybe_dm({}, [_hm(NOW - 10)], [], NOW, exclude={"bot2"})
    assert rec["inject"] and rec["inject"][0] != "bot2"
