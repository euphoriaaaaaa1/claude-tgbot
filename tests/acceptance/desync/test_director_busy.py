"""缺陷②（公开 director.py）：常量、_busy_bots 读 marker、_log_busy 限流、_pick_other。"""
import os

import pytest

from _helpers import NOW, marker

BUSY_LINE = "[director] busy_skip where="


def test_常量_PRIVATE_BUSY_MIN_30_限流300_MARKER_DIR来自env(director, iso):
    assert director.PRIVATE_BUSY_MIN == 30
    assert director.BUSY_LOG_THROTTLE_SEC == 300
    assert os.path.abspath(director.MARKER_DIR) == os.path.abspath(iso["mk"])


def test_busy_bots_marker刚写_该bot忙_since为0(director, iso):
    marker(iso, "bot1", "123", int(NOW))
    r = director._busy_bots(NOW)
    assert set(r) == {"bot1"} and r["bot1"] == 0


def test_busy_bots_29分钟前_仍忙_since约29(director, iso):
    marker(iso, "bot2", "123", int(NOW) - 29 * 60)
    r = director._busy_bots(NOW)
    assert set(r) == {"bot2"} and 28.9 <= r["bot2"] <= 29.1


def test_busy_bots_恰好30分钟_不忙(director, iso):
    marker(iso, "bot2", "123", int(NOW) - 30 * 60)
    assert director._busy_bots(NOW) == {}


def test_busy_bots_同bot多个chat取最大ts(director, iso):
    marker(iso, "bot3", "111", int(NOW) - 3 * 3600)
    marker(iso, "bot3", "222", int(NOW) - 60)
    r = director._busy_bots(NOW)
    assert set(r) == {"bot3"} and 0.9 <= r["bot3"] <= 1.1


def test_busy_bots_未来30秒内_算忙_since截为0(director, iso):
    marker(iso, "bot1", "1", int(NOW) + 30)
    r = director._busy_bots(NOW)
    assert r == {"bot1": 0}


def test_busy_bots_未来超过60秒_不忙(director, iso):
    marker(iso, "bot1", "1", int(NOW) + 61)
    assert director._busy_bots(NOW) == {}


def test_busy_bots_内容非整数_忽略并打busy_marker_bad一次(director, iso, capsys):
    p = marker(iso, "bot1", "1", "abc")
    assert director._busy_bots(NOW) == {}
    director._busy_bots(NOW)
    out = capsys.readouterr().out
    assert out.count("[director] busy_marker_bad bot=bot1 err=ValueError") == 1, out


def test_busy_bots_内容为空_忽略不抛(director, iso):
    marker(iso, "bot2", "1", "")
    assert director._busy_bots(NOW) == {}


def test_busy_bots_带换行的int_合法(director, iso):
    marker(iso, "bot2", "1", f"{int(NOW) - 10}\n")
    assert set(director._busy_bots(NOW)) == {"bot2"}


def test_busy_bots_三bot全无文件_打busy_marker_missing一次(director, iso, capsys):
    director._busy_bots(NOW)
    director._busy_bots(NOW)
    out = capsys.readouterr().out
    assert out.count(f"[director] busy_marker_missing dir={iso['mk']}") == 1, out


def test_busy_bots_部分bot有文件_不打missing(director, iso, capsys):
    marker(iso, "bot1", "1", int(NOW))
    director._busy_bots(NOW)
    assert "busy_marker_missing" not in capsys.readouterr().out


def test_busy_bots_marker目录不存在_不抛_返回空(director, iso, capsys):
    os.rmdir(iso["mk"])
    assert director._busy_bots(NOW) == {}


def test_busy_bots_不受HUMAN_ID影响_任意chat后缀都算(director, iso):
    marker(iso, "bot1", "-1001234567890", int(NOW) - 5)
    assert set(director._busy_bots(NOW)) == {"bot1"}


def test_busy_bots_不匹配的文件名_不算(director, iso):
    # 只有 <BOT_DIR_NAME>-*.last-user 才算；.last / 其它 bot 名不算
    with open(os.path.join(iso["mk"], "bot1-1.last"), "w") as f:
        f.write(str(int(NOW)))
    marker(iso, "unknownbot", "1", int(NOW))
    assert director._busy_bots(NOW) == {}


def test_log_busy_首次打印返回True_300秒内同键不打返回False(director, capsys):
    assert director._log_busy("jiwen_opener", "bot1", 12.7, NOW) is True
    assert director._log_busy("jiwen_opener", "bot1", 13.0, NOW + 299) is False
    out = capsys.readouterr().out
    assert out.count(f"{BUSY_LINE}jiwen_opener bot=bot1 since_min=12") == 1, out


def test_log_busy_不同where或bot_各自独立(director, capsys):
    director._log_busy("scene_gate", "bot1", 1, NOW)
    assert director._log_busy("scene_turn", "bot1", 1, NOW) is True
    assert director._log_busy("scene_gate", "bot2", 1, NOW) is True
    assert capsys.readouterr().out.count(BUSY_LINE) == 3


def test_log_busy_超过300秒后再打(director, capsys):
    director._log_busy("initiate", "bot3", 0, NOW)
    assert director._log_busy("initiate", "bot3", 5, NOW + 301) is True
    assert capsys.readouterr().out.count(f"{BUSY_LINE}initiate bot=bot3") == 2


def test_pick_other_按BOTS顺序取第一个未排除(director):
    assert director._pick_other({"bot1"}) == "bot2"
    assert director._pick_other(set()) == "bot1"


def test_pick_other_全排除返回None(director):
    assert director._pick_other({"bot1", "bot2", "bot3"}) is None
