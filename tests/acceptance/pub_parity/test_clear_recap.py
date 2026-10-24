# -*- coding: utf-8 -*-
"""pub-parity：scripts/clear_recap.py（清空后回顾，行为清单 24、25）。

构造方式（均出自接口文件）：/clear 把会话改名为 <会话文件名>.deleted-<UTC时刻 YYYYmmddTHHMMSS>；
摘要在每次清空时写下，判定看它的修改时刻是否落在最新清空时刻 T 前 600 秒到后 60 秒之间；
每次清空只回顾一次，记录文件是 <会话文件名>.recap-done（内容是这次的 T）。
假设（接口文件未写明、取自现有 h23_h24 测试的目录布局）：摘要文件是 <cwd>/memory/recent_conversation.md；
为稳妥，同内容也放一份到会话目录旁的 memory/ 下，二者修改时刻一致。
"""
import calendar
import datetime
import os
import stat

import pytest

from pp_helpers import hook_payload, make_home, meta_stderr_ok, projects_dir, run_hook, skip_if_root, skip_on_windows

SCRIPT = "clear_recap.py"
NAME = "33333333-4444-5555-6666-777777777777.jsonl"
SUMMARY = "# 最近对话摘要\n\n更新：清空时写下\n\n用户聊了周末去爬山的计划，约好周六早上出发。\n"
UTC = datetime.timezone.utc


def _stamp(t):
    return t.strftime("%Y%m%dT%H%M%S")


def _epoch(t):
    return calendar.timegm(t.timetuple())


def _now_utc():
    return datetime.datetime.now(UTC).replace(microsecond=0, tzinfo=None)


def _write_summary(env, text, mtime_epoch):
    for d in (env["bot"] / "memory", env["tdir"] / "memory"):
        d.mkdir(parents=True, exist_ok=True)
        p = d / "recent_conversation.md"
        p.write_text(text, encoding="utf-8")
        os.utime(p, (mtime_epoch, mtime_epoch))


def _setup(tmp_path, deleted=(), summary_mtime=None, text=SUMMARY):
    home = make_home(tmp_path)
    bot = tmp_path / "channels" / "bot1"
    bot.mkdir(parents=True)
    tdir = projects_dir(home)
    tpath = tdir / NAME
    tpath.write_text("", encoding="utf-8")
    env = {"home": home, "bot": bot, "tdir": tdir, "tpath": tpath}
    for t in deleted:
        (tdir / f"{NAME}.deleted-{_stamp(t)}").write_text('{"old": "session"}\n', encoding="utf-8")
    if summary_mtime is not None:
        _write_summary(env, text, summary_mtime)
    return env


def _run(env, source="startup", args=()):
    return run_hook(SCRIPT, env["home"], hook_payload(source, env["bot"], env["tpath"]), args)


def _done(env):
    return env["tdir"] / f"{NAME}.recap-done"


def _has_recap(out, text=SUMMARY):
    body = text.strip()
    return body in out and out.index(body) > 0 and len(out.strip()) > len(body)      # 说明 + 全文


# ---------------- 行为清单 24 ----------------
def test_回顾_清空一小时后才起会话_摘要在窗口内_照样输出说明加全文(tmp_path):
    t = _now_utc() - datetime.timedelta(hours=1)
    env = _setup(tmp_path, deleted=[t], summary_mtime=_epoch(t) - 120)
    rc, out, err = _run(env)
    assert rc == 0, err
    assert _has_recap(out), out
    meta = meta_stderr_ok(err, "clear_recap")
    assert "爬山" not in meta


@pytest.mark.parametrize("offset,expect_output", [(-700, False), (120, False), (-300, True), (30, True)])
def test_回顾_摘要修改时刻相对清空时刻_窗口内输出_窗口外不输出(tmp_path, offset, expect_output):
    t = _now_utc() - datetime.timedelta(hours=1)
    env = _setup(tmp_path, deleted=[t], summary_mtime=_epoch(t) + offset)
    rc, out, err = _run(env)
    assert rc == 0, err
    assert (out != "") is expect_output, f"偏移 {offset} 秒: {out!r}"
    if expect_output:
        assert _has_recap(out)


def test_回顾_没有清空记录_空输出(tmp_path):
    env = _setup(tmp_path, deleted=[], summary_mtime=_epoch(_now_utc()))
    rc, out, err = _run(env)
    assert rc == 0 and out == "", err


def test_回顾_摘要不存在_空输出(tmp_path):
    env = _setup(tmp_path, deleted=[_now_utc() - datetime.timedelta(hours=1)], summary_mtime=None)
    rc, out, err = _run(env)
    assert rc == 0 and out == "", err


@pytest.mark.parametrize("summary_near,expect_output", [("较早一次清空", False), ("最新一次清空", True)])
def test_回顾_有多次清空记录_只看最新一次的时刻(tmp_path, summary_near, expect_output):
    old_t = _now_utc() - datetime.timedelta(days=2)
    new_t = _now_utc() - datetime.timedelta(hours=1)
    near = old_t if summary_near == "较早一次清空" else new_t
    env = _setup(tmp_path, deleted=[old_t, new_t], summary_mtime=_epoch(near) - 60)
    rc, out, err = _run(env)
    assert rc == 0, err
    assert (out != "") is expect_output, out


@pytest.mark.parametrize("source", ["resume", "compact", "clear"])
def test_回顾_source不是startup_空输出(tmp_path, source):
    t = _now_utc() - datetime.timedelta(hours=1)
    env = _setup(tmp_path, deleted=[t], summary_mtime=_epoch(t) - 60)
    rc, out, err = _run(env, source=source)
    assert rc == 0 and out == "", err
    assert not _done(env).exists()


def test_回顾_命令行参数被忽略_照常输出(tmp_path):
    t = _now_utc() - datetime.timedelta(hours=1)
    env = _setup(tmp_path, deleted=[t], summary_mtime=_epoch(t) - 60)
    rc, out, err = _run(env, args=["--whatever", "x"])
    assert rc == 0 and _has_recap(out), (out, err)


# ---------------- 行为清单 25 ----------------
def test_回顾_输出后写recap_done_内容是这次的T_权限0600_无临时文件残留(tmp_path):
    t = _now_utc() - datetime.timedelta(hours=1)
    env = _setup(tmp_path, deleted=[t], summary_mtime=_epoch(t) - 60)
    rc, out, err = _run(env)
    assert rc == 0 and _has_recap(out), err
    done = _done(env)
    assert done.is_file() and done.read_text(encoding="utf-8").strip() == _stamp(t)
    assert len(done.read_text(encoding="utf-8").strip().splitlines()) == 1
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(done).st_mode) == 0o600
    assert not [n for n in os.listdir(env["tdir"]) if ".tmp-" in n]
    assert not done.name.endswith(".jsonl") and not done.name.startswith((".bak.", ".deleted-"))


def test_回顾_同一次清空已回顾过_再来startup_空输出_记录不变(tmp_path):
    t = _now_utc() - datetime.timedelta(hours=1)
    env = _setup(tmp_path, deleted=[t], summary_mtime=_epoch(t) - 60)
    assert _run(env)[1] != ""
    record = _done(env).read_bytes()
    rc, out, err = _run(env)
    assert rc == 0 and out == "", err
    assert _done(env).read_bytes() == record


def test_回顾_之后又清空一次_T变新_再输出一次_记录更新为新T(tmp_path):
    t1 = _now_utc() - datetime.timedelta(hours=2)
    env = _setup(tmp_path, deleted=[t1], summary_mtime=_epoch(t1) - 60)
    assert _run(env)[1] != ""
    t2 = _now_utc() - datetime.timedelta(hours=1)
    (env["tdir"] / f"{NAME}.deleted-{_stamp(t2)}").write_text('{"old": "session2"}\n', encoding="utf-8")
    new_text = SUMMARY.replace("爬山", "看电影")
    _write_summary(env, new_text, _epoch(t2) - 30)
    rc, out, err = _run(env)
    assert rc == 0 and _has_recap(out, new_text), (out, err)
    assert _done(env).read_text(encoding="utf-8").strip() == _stamp(t2)


@skip_if_root
@skip_on_windows
def test_回顾_已回顾记录写失败_stderr只记类名_本次照常输出(tmp_path):
    t = _now_utc() - datetime.timedelta(hours=1)
    env = _setup(tmp_path, deleted=[t], summary_mtime=_epoch(t) - 60)
    os.chmod(env["tdir"], stat.S_IRUSR | stat.S_IXUSR)               # 会话目录只读：写不了记录
    try:
        rc, out, err = _run(env)
    finally:
        os.chmod(env["tdir"], stat.S_IRWXU)
    assert rc == 0 and _has_recap(out), (out, err)                   # 本次照常输出
    assert err.strip() == "clear_recap: skip PermissionError"
    assert not _done(env).exists()
