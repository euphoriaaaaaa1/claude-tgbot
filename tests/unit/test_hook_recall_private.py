# -*- coding: utf-8 -*-
"""recall_private 的重置点判定、清空下限与布防标记（纯函数 + 一次子进程端到端，HOME 指向临时目录）。"""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "recall_private.py"
_spec = importlib.util.spec_from_file_location("recall_private", _SCRIPT)
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)

SAMPLE = (_ROOT / "tests" / "fixtures" / "inbound_private.txt").read_text(encoding="utf-8").rstrip()
OPEN_END = SAMPLE.index(">", SAMPLE.rindex("<channel")) + 1
CHAT_ID = re.search(r'chat_id="([^"]+)"', SAMPLE).group(1)


def _inbound(body, mid, ts):
    head = SAMPLE[:OPEN_END].replace('message_id="7"', f'message_id="{mid}"')
    head = head.replace('ts="2026-10-24T08:30:00.000Z"', f'ts="{ts}"')
    return f"{head}\n{body}\n</channel>"


def _user(uuid, parent, body, mid, ts):
    return {"type": "user", "uuid": uuid, "parentUuid": parent, "timestamp": ts,
            "message": {"role": "user", "content": _inbound(body, mid, ts)}}


def _reply(uuid, parent, text, ts):
    return {"type": "assistant", "uuid": uuid, "parentUuid": parent, "timestamp": ts, "message": {"role": "assistant",
            "content": [{"type": "tool_use", "id": "t" + uuid, "name": "mcp__telegram-worker__reply",
                         "input": {"chat_id": CHAT_ID, "text": text}}]}}


def _lines(rows):
    return [json.dumps(r, ensure_ascii=False) for r in rows]


def _ts(minute):
    return f"2026-10-24T09:{minute:02d}:00.000Z"


def test_find_reset_会话首行的null父不算_守卫形态算且本行在上下文():
    first_only = _lines([_user("u0", None, "甲", 1, _ts(0)), _reply("a0", "u0", "回", _ts(0))])
    assert rp.find_reset(first_only) is None
    guarded = _lines([_user("u0", None, "甲", 1, _ts(0)), _reply("a0", "u0", "回", _ts(0)),
                      _user("u1", None, "乙", 2, _ts(1))])
    assert rp.find_reset(guarded) == 2 and rp.context_start(guarded, 2) == 2


def test_find_reset_原生边界与守卫总结行_其后才是上下文():
    boundary = {"type": "system", "subtype": "compact_boundary", "parentUuid": None, "timestamp": _ts(2)}
    rows = _lines([_user("u0", None, "甲", 1, _ts(0)), boundary, _user("u1", "b", "乙", 2, _ts(3))])
    assert rp.find_reset(rows) == 1 and rp.context_start(rows, 1) == 2
    summary = {"type": "user", "isCompactSummary": True, "parentUuid": None, "timestamp": _ts(0),
               "message": {"role": "user", "content": "总结"}}
    rows = _lines([summary, _user("u1", "s", "乙", 2, _ts(3))])
    assert rp.find_reset(rows) == 0 and rp.context_start(rows, 0) == 1


def test_清空下限_只认最新清空时刻_更早的备份不读(tmp_path):
    name = "s.jsonl"
    for n in ("s.jsonl.deleted-20261024T090000", "s.jsonl.deleted-20261020T000000", "other.jsonl.deleted-20991231T000000"):
        (tmp_path / n).write_text("", encoding="utf-8")
    floor = rp.cleared_floor(str(tmp_path), name)
    assert floor == datetime(2026, 10, 24, 9, 0, 0).replace(tzinfo=rp.timezone.utc).timestamp()
    old, new = int(floor) - 10, int(floor) + 10
    for sec in (old, new):
        (tmp_path / f"{name}.bak.{sec}.compact").write_text("", encoding="utf-8")
    assert [os.path.basename(p) for p in rp.list_backups(str(tmp_path), name, floor)] == [f"{name}.bak.{new}.compact"]


def _arm(bot, when):
    (bot / ".claude").mkdir(parents=True, exist_ok=True)
    marker = bot / ".claude" / "compact-recall.armed"
    marker.write_text(when.astimezone().isoformat(timespec="seconds") + "\n", encoding="utf-8")
    return marker


def test_布防标记_内容时刻与修改时刻都须在72小时内(tmp_path):
    marker = _arm(tmp_path, datetime.now())
    assert rp.marker_armed(str(tmp_path))
    _arm(tmp_path, datetime.now() - timedelta(hours=73))                 # 内容过期，修改时刻是新的
    assert not rp.marker_armed(str(tmp_path))
    marker.write_text("不是时刻\n", encoding="utf-8")
    assert not rp.marker_armed(str(tmp_path))
    _arm(tmp_path, datetime.now())
    old = time.time() - 73 * 3600
    os.utime(marker, (old, old))                                         # 修改时刻过期
    assert not rp.marker_armed(str(tmp_path))


def test_端到端_守卫压缩后补回被裁掉的私聊_标记用掉(tmp_path):
    home, bot = tmp_path / "home", tmp_path / "bot"
    proj = home / ".claude" / "projects" / "-bot"
    proj.mkdir(parents=True)
    bot.mkdir()
    tpath = proj / "s.jsonl"
    rows = [_user("u0", None, "被裁掉的甲", 1, _ts(0)), _reply("a0", "u0", "回甲", _ts(0)),
            _user("u1", None, "保留区首条", 2, _ts(5)), _reply("a1", "u1", "回首条", _ts(5))]
    tpath.write_text("\n".join(_lines(rows)) + "\n", encoding="utf-8")
    marker = _arm(bot, datetime.now())
    payload = {"source": "resume", "cwd": str(bot), "transcript_path": str(tpath)}
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "USERPROFILE": str(home), "TZ": "UTC"}
    r = subprocess.run([sys.executable, str(_SCRIPT)], input=json.dumps(payload).encode(), capture_output=True,
                       env=env, timeout=30)
    out = r.stdout.decode("utf-8")
    assert r.returncode == 0 and "被裁掉的甲" in out and "回甲" in out and "保留区首条" not in out
    assert not marker.exists() and len(list((bot / ".claude").glob("compact-recall.used-*"))) == 1
    assert r.stderr.decode().startswith("recall_private: ok ") and "被裁掉" not in r.stderr.decode()
