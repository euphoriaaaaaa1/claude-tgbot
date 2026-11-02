# -*- coding: utf-8 -*-
"""pub-memo-restart · C：scripts/recall_private.py 一次性找回（行为清单 1-11）。

钩子以子进程运行（stdin 喂 JSON），HOME 指临时目录，布防一律经 install_compact_hook.py --recall。
观察口只有：stdout、stderr、退出码、bot 目录 .claude/ 下的文件。
"""
import json
import os
import re
import stat
import subprocess
import sys
import time

import pytest

import pmr_helpers as H
from pmr_helpers import (EARLIER, HEAD, IN_CONTEXT, OK_LINE_PREFIX, arm, armed_path, file_state, inflight_path,
                         inflight_used_files, read_inflight, run_recall, standard_session, stderr_lines,
                         skip_if_root, skip_on_windows, used_files)

OK_LINE = re.compile(r"^recall_private: ok n=\d+ chars=\d+ newest=\S+ t_in=\S+ sources=\d+$")


def _first_resume(tmp_path):
    """布防并第一次 resume；返回 (会话, 调用前时刻, rc, out, err)。"""
    s = standard_session(tmp_path)
    arm(s)
    before = time.time()
    rc, out, err = run_recall(s)
    return s, before, rc, out, err


def _append_user_after_inflight(s, delta=10.0, **kw):
    """会话末尾追加一行 timestamp = inflight.t + delta 的用户行。"""
    t = read_inflight(s["bot"])["t"]
    H.append_row(s["tpath"], H.user_row("u_new", s["last_uuid"], "下一条用户消息", H.iso_ms(t + delta)))
    return t


# ───────────────── #1 ─────────────────
def test_C1_已布防第一次resume_输出补回并写inflight_count为1(tmp_path):
    s, before, rc, out, err = _first_resume(tmp_path)
    assert rc == 0, err
    assert out.startswith(HEAD)
    for body in EARLIER:
        assert body in out
    assert IN_CONTEXT not in out
    lines = stderr_lines(err)
    assert len(lines) == 1 and OK_LINE.match(lines[0]), err
    assert armed_path(s["bot"]).is_file()                               # 补回不再等于用掉：armed 仍在
    ip = inflight_path(s["bot"])
    raw = ip.read_text(encoding="utf-8")
    assert raw.endswith("\n") and raw.count("\n") == 1                   # 一行 JSON 加换行
    data = json.loads(raw)
    assert isinstance(data, dict) and set(data) == {"t", "count", "armed_ns"}
    assert data["count"] == 1 and type(data["count"]) is int
    assert data["armed_ns"] == armed_path(s["bot"]).stat().st_mtime_ns
    assert isinstance(data["t"], float) and data["t"] >= before
    if os.name != "nt":
        assert H.perm_bits(ip) == 0o600


# ───────────────── #2 ─────────────────
def test_C2_会话不动连续resume_补满五次后放弃并用掉标记(tmp_path):
    s, _, rc, out1, err = _first_resume(tmp_path)
    assert rc == 0, err
    for expect_count in (2, 3, 4, 5):
        rc, out, err = run_recall(s)
        assert rc == 0, err
        assert out == out1, "第 %d 次补回应与第一次相同" % expect_count
        assert read_inflight(s["bot"])["count"] == expect_count
        assert armed_path(s["bot"]).is_file()
    rc, out, err = run_recall(s)                                        # 第六次：已补满 5 次仍没落盘
    assert rc == 0, err
    assert out == ""
    assert not armed_path(s["bot"]).exists()
    assert "give-up attempts=5" in err
    assert len(used_files(s["bot"])) == 1


# ───────────────── #3 ─────────────────
@pytest.mark.parametrize("with_api_error", [False, True], ids=["其后无行", "其后跟接口报错的assistant行"])
def test_C3_补回内容已落盘_消费标记并留档(tmp_path, with_api_error):
    s, _, rc, _, err = _first_resume(tmp_path)
    assert rc == 0, err
    t = _append_user_after_inflight(s)
    if with_api_error:
        H.append_row(s["tpath"], H.api_error_row("a_err", "u_new", H.iso_ms(t + 11)))
    rc, out, err = run_recall(s)
    assert rc == 0, err
    assert out == ""
    assert not armed_path(s["bot"]).exists()
    used, inflight_used = used_files(s["bot"]), inflight_used_files(s["bot"])
    assert len(used) == 1 and len(inflight_used) == 1
    assert used[0].split("used-", 1)[1] == inflight_used[0].split("used-", 1)[1]    # 后缀相同
    assert not inflight_path(s["bot"]).exists()
    assert "consumed attempts=1" in err


# ───────────────── #4 ─────────────────
@pytest.mark.parametrize("kind", ["user行早于t", "user行缺timestamp", "只有晚于t的assistant行"])
def test_C4_没有真正落盘的证据_照常再补一次(tmp_path, kind):
    s, _, rc, _, err = _first_resume(tmp_path)
    assert rc == 0, err
    t = read_inflight(s["bot"])["t"]
    if kind == "user行早于t":
        row = H.user_row("u_old", s["last_uuid"], "早于t的用户行", H.iso_ms(t - 100))
    elif kind == "user行缺timestamp":
        row = H.user_row("u_nots", s["last_uuid"], "没有时刻的用户行", None)
    else:
        row = H.reply_row("a_late", s["last_uuid"], "1", "晚于t的助手行", H.iso_ms(t + 10))
    H.append_row(s["tpath"], row)
    rc, out, err = run_recall(s)
    assert rc == 0, err
    assert out.startswith(HEAD)
    assert read_inflight(s["bot"])["count"] == 2
    assert armed_path(s["bot"]).is_file()
    assert "consumed" not in err and "give-up" not in err


# ───────────────── #5 ─────────────────
def _bad_inflight_variants(armed_ns, tmp_path):
    ok = {"t": time.time() - 1000, "count": 2, "armed_ns": armed_ns}

    def js(**over):
        d = dict(ok)
        d.update(over)
        return json.dumps(d)
    return {
        "乱码": b"\xff\xfe not json at all",
        "不是JSON对象": b"[1, 2, 3]\n",
        "t是布尔": js(t=True).encode(),
        "t是1e400": ('{"t": 1e400, "count": 2, "armed_ns": %d}' % armed_ns).encode(),
        "count为0": js(count=0).encode(),
        "count为1.5": js(count=1.5).encode(),
        "count是字符串": js(count="1").encode(),
        "count是布尔": js(count=True).encode(),
        "缺armed_ns": json.dumps({"t": ok["t"], "count": 2}).encode(),
        "超过4KB": json.dumps(dict(ok, pad="x" * 5000)).encode(),
    }


_VARIANT_IDS = ["乱码", "不是JSON对象", "t是布尔", "t是1e400", "count为0", "count为1.5", "count是字符串",
                "count是布尔", "缺armed_ns", "超过4KB", "符号链接", "armed_ns对不上"]


@pytest.mark.parametrize("variant", _VARIANT_IDS)
def test_C5_inflight无效_按第一次注入处理并重写为count1(tmp_path, variant):
    s = standard_session(tmp_path)
    arm(s)
    ip = inflight_path(s["bot"])
    armed_ns = armed_path(s["bot"]).stat().st_mtime_ns
    link_target = None
    if variant == "符号链接":
        link_target = tmp_path / "elsewhere-inflight.json"
        link_target.write_text(json.dumps({"t": time.time() - 1000, "count": 2, "armed_ns": armed_ns}), encoding="utf-8")
        os.symlink(link_target, ip)
    elif variant == "armed_ns对不上":
        rc, out, err = run_recall(s)                                    # 先产生一份有效 inflight
        assert rc == 0 and read_inflight(s["bot"])["count"] == 1, err
        arm(s)                                                           # 又布防一次：armed 的修改时刻变了
        armed_ns = armed_path(s["bot"]).stat().st_mtime_ns
        assert read_inflight(s["bot"])["armed_ns"] != armed_ns
    else:
        ip.write_bytes(_bad_inflight_variants(armed_ns, tmp_path)[variant])
    target_before = link_target.read_bytes() if link_target else None
    rc, out, err = run_recall(s)
    assert rc == 0, err
    assert out.startswith(HEAD)
    assert not ip.is_symlink()
    data = read_inflight(s["bot"])
    assert data["count"] == 1
    assert data["armed_ns"] == armed_ns
    assert armed_path(s["bot"]).is_file()
    if link_target:
        assert link_target.read_bytes() == target_before                # 不跟随链接写到别处


# ───────────────── #6 ─────────────────
def test_C6_已布防但没有重置点_补出0条_当场用掉且不写inflight(tmp_path):
    s = H.make_session(tmp_path, EARLIER + [IN_CONTEXT], reset_index=None)
    arm(s)
    rc, out, err = run_recall(s)
    assert rc == 0, err
    assert out == ""
    lines = stderr_lines(err)
    assert len(lines) == 1 and lines[0].startswith("recall_private: ok n=0 "), err
    assert not armed_path(s["bot"]).exists()
    assert len(used_files(s["bot"])) == 1
    assert not inflight_path(s["bot"]).exists()


# ───────────────── #7 ─────────────────
def test_C7_inflight位置是目录_写不进去_退回改前的用一次即失效(tmp_path):
    s = standard_session(tmp_path)
    arm(s)
    os.mkdir(inflight_path(s["bot"]))
    rc, out, err = run_recall(s)
    assert rc == 0, err
    assert out.startswith(HEAD) and EARLIER[0] in out
    assert not armed_path(s["bot"]).exists()
    assert len(used_files(s["bot"])) == 1
    lines = stderr_lines(err)
    assert lines and lines[0].startswith(OK_LINE_PREFIX), err
    assert inflight_path(s["bot"]).is_dir()


# ───────────────── #8 ─────────────────
@skip_on_windows
@skip_if_root
def test_C8_消费时标记改名失败_本次不输出_留着下次再试(tmp_path):
    s, _, rc, _, err = _first_resume(tmp_path)
    assert rc == 0, err
    _append_user_after_inflight(s)
    armed_before, inflight_before = file_state(armed_path(s["bot"])), file_state(inflight_path(s["bot"]))
    cdir = os.path.dirname(armed_path(s["bot"]))
    os.chmod(cdir, stat.S_IRUSR | stat.S_IXUSR)                         # .claude 只读：改名必失败
    try:
        rc, out, err = run_recall(s)
    finally:
        os.chmod(cdir, stat.S_IRWXU)
    assert rc == 0, err
    assert out == ""
    assert "consumed" not in err
    assert file_state(armed_path(s["bot"])) == armed_before
    assert file_state(inflight_path(s["bot"])) == inflight_before
    rc, out, err = run_recall(s)                                        # 恢复可写后再来
    assert rc == 0, err
    assert "consumed attempts=1" in err
    assert not armed_path(s["bot"]).exists()


# ───────────────── #9 ─────────────────
def test_C9_同一秒内消费两次_第二份留档名带减1(tmp_path):
    names = []
    for attempt in range(8):       # 两次消费可能恰好跨秒：跨了就换一只 bot 重来
        s = H.make_session(tmp_path / ("try%d" % attempt), EARLIER + [IN_CONTEXT], reset_index=None)
        arm(s)
        assert run_recall(s)[0] == 0
        arm(s)
        assert run_recall(s)[0] == 0
        names = used_files(s["bot"])
        assert len(names) == 2, names
        base = min(names, key=len)
        if sorted(names) == sorted([base, base + "-1"]):
            return
    pytest.fail("8 次都没落在同一秒内，或留档名不是 <名> 与 <名>-1：%r" % names)


def test_C9_离线导出render_不读不写inflight(tmp_path):
    s = standard_session(tmp_path)
    arm(s)
    ip = inflight_path(s["bot"])
    assert not ip.exists()
    args = ["--render", "--cwd", str(s["bot"]), "--transcript", str(s["tpath"])]
    rc, out, err = H.run_py(H.RECALL, s["home"], args=args)
    assert rc == 0, err
    assert all(b in out for b in EARLIER)
    assert not ip.exists()                                              # 不生成
    armed_ns = armed_path(s["bot"]).stat().st_mtime_ns
    crafted = json.dumps({"t": time.time() - 1000, "count": 3, "armed_ns": armed_ns}) + "\n"
    ip.write_text(crafted, encoding="utf-8")
    before = file_state(ip)
    rc, out, err = H.run_py(H.RECALL, s["home"], args=args)
    assert rc == 0, err
    assert file_state(ip) == before                                     # 不改


# ───────────────── #10 ─────────────────
@pytest.mark.parametrize("case", ["没布防", "已布防_source为compact", "已布防_source为startup"])
def test_C10_不该触发的场景_空输出且不动标记不生成inflight(tmp_path, case):
    s = standard_session(tmp_path)
    source = "resume"
    before = None
    if case != "没布防":
        arm(s)
        before = file_state(armed_path(s["bot"]))
        source = "compact" if "compact" in case else "startup"
    rc, out, err = run_recall(s, source=source)
    assert rc == 0, err
    assert out == ""
    assert not inflight_path(s["bot"]).exists()
    if before is not None:
        assert file_state(armed_path(s["bot"])) == before
    else:
        assert not armed_path(s["bot"]).exists() and used_files(s["bot"]) == []


# ───────────────── #11 ─────────────────
LINK_BODY = "看这个\n[link] 好玩"
PHOTO_INJECT = "[auto-photo] 一张自动注入的照片说明\n"


def _items_with_traps(last_is_reset_row):
    items = [LINK_BODY, ("带注入行的消息", PHOTO_INJECT)]
    if last_is_reset_row:
        items.append("上下文内丙")
    return items


@pytest.mark.parametrize("source", ["resume", "compact"])
def test_C11_补回正文保留link行_不含auto_photo注入行(tmp_path, source):
    if source == "resume":
        s = H.make_session(tmp_path, _items_with_traps(True), reset_index=2)
        arm(s)
        rc, out, err = run_recall(s, source="resume")
    else:
        s = H.make_session(tmp_path, _items_with_traps(False), reset_index=None)
        payload = json.dumps(H.hook_payload("compact", s["bot"], s["tpath"]), ensure_ascii=False)
        rc, out, err = H.run_py(H.COMPACT_PRIVATE, s["home"], stdin_text=payload)
    assert rc == 0, err
    assert "[link] 好玩" in out
    assert "[auto-photo]" not in out
