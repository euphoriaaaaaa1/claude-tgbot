"""缺陷② + 需求⑤（公开 director.py）tick 级：活跃场的续轮/收场 × 忙碌与停止集合（INTERFACE §2.6、§9.2）。
预置活跃场按 §10.2 形状；停止用 configs/<bot>.yml enabled:false（公开权威判定 disabled_ids_safe）。
"""
from types import SimpleNamespace

from _helpers import NOW, gt_line, inbox, marker, switch_on, write_cfg

CHAT = "-100"


def _scene(d, iso, who="bot1", last_speaker="bot2"):
    switch_on(iso, CHAT)
    gt_line(iso, CHAT, NOW - 3 * 3600, "昨天的话", mid=1)
    d._save_state(CHAT, {"last_ts": NOW - 3 * 3600, "last_mid": 1, "last_initiate_ts": NOW - 60,
                         "scene": {"active": True, "kind": "cold_gap", "budget": 5, "last_speaker": last_speaker,
                                   "opened_ts": NOW - 60, "next_turn_after": NOW - 1, "fail_count": 0,
                                   "closing_done": False}})
    d._global_cfg = lambda: {}
    d._jiwen_reader = SimpleNamespace(read=lambda bot, human_id, cfg: None)
    d._db = SimpleNamespace(list_moments=lambda limit, since_ts: [])
    d._holiday = SimpleNamespace(is_holiday=lambda date: None)
    d.call_claude_json = lambda prompt, **kw: {"done": False, "dangling": False, "who": who,
                                               "speak": True, "heat": 3, "reason": "r"}


def _all_inbox(iso):
    return {b: inbox(iso, b) for b in ("bot1", "bot2", "bot3")}


def test_活跃场到点_两bot私聊中_scene_end_理由列忙bot_不inject(director, iso):
    _scene(director, iso)
    marker(iso, "bot1", "1", int(NOW) - 60)
    marker(iso, "bot3", "1", int(NOW) - 60)
    r = director.tick(CHAT, NOW)
    assert r == {"action": "scene_end", "reason": "私聊忙碌(bot1,bot3)可用bot<2"}
    assert director._load_state(CHAT)["scene"]["active"] is False
    assert _all_inbox(iso) == {"bot1": [], "bot2": [], "bot3": []}


def test_活跃场到点_一bot忙_续轮照常_不收场(director, iso):
    _scene(director, iso, who="bot1")
    marker(iso, "bot3", "1", int(NOW) - 60)
    r = director.tick(CHAT, NOW)
    assert r["action"] != "scene_end", r
    assert director._load_state(CHAT)["scene"]["active"] is True


def test_续轮_LLM点名忙bot_改点非忙bot_忙bot目录无inbox(director, iso):
    _scene(director, iso, who="bot3")
    marker(iso, "bot3", "1", int(NOW) - 60)
    director.tick(CHAT, NOW)
    boxes = _all_inbox(iso)
    assert boxes["bot3"] == []
    assert sum(len(v) for v in boxes.values()) == 1, boxes


def test_续轮_LLM点名已停止bot_改点其它bot_停止bot目录无inbox(director, iso):
    _scene(director, iso, who="bot3")
    write_cfg(iso["cfg"], "bot3", enabled=False)
    director.tick(CHAT, NOW)
    boxes = _all_inbox(iso)
    assert boxes["bot3"] == []
    assert sum(len(v) for v in boxes.values()) == 1, boxes


def test_活跃场到点_一忙一停_scene_end_理由两段各列(director, iso):
    _scene(director, iso)
    marker(iso, "bot1", "1", int(NOW) - 60)
    write_cfg(iso["cfg"], "bot2", enabled=False)
    r = director.tick(CHAT, NOW)
    assert r == {"action": "scene_end", "reason": "私聊忙碌(bot1)或已停止(bot2)可用bot<2"}
    assert _all_inbox(iso) == {"bot1": [], "bot2": [], "bot3": []}


def test_活跃场到点_两bot停止_scene_end_理由只有已停止段(director, iso):
    _scene(director, iso)
    write_cfg(iso["cfg"], "bot1", enabled=False)
    write_cfg(iso["cfg"], "bot2", enabled=False)
    r = director.tick(CHAT, NOW)
    assert r == {"action": "scene_end", "reason": "已停止(bot1,bot2)可用bot<2"}
    assert _all_inbox(iso) == {"bot1": [], "bot2": [], "bot3": []}


def test_无活跃场_jiwen触发_两bot停止_scene_skip_理由只有已停止段(director, iso):
    _scene(director, iso)
    director._save_state(CHAT, {"last_ts": NOW - 3 * 3600, "last_mid": 1, "last_initiate_ts": NOW - 60})
    director._jiwen_reader = SimpleNamespace(read=lambda bot, human_id, cfg: (
        {"forced": True, "description": "已经撑不住了"} if bot == "bot3" else None))
    write_cfg(iso["cfg"], "bot1", enabled=False)
    write_cfg(iso["cfg"], "bot2", enabled=False)
    r = director.tick(CHAT, NOW)
    assert r == {"action": "scene_skip", "kind": "jiwen", "reason": "已停止(bot1,bot2)可用bot<2"}
    assert _all_inbox(iso) == {"bot1": [], "bot2": [], "bot3": []}


def test_真人新消息分支_LLM点已停止bot_改点其它_停止bot无inbox(director, iso):
    _scene(director, iso, who="bot2")
    director._save_state(CHAT, {"last_ts": NOW - 3 * 3600, "last_mid": 1, "last_initiate_ts": NOW - 60})
    gt_line(iso, CHAT, NOW - 10, "刚说的话", mid=2)
    write_cfg(iso["cfg"], "bot2", enabled=False)
    director.tick(CHAT, NOW)
    boxes = _all_inbox(iso)
    assert boxes["bot2"] == []
    assert sum(len(v) for v in boxes.values()) == 1, boxes


def test_活跃场_stopped与resumed都为空_busy为空_输出与预置场续轮一致(director, iso):
    _scene(director, iso, who="bot1")
    r = director.tick(CHAT, NOW)
    assert r["action"] not in ("scene_end", "scene_skip", "idle", "off"), r
    assert len(inbox(iso, "bot1")) == 1
