# -*- coding: utf-8 -*-
"""clear_recap 的摘要落点、清空时刻解析与"每次清空只回顾一次"（纯函数 + 子进程，HOME 指向临时目录）。"""
import calendar
import importlib.util
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "clear_recap.py"
_spec = importlib.util.spec_from_file_location("clear_recap", _SCRIPT)
cr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cr)

NAME = "s.jsonl"


def _stamp(dt):
    return dt.strftime("%Y%m%dT%H%M%S")


def _setup(tmp_path):
    home, bot = tmp_path / "home", tmp_path / "bot"
    proj = home / ".claude" / "projects" / "-bot"
    proj.mkdir(parents=True)
    bot.mkdir()
    return home, bot, proj


def _run(home, bot, proj, **over):
    payload = dict({"source": "startup", "cwd": str(bot), "transcript_path": str(proj / NAME)}, **over)
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "USERPROFILE": str(home)}
    r = subprocess.run([sys.executable, str(_SCRIPT)], input=json.dumps(payload).encode(), capture_output=True,
                       env=env, timeout=30)
    return r.returncode, r.stdout.decode("utf-8"), r.stderr.decode("utf-8")


def test_摘要落点_有memory子目录读它_没有读会话目录(tmp_path):
    assert cr.note_path(str(tmp_path)) == os.path.join(str(tmp_path), "recent_conversation.md")
    (tmp_path / "memory").mkdir()
    assert cr.note_path(str(tmp_path)) == os.path.join(str(tmp_path), "memory", "recent_conversation.md")


def test_清空时刻_只认同名且合法的时刻串_取最新(tmp_path):
    for n in (f"{NAME}.deleted-20261024T080000", f"{NAME}.deleted-20261024T090000", f"{NAME}.deleted-20261399T000000",
              f"other.jsonl.deleted-20991231T000000", f"{NAME}.deleted-20991231T000000.bak"):
        (tmp_path / n).write_text("", encoding="utf-8")
    t, stamp = cr.latest_clear(str(tmp_path), NAME)
    assert stamp == "20261024T090000" and t == calendar.timegm((2026, 10, 24, 9, 0, 0))


def test_平铺落点_隔一小时才起会话也回顾_同一次清空只回顾一次(tmp_path):
    home, bot, proj = _setup(tmp_path)
    cleared = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    (proj / f"{NAME}.deleted-{_stamp(cleared)}").write_text("{}\n", encoding="utf-8")
    note = proj / "recent_conversation.md"                       # 没有 memory/ 子目录：摘要直接在会话目录
    note.write_text("# 摘要\n约好周六去看展。\n", encoding="utf-8")
    t = calendar.timegm(cleared.timetuple()) - 30
    os.utime(note, (t, t))
    rc, out, err = _run(home, bot, proj)
    assert rc == 0 and out.startswith(cr.HEAD) and "约好周六去看展。" in out
    assert err.startswith("clear_recap: ok ") and "看展" not in err
    assert (proj / f"{NAME}{cr.DONE_SUFFIX}").read_text(encoding="utf-8") == _stamp(cleared) + "\n"
    assert _run(home, bot, proj)[1] == ""


def test_cwd不是非空字符串_空输出(tmp_path):
    home, bot, proj = _setup(tmp_path)
    cleared = datetime.now(timezone.utc).replace(microsecond=0)
    (proj / f"{NAME}.deleted-{_stamp(cleared)}").write_text("{}\n", encoding="utf-8")
    (proj / "recent_conversation.md").write_text("摘要\n", encoding="utf-8")
    for bad in ("", None, 7):
        rc, out, err = _run(home, bot, proj, cwd=bad)
        assert rc == 0 and out == "" and err == ""
    assert _run(home, bot, proj)[1] != ""                       # 对照：cwd 合法时照常输出
