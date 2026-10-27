# -*- coding: utf-8 -*-
"""pub-parity：scripts/recall_private.py（一次性找回，行为清单 23）。

布防标记由补装命令 `--recall` 写出（保证格式与产品一致）；会话内容由样例文件构造；
公开版守卫压缩的形态 = 保留区首行是 parentUuid 为 null 的 user 行，且它之前已有带 parentUuid 键的行。
"""
import datetime
import glob
import os
import stat

import pytest

from pp_helpers import (arm_marker, build_inbound, hook_payload, iso_z, load_sample, make_bot, make_home, meta_stderr_ok,
                        parse_inbound, projects_dir, reply_row, run_hook, sample_identity, skip_if_root, skip_on_windows,
                        user_row, write_jsonl)

SCRIPT = "recall_private.py"
EARLIER = ["早先私聊甲", "早先私聊乙"]
IN_CONTEXT = "上下文内私聊丙"


def _session(tmp_path):
    """三条私聊：甲乙在重置行之前（应被找回），丙是重置行本身（已在上下文）。根行 parentUuid 也是 null，但不是重置行。"""
    sample = load_sample()
    name, ts0, chat_id = sample_identity(sample)
    present = dict(parse_inbound(sample)["attrs"])
    base = datetime.datetime.fromisoformat(ts0.replace("Z", "+00:00"))
    home = make_home(tmp_path)
    bot, _ = make_bot(tmp_path)
    tpath = projects_dir(home) / "22222222-3333-4444-5555-666666666666.jsonl"
    rows, prev = [], None
    for i, (body, parent_null) in enumerate([(EARLIER[0], True), (EARLIER[1], False), (IN_CONTEXT, True)]):
        ts = iso_z(base + datetime.timedelta(minutes=i))
        ov = {k: v for k, v in (("ts", ts), ("message_id", str(200 + i))) if k in present}
        parent = None if parent_null else prev
        rows.append(user_row(f"u{i}", parent, build_inbound(sample, body, **ov), ts))
        rows.append(reply_row(f"a{i}", f"u{i}", chat_id, f"回复{i}", ts))
        prev = f"a{i}"
    write_jsonl(tpath, rows)
    return {"home": home, "bot": bot, "tpath": tpath, "name": name}


def _run(s, source="resume", args=()):
    return run_hook(SCRIPT, s["home"], hook_payload(source, s["bot"], s["tpath"]), args)


def _claude_dir_state(bot):
    d = os.path.join(str(bot), ".claude")
    return sorted((n, os.stat(os.path.join(d, n)).st_mtime_ns) for n in os.listdir(d))


def _used(bot):
    return glob.glob(os.path.join(str(bot), ".claude", "compact-recall.used-*"))


def test_找回_没布防_空输出_不动文件(tmp_path):
    s = _session(tmp_path)
    before, tbytes = _claude_dir_state(s["bot"]), s["tpath"].read_bytes()
    rc, out, err = _run(s)
    assert rc == 0 and out == "", err
    assert _claude_dir_state(s["bot"]) == before and s["tpath"].read_bytes() == tbytes
    meta_stderr_ok(err, "recall_private")


def test_找回_布防且在72小时内_resume_输出更早的私聊_标记改名为used(tmp_path):
    s = _session(tmp_path)
    marker = arm_marker(s["home"], s["bot"])
    assert os.path.isfile(marker)
    rc, out, err = _run(s)
    assert rc == 0, err
    for body in EARLIER:
        assert body in out, f"{body} 应被找回"
    assert IN_CONTEXT not in out                                    # 已在上下文里的不重复补
    assert not os.path.exists(marker) and len(_used(s["bot"])) == 1  # 标记用掉：改名为 used-<时刻>
    meta = meta_stderr_ok(err, "recall_private")
    assert s["name"] not in meta and EARLIER[0] not in meta


def test_找回_用掉标记后再次触发_空输出(tmp_path):
    s = _session(tmp_path)
    arm_marker(s["home"], s["bot"])
    assert _run(s)[1] != ""
    used_before = _used(s["bot"])
    rc, out, err = _run(s)
    assert rc == 0 and out == "", err
    assert _used(s["bot"]) == used_before                           # 不再产生新的 used 记录


def test_找回_布防超过72小时_空输出(tmp_path):
    s = _session(tmp_path)
    marker = arm_marker(s["home"], s["bot"])
    t = datetime.datetime.fromisoformat(open(marker, encoding="utf-8").read().strip()) - datetime.timedelta(hours=73)
    with open(marker, "w", encoding="utf-8") as f:
        f.write(t.isoformat(timespec="seconds") + "\n")
    rc, out, err = _run(s)
    assert rc == 0 and out == "", err


@pytest.mark.parametrize("source", ["startup", "compact"])
def test_找回_source不是resume_空输出_标记不被消耗(tmp_path, source):
    s = _session(tmp_path)
    marker = arm_marker(s["home"], s["bot"])
    rc, out, err = _run(s, source=source)
    assert rc == 0 and out == "", err
    assert os.path.isfile(marker) and _used(s["bot"]) == []


def test_找回_带未知命令行参数_写skip_BadArgs_空输出_退出0(tmp_path):
    s = _session(tmp_path)
    marker = arm_marker(s["home"], s["bot"])
    rc, out, err = _run(s, args=["--bogus"])
    assert rc == 0 and out == ""
    assert err.strip() == "recall_private: skip BadArgs"
    assert os.path.isfile(marker)                                   # 参数不对时不动标记


@skip_if_root
@skip_on_windows
def test_找回_改名失败_照常先输出_标记留着下次再补(tmp_path):
    s = _session(tmp_path)
    marker = arm_marker(s["home"], s["bot"])
    cdir = os.path.dirname(marker)
    os.chmod(cdir, stat.S_IRUSR | stat.S_IXUSR)                     # 目录只读：标记改名必失败
    try:
        rc, out, err = _run(s)
    finally:
        os.chmod(cdir, stat.S_IRWXU)
    assert rc == 0, err
    assert all(b in out for b in EARLIER)                           # 先输出
    assert os.path.isfile(marker) and _used(s["bot"]) == []         # 改名失败：标记留着
    meta_stderr_ok(err, "recall_private")
