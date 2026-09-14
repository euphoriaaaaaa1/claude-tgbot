"""缺陷②（公开 director.py）tick 级：触发器 × 忙碌集合（INTERFACE §2.6，注入点 §10.2）。
注入：d._jiwen_reader / d._global_cfg / d._db / d._holiday / d.call_claude_json 属性覆盖；
transcript 写 DIRECTOR_GT_DIR/<chat>.jsonl；状态 d._save_state；模式开关 DIRECTOR_MODE_DIR/<chat>。
DIRECTOR_NO_SPAWN=1 + PATH 拒绝桩：任何 tmux/claude 真调用都会被拦成 exit 97 并进 calls.log。
"""
import os
from types import SimpleNamespace

from _helpers import NOW, calls, gt_line, inbox, marker, switch_on

CHAT = "-100"
LLM = {"speak": True, "who": "bot2", "done": False, "dangling": False, "heat": 3, "reason": "r"}


def _base(d, iso, jiwen_bot=None, moments=None, holiday=None):
    """无新消息、无活跃场、冷场闸关着（last_initiate_ts 刚推进）。"""
    switch_on(iso, CHAT)
    gt_line(iso, CHAT, NOW - 3 * 3600, "昨天的话", mid=1)
    d._save_state(CHAT, {"last_ts": NOW - 3 * 3600, "last_mid": 1, "last_initiate_ts": NOW - 60})
    d._global_cfg = lambda: {}
    d._jiwen_reader = SimpleNamespace(read=lambda bot, human_id, cfg: (
        {"forced": True, "description": "已经撑不住了"} if bot == jiwen_bot else None))
    d._db = SimpleNamespace(list_moments=lambda limit, since_ts: list(moments or []))
    d._holiday = SimpleNamespace(is_holiday=lambda date: holiday)
    d.call_claude_json = lambda prompt, **kw: dict(LLM)


def test_模式开关缺失_返回off(director, iso):
    assert director.tick(CHAT, NOW) == {"action": "off"}


def test_jiwen触发_开场人不忙_写开场人inbox_场景激活(director, iso):
    _base(director, iso, jiwen_bot="bot1")
    r = director.tick(CHAT, NOW)
    assert r["action"] not in ("idle", "scene_skip", "off"), r
    assert len(inbox(iso, "bot1")) == 1 and inbox(iso, "bot2") == [] and inbox(iso, "bot3") == []
    assert director._load_state(CHAT)["scene"]["active"] is True
    assert calls(iso) == ""


def test_jiwen触发的bot正在私聊_无其它触发_idle_并打busy_skip_jiwen_opener(director, iso, capsys):
    _base(director, iso, jiwen_bot="bot1")
    marker(iso, "bot1", "123", int(NOW) - 300)
    assert director.tick(CHAT, NOW) == {"action": "idle"}
    assert "[director] busy_skip where=jiwen_opener bot=bot1 since_min=5" in capsys.readouterr().out
    assert inbox(iso, "bot1") == [] and inbox(iso, "bot2") == [] and inbox(iso, "bot3") == []
    assert director._load_state(CHAT).get("scene", {}).get("active") is not True


def test_两bot私聊中_jiwen触发第三个_scene_skip_理由列忙bot_不inject(director, iso):
    _base(director, iso, jiwen_bot="bot3")
    marker(iso, "bot1", "1", int(NOW) - 60)
    marker(iso, "bot2", "1", int(NOW) - 120)
    r = director.tick(CHAT, NOW)
    assert r == {"action": "scene_skip", "kind": "jiwen", "reason": "私聊忙碌(bot1,bot2)可用bot<2"}
    assert inbox(iso, "bot1") == [] and inbox(iso, "bot2") == [] and inbox(iso, "bot3") == []
    assert os.path.isfile(os.path.join(iso["st"], f"{CHAT}.json"))


def test_朋友圈触发_默认开场人忙_改选其它bot_不点发帖人(director, iso):
    m = [{"bot_id": "bot1", "ts": int(NOW) - 100, "text": "合成动态", "visibility": "public"}]
    _base(director, iso, moments=m)
    marker(iso, "bot2", "1", int(NOW) - 60)
    director.tick(CHAT, NOW)
    assert inbox(iso, "bot1") == [] and inbox(iso, "bot2") == []
    assert len(inbox(iso, "bot3")) == 1
    assert director._load_state(CHAT)["last_moment_seen_ts"] >= int(NOW) - 100


def test_朋友圈触发_除发帖人外全忙_不开场_但last_moment_seen_ts仍推进(director, iso):
    m = [{"bot_id": "bot1", "ts": int(NOW) - 100, "text": "合成动态", "visibility": "public"}]
    _base(director, iso, moments=m)
    marker(iso, "bot2", "1", int(NOW) - 60)
    marker(iso, "bot3", "1", int(NOW) - 60)
    r = director.tick(CHAT, NOW)
    assert r["action"] in ("idle", "scene_skip"), r
    for b in ("bot1", "bot2", "bot3"):
        assert inbox(iso, b) == []
    assert director._load_state(CHAT)["last_moment_seen_ts"] >= int(NOW) - 100


def test_特殊日触发_首选开场人忙_改选未忙bot(director, iso):
    _base(director, iso, holiday="中秋")
    marker(iso, "bot1", "1", int(NOW) - 60)
    director.tick(CHAT, NOW)
    assert inbox(iso, "bot1") == []
    assert len(inbox(iso, "bot2")) + len(inbox(iso, "bot3")) == 1


def test_特殊日触发_三bot全忙_不开场_不写任何inbox(director, iso):
    _base(director, iso, holiday="中秋")
    for b in ("bot1", "bot2", "bot3"):
        marker(iso, b, "1", int(NOW) - 60)
    r = director.tick(CHAT, NOW)
    assert r["action"] in ("idle", "scene_skip"), r
    for b in ("bot1", "bot2", "bot3"):
        assert inbox(iso, b) == []


def test_真人新消息分支_busy不影响_LLM点忙bot照常inject(director, iso):
    _base(director, iso)
    gt_line(iso, CHAT, NOW - 10, "刚说的话", mid=2)
    marker(iso, "bot2", "1", int(NOW) - 60)  # LLM 固定选 bot2，且 bot2 正在私聊
    director.tick(CHAT, NOW)
    assert len(inbox(iso, "bot2")) == 1, "真人新消息分支不看 busy（BRIEF ②）"
    assert inbox(iso, "bot1") == [] and inbox(iso, "bot3") == []


def test_jiwen开场_全程不触碰tmux_claude(director, iso):
    _base(director, iso, jiwen_bot="bot2")
    director.tick(CHAT, NOW)
    assert calls(iso) == "", calls(iso)
