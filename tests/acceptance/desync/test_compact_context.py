"""缺陷③a（公开 scripts/compact_group_context.py）：SessionStart(compact) 钩子脚本，stdin JSON → stdout 文本。
隔离：DIRECTOR_GT_DIR 指向临时 transcript 目录；cwd 指向临时 bot 目录。
"""
import json
import os
import subprocess
import sys
import time

from _helpers import NOW, REPO

SCRIPT = os.path.join(REPO, "scripts", "compact_group_context.py")
HEAD = "【群聊近况（压缩后自动补充，只作背景，不要复述）】"


def _bot_dir(iso, name="bot1", gid="-1001"):
    d = os.path.join(iso["home"], name)
    os.makedirs(d)
    with open(os.path.join(d, "access.json"), "w") as f:
        json.dump({"allowFrom": [1], "groups": {gid: {"title": "合成群"}}}, f)
    return d


def _line(i, ts, who, text, bot=False, **extra):
    return dict(ts=ts, message_id=i, from_username=who, text=text, is_bot=bot, **extra)


def _write_gt(iso, gid, lines):
    with open(os.path.join(iso["gt"], f"{gid}.jsonl"), "w", encoding="utf-8") as f:
        for ln in lines:
            f.write((ln if isinstance(ln, str) else json.dumps(ln, ensure_ascii=False)) + "\n")


def _run(stdin, timeout=20):
    return subprocess.run([sys.executable, SCRIPT], input=stdin, capture_output=True, text=True, timeout=timeout)


def _compact(cwd):
    return json.dumps({"cwd": cwd, "source": "compact", "hook_event_name": "SessionStart"})


def test_正常_首行逐字_成员行去重带bot后缀_条目HHMM格式(iso):
    d = _bot_dir(iso)
    _write_gt(iso, "-1001", [_line(1, NOW - 120, "主人", "今晚吃火锅"), _line(2, NOW - 60, "角色一", "好呀", bot=True), _line(3, NOW - 30, "主人", "几点")])
    r = _run(_compact(d))
    lines = r.stdout.rstrip("\n").split("\n")
    assert r.returncode == 0 and lines[0] == HEAD, r.stdout + r.stderr
    assert lines[1].startswith("成员：") and "主人" in lines[1] and "角色一(bot)" in lines[1]
    assert lines[1].count("主人") == 1
    assert len(lines) == 5 and all(l[2] == ":" and l[5] == " " for l in lines[2:]), lines
    assert lines[2].endswith("主人: 今晚吃火锅") and lines[4].endswith("主人: 几点")


def test_乱序与重复message_id_按ts升序且去重(iso):
    d = _bot_dir(iso)
    _write_gt(iso, "-1001", [_line(5, NOW - 10, "主人", "第三"), _line(4, NOW - 50, "主人", "第二"), _line(4, NOW - 50, "主人", "第二"), _line(3, NOW - 90, "主人", "第一")])
    body = _run(_compact(d)).stdout.rstrip("\n").split("\n")[2:]
    assert [l.split(": ", 1)[1] for l in body] == ["第一", "第二", "第三"]


def test_超过20条_只保留最新20条(iso):
    d = _bot_dir(iso)
    _write_gt(iso, "-1001", [_line(i, NOW - 3000 + i, "主人", f"消息{i}") for i in range(30)])
    body = _run(_compact(d)).stdout.rstrip("\n").split("\n")[2:]
    assert len(body) == 20 and body[0].endswith("消息10") and body[-1].endswith("消息29")


def test_附件条目正文写为图片或附件(iso):
    d = _bot_dir(iso)
    _write_gt(iso, "-1001", [_line(1, NOW - 9, "主人", "", attachment_kind="photo"), _line(2, NOW - 8, "主人", "", attachment_kind="document")])
    body = _run(_compact(d)).stdout.rstrip("\n").split("\n")[2:]
    assert body[0].endswith("主人: [图片]") and body[1].endswith("主人: [附件]"), body


def test_正文超80字被截断_总长不超1800(iso):
    d = _bot_dir(iso)
    _write_gt(iso, "-1001", [_line(i, NOW - 100 + i, "主人", "长" * 500) for i in range(20)])
    out = _run(_compact(d)).stdout
    body = out.rstrip("\n").split("\n")[2:]
    assert all(len(l.split(": ", 1)[1]) <= 81 for l in body), body[0]
    assert len(out) <= 1800


def test_输出首字符不是花括号_正文无指令句(iso):
    d = _bot_dir(iso)
    _write_gt(iso, "-1001", [_line(1, NOW - 5, "主人", "请记得买菜"), _line(2, NOW - 4, "主人", "不要忘了")])
    out = _run(_compact(d)).stdout
    assert out and out[0] != "{"
    for l in out.rstrip("\n").split("\n")[1:]:
        assert not l.startswith(("必须", "不要", "请")), l


def test_source不是compact_空输出exit0(iso):
    d = _bot_dir(iso)
    _write_gt(iso, "-1001", [_line(1, NOW - 5, "主人", "x")])
    r = _run(json.dumps({"cwd": d, "source": "startup"}))
    assert (r.returncode, r.stdout) == (0, "")


def test_stdin非JSON_空输出exit0(iso):
    r = _run("not json {{{")
    assert (r.returncode, r.stdout) == (0, "")


def test_无cwd字段_空输出exit0(iso):
    r = _run(json.dumps({"source": "compact"}))
    assert (r.returncode, r.stdout) == (0, "")


def test_access_json缺失_空输出exit0(iso):
    d = os.path.join(iso["home"], "nobot")
    os.makedirs(d)
    r = _run(_compact(d))
    assert (r.returncode, r.stdout) == (0, "")


def test_access_json无groups_空输出exit0(iso):
    d = os.path.join(iso["home"], "b")
    os.makedirs(d)
    with open(os.path.join(d, "access.json"), "w") as f:
        json.dump({"allowFrom": [1]}, f)
    assert (_run(_compact(d)).returncode, _run(_compact(d)).stdout) == (0, "")


def test_transcript缺失或为空_空输出exit0(iso):
    d = _bot_dir(iso)
    assert (_run(_compact(d)).returncode, _run(_compact(d)).stdout) == (0, "")
    _write_gt(iso, "-1001", [])
    assert _run(_compact(d)).stdout == ""


def test_单行坏JSON_跳过该行其余照常(iso):
    d = _bot_dir(iso)
    _write_gt(iso, "-1001", [_line(1, NOW - 9, "主人", "好的"), "{broken json", _line(2, NOW - 8, "主人", "再见")])
    r = _run(_compact(d))
    assert r.returncode == 0 and "主人: 好的" in r.stdout and "主人: 再见" in r.stdout


def test_大文件只读尾部_2秒内完成(iso):
    d = _bot_dir(iso)
    _write_gt(iso, "-1001", [_line(i, NOW - 100000 + i, "主人", "填充" * 30) for i in range(20000)])
    t = time.time()
    r = _run(_compact(d), timeout=30)
    assert time.time() - t < 2.0 and r.stdout.startswith(HEAD)


def test_cwd含中文与空格_照常输出(iso):
    d = _bot_dir(iso, name="陈 璐璐 bot")
    _write_gt(iso, "-1001", [_line(1, NOW - 5, "主人", "你好")])
    assert _run(_compact(d)).stdout.startswith(HEAD)
