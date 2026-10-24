# -*- coding: utf-8 -*-
"""pub-parity：scripts/compact_private_context.py（私聊压缩补回，行为清单 21、22）与三个钩子脚本共同的非法输入契约。

会话内容一律由样例文件 tests/fixtures/inbound_private.txt 构造（由实现提供，测试只读）；
断言：stdout 的行、退出码 0、stderr 至多一行元数据。HOME 指向临时目录。
"""
import datetime
import os
import re
import sys

import pytest

from pp_helpers import (REPO, build_inbound, expect_line, hook_payload, iso_z, load_sample, make_home, meta_stderr_ok,
                        parse_inbound, projects_dir, reply_row, run_hook, sample_identity, user_row, write_jsonl)

SCRIPT = "compact_private_context.py"
LINE_RE = re.compile(r"^\d{2}-\d{2} \d{2}:\d{2} ")


def _mk_session(tmp_path, items):
    """items: [{body, prefix?, attrs?, reply?}]；返回会话环境 dict（含期望输出行 lines）。"""
    sample = load_sample()
    user_name, ts0, chat_id = sample_identity(sample)
    present = dict(parse_inbound(sample)["attrs"])
    base = datetime.datetime.fromisoformat(ts0.replace("Z", "+00:00"))
    home = make_home(tmp_path)
    bot = tmp_path / "channels" / "bot1"
    bot.mkdir(parents=True)
    tpath = projects_dir(home) / "11111111-2222-3333-4444-555555555555.jsonl"
    rows, lines, prev = [], [], None
    for i, it in enumerate(items):
        ts = iso_z(base + datetime.timedelta(minutes=i))
        ov = {k: v for k, v in (("ts", ts), ("message_id", str(100 + i))) if k in present}
        ov.update(it.get("attrs", {}))
        rows.append(user_row(f"u{i}", prev, build_inbound(sample, it["body"], prefix=it.get("prefix"), **ov), ts))
        prev = f"u{i}"
        if it.get("reply", True):
            rows.append(reply_row(f"a{i}", prev, chat_id, f"回复{i}", ts))
            prev = f"a{i}"
        lines.append(expect_line(user_name, ts, it["body"]))
    write_jsonl(tpath, rows)
    return {"home": home, "bot": bot, "tpath": tpath, "lines": lines, "name": user_name, "sample": sample,
            "present": present, "chat_id": chat_id}


def _run(s, source="compact", args=()):
    return run_hook(SCRIPT, s["home"], hook_payload(source, s["bot"], s["tpath"]), args)


# ---------------- 行为清单 21 ----------------
def test_压缩补回_样例会话_首行说明加每条一行_时刻名字正文(tmp_path):
    s = _mk_session(tmp_path, [{"body": "第一条私聊消息"}, {"body": "第二条私聊消息"}, {"body": "第三条私聊消息"}])
    rc, out, err = _run(s)
    assert rc == 0, err
    lines = out.splitlines()
    assert lines and lines[0].strip() and not LINE_RE.match(lines[0])         # 首行是说明，不是消息行
    idx = [lines.index(l) for l in s["lines"]]                                # 三条都取得出，格式逐字
    assert idx == sorted(idx) and idx[0] >= 1                                 # 按时间先后
    meta = meta_stderr_ok(err, "compact_private_context")
    assert s["name"] not in meta and "第一条" not in meta                      # stderr 不含名字与正文


@pytest.mark.parametrize("source", ["startup", "resume", "clear"])
def test_压缩补回_source不是compact_空输出_退出0(tmp_path, source):
    s = _mk_session(tmp_path, [{"body": "不该被补回"}])
    rc, out, err = _run(s, source=source)
    assert rc == 0 and out == ""
    meta_stderr_ok(err, "compact_private_context")


@pytest.mark.parametrize("where", ["projects之外的目录", "projects下的链接指向外面"])
def test_压缩补回_transcript不在家目录projects下_空输出_退出0(tmp_path, where):
    s = _mk_session(tmp_path, [{"body": "不该被补回"}])
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    real = outside / "x.jsonl"
    real.write_bytes(s["tpath"].read_bytes())
    if where == "projects之外的目录":
        s["tpath"] = real
    else:
        link = s["tpath"].parent / "link.jsonl"
        link.symlink_to(real)
        s["tpath"] = link
    rc, out, err = _run(s)
    assert rc == 0 and out == ""
    meta_stderr_ok(err, "compact_private_context")


# ---------------- 行为清单 22 ----------------
def test_压缩补回_开标签前不是私聊或群聊标注_整条不采用(tmp_path):
    s = _mk_session(tmp_path, [{"body": "正常消息一"}, {"body": "前缀不对的消息", "prefix": "随手写的前缀，不以标注结尾\n"},
                               {"body": "正常消息三"}])
    rc, out, err = _run(s)
    assert rc == 0, err
    assert s["lines"][0] in out.splitlines() and s["lines"][2] in out.splitlines()
    assert "前缀不对的消息" not in out


def test_压缩补回_群聊标注的入站_不当私聊补回(tmp_path):
    sample = load_sample()
    p = parse_inbound(sample)["prefix"]
    group_prefix = p.replace("【私聊】", "【群聊】") if "【私聊】" in p else p.rstrip() + "【群聊】"
    ov = {k: "group" for k in ("chat_type", "scene") if k in s_present(sample)}
    s = _mk_session(tmp_path, [{"body": "私聊消息"}, {"body": "群里的消息", "prefix": group_prefix, "attrs": ov}])
    rc, out, err = _run(s)
    assert rc == 0, err
    assert s["lines"][0] in out.splitlines() and "群里的消息" not in out


def s_present(sample):
    return dict(parse_inbound(sample)["attrs"])


@pytest.mark.parametrize("case", ["user_id与chat_id不一致", "chat_id不是纯数字"])
def test_压缩补回_身份校验不过_不采用(tmp_path, case):
    sample = load_sample()
    assert "user_id" in s_present(sample) and "chat_id" in s_present(sample), "样例开标签应有 chat_id 与 user_id"
    ov = {"user_id": "999000111"} if case == "user_id与chat_id不一致" else {"chat_id": "-1001234567890", "user_id": "-1001234567890"}
    s = _mk_session(tmp_path, [{"body": "正常消息"}, {"body": "身份不符的消息", "attrs": ov}])
    rc, out, err = _run(s)
    assert rc == 0, err
    assert s["lines"][0] in out.splitlines() and "身份不符的消息" not in out


def test_压缩补回_合成前缀表的每一项都被挡住_chat_history_INTERNAL_MARKERS(tmp_path):
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    import chat_history
    markers = [m for m in chat_history._INTERNAL_MARKERS if isinstance(m, str)]
    assert markers, "chat_history._INTERNAL_MARKERS 里应有字符串项"
    items = [{"body": "正常消息"}] + [{"body": f"{m} 合成内容{i}"} for i, m in enumerate(markers)]
    s = _mk_session(tmp_path, items)
    rc, out, err = _run(s)
    assert rc == 0, err
    assert s["lines"][0] in out.splitlines()                                  # 对照：正常消息取得出
    leaked = [m for i, m in enumerate(markers) if f"合成内容{i}" in out]
    assert leaked == [], f"这些合成前缀没被挡住: {leaked}"


def test_压缩补回_尾部还没回复的消息_不补回(tmp_path):
    s = _mk_session(tmp_path, [{"body": "已回复的消息"}, {"body": "还没回复的消息", "reply": False}])
    rc, out, err = _run(s)
    assert rc == 0, err
    assert s["lines"][0] in out.splitlines() and "还没回复的消息" not in out


def test_压缩补回_超过条数上限_截断(tmp_path):
    n = 300
    s = _mk_session(tmp_path, [{"body": f"批量消息{i:03d}"} for i in range(n)])
    rc, out, err = _run(s)
    assert rc == 0, err
    got = [l for l in out.splitlines() if "批量消息" in l]
    assert 1 <= len(got) < n, f"应截断到上限以内，实际 {len(got)} 条"


def test_压缩补回_超长正文_输出受长度上限约束(tmp_path):
    s = _mk_session(tmp_path, [{"body": "短消息"}, {"body": "长" * 60000}])
    rc, out, err = _run(s)
    assert rc == 0, err
    assert s["lines"][0] in out.splitlines()
    assert len(out) < 30000, f"60000 字的正文应被截断，输出长度 {len(out)}"


# ---------------- 钩子脚本共同契约 ----------------
def test_压缩补回_带命令行参数_写skip_BadArgs_空输出_退出0(tmp_path):
    s = _mk_session(tmp_path, [{"body": "消息"}])
    rc, out, err = _run(s, args=["--bogus"])
    assert rc == 0 and out == ""
    assert err.strip() == "compact_private_context: skip BadArgs"


BAD_CASES = ["非JSON文本", "JSON数组", "cwd为空串", "cwd不是字符串", "transcript后缀不是jsonl", "transcript是相对路径", "transcript在projects之外"]


@pytest.mark.parametrize("script,source", [("compact_private_context.py", "compact"), ("recall_private.py", "resume"),
                                           ("clear_recap.py", "startup")])
@pytest.mark.parametrize("case", BAD_CASES)
def test_三个脚本_非法输入_空输出_退出0_stderr至多一行元数据(tmp_path, script, source, case):
    home = make_home(tmp_path)
    bot = tmp_path / "channels" / "bot1"
    bot.mkdir(parents=True)
    tpath = projects_dir(home) / "s1.jsonl"
    tpath.write_text("", encoding="utf-8")
    good = hook_payload(source, bot, tpath)
    payload = {
        "非JSON文本": "this is not json",
        "JSON数组": "[1, 2, 3]",
        "cwd为空串": dict(good, cwd=""),
        "cwd不是字符串": dict(good, cwd=123),
        "transcript后缀不是jsonl": dict(good, transcript_path=str(projects_dir(home) / "s1.txt")),
        "transcript是相对路径": dict(good, transcript_path="s1.jsonl"),
        "transcript在projects之外": dict(good, transcript_path=str(tmp_path / "x.jsonl")),
    }[case]
    rc, out, err = run_hook(script, home, payload)
    assert rc == 0 and out == "", (rc, out, err)
    meta_stderr_ok(err, script[:-3])
