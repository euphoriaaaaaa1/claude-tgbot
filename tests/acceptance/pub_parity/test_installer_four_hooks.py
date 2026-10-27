# -*- coding: utf-8 -*-
"""pub-parity：补装命令 scripts/install_compact_hook.py <bot_dir> [--dry-run] [--recall]（行为清单 18、19、20）。

只经命令行（退出码 + stdout + 文件字节）与接口文件列出的公开函数 merge_all 观察。
隔离：bot 目录与 HOME 都在 pytest 临时目录；令牌为假值。
"""
import copy
import datetime
import glob
import importlib.util
import json
import os
import re
import shlex
import stat
import sys

import pytest

from pp_helpers import (FAKE_TOKEN, FOUR_HOOKS, GROUP_HOOK, INSTALLER, PRIVATE_HOOK, RECALL_HOOK, RECAP_HOOK,
                        make_bot, make_home, run_installer, script_path, skip_if_root, skip_on_windows)

BASE = {"model": "haiku", "permissions": {"allow": ["Read"]},
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "echo pre"}]}]}}
CHANGE = re.compile(r"^SessionStart\[(\w+)\] (installed|replaced): (.+)$")


def _cmd(name):
    return f"{sys.executable} {script_path(name)}"


def _item(matcher, command, timeout=10):
    return {"matcher": matcher, "hooks": [{"type": "command", "command": command, "timeout": timeout}]}


def _load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _bytes(p):
    with open(p, "rb") as f:
        return f.read()


def _backups(bot):
    return sorted(glob.glob(os.path.join(str(bot), ".claude", "settings.json.bak*")))


def _changes(stdout):
    return [m.groups() for m in (CHANGE.match(l) for l in stdout.splitlines()) if m]


def _managed(settings):
    """SessionStart 里本安装器管理的项 -> [(matcher, 脚本文件名)]，按文件内顺序。"""
    out = []
    for it in settings.get("hooks", {}).get("SessionStart", []):
        for h in it.get("hooks", []):
            toks = shlex.split(h.get("command", ""))
            names = [os.path.basename(t) for t in toks]
            hit = [n for n in names if n in {x[1] for x in FOUR_HOOKS}]
            if hit:
                out.append((it.get("matcher"), hit[0]))
    return out


def _installed(tmp_path):
    """建 bot 并用补装命令装齐四条；返回 (home, bot, settings 路径)。"""
    home = make_home(tmp_path)
    bot, sp = make_bot(tmp_path, settings=copy.deepcopy(BASE))
    rc, out, err = run_installer(home, bot)
    assert rc == 0, out + err
    assert _managed(_load(sp)) == FOUR_HOOKS, "前置：补装一次应装齐四条（群聊、私聊、找回、回顾）"
    return home, bot, sp


# ---------------- 行为清单 18：补齐、幂等、解释器宽松、脚本路径不同则替换 ----------------
def test_补装_只有群聊一条_补齐另三条_原有项与其它键原样保留(tmp_path):
    home = make_home(tmp_path)
    mem = {"matcher": "startup", "hooks": [{"type": "command", "command": "echo mem"}]}
    s = copy.deepcopy(BASE)
    s["hooks"]["SessionStart"] = [_item("compact", _cmd(GROUP_HOOK)), mem]
    bot, sp = make_bot(tmp_path, settings=s)
    rc, out, err = run_installer(home, bot)
    assert rc == 0, out + err
    new = _load(sp)
    ss = new["hooks"]["SessionStart"]
    assert ss[:2] == s["hooks"]["SessionStart"]                      # 原有两项原样保留在前
    assert [(m, n) for m, n in _managed(new)] == FOUR_HOOKS          # 四条齐，群聊只一条
    assert new["model"] == "haiku" and new["permissions"] == BASE["permissions"]
    assert new["hooks"]["PreToolUse"] == BASE["hooks"]["PreToolUse"]
    got = _changes(out)
    assert [(m, v) for m, v, _ in got] == [("compact", "installed"), ("resume", "installed"), ("startup", "installed")]
    for (_, _, cmd), name in zip(got, [PRIVATE_HOOK, RECALL_HOOK, RECAP_HOOK]):
        toks = shlex.split(cmd)
        assert toks[0] == sys.executable and os.path.realpath(toks[-1]) == os.path.realpath(script_path(name))
    assert re.search(r"^backup: .+", out, re.M) and re.search(r"^written: .+", out, re.M)
    assert len(_backups(bot)) == 1


def test_补装_装齐后再跑一次_already_installed_退出0_文件不变_不再备份(tmp_path):
    home, bot, sp = _installed(tmp_path)
    before, nb = _bytes(sp), len(_backups(bot))
    rc, out, err = run_installer(home, bot)
    assert rc == 0 and out.startswith("already installed: ") and out.strip().endswith(str(sp))
    assert _bytes(sp) == before and len(_backups(bot)) == nb


def test_补装_解释器换成另一个存在的解释器_仍算已装(tmp_path):
    home, bot, sp = _installed(tmp_path)
    other = tmp_path / "other-python"
    other.write_text("#!/bin/sh\n", encoding="utf-8")
    other.chmod(0o755)
    s = _load(sp)
    for it in s["hooks"]["SessionStart"]:
        for h in it["hooks"]:
            h["command"] = h["command"].replace(sys.executable, str(other), 1)
    sp.write_text(json.dumps(s, indent=2), encoding="utf-8")
    before, nb = _bytes(sp), len(_backups(bot))
    rc, out, err = run_installer(home, bot)
    assert rc == 0 and "already installed" in out, out + err
    assert _bytes(sp) == before and len(_backups(bot)) == nb      # 不替换、不备份


def test_补装_解释器已不存在_换成当前解释器(tmp_path):
    home, bot, sp = _installed(tmp_path)
    s = _load(sp)
    for it in s["hooks"]["SessionStart"]:
        for h in it["hooks"]:
            h["command"] = h["command"].replace(sys.executable, "/nonexistent-python-xyz/bin/python", 1)
    sp.write_text(json.dumps(s, indent=2), encoding="utf-8")
    rc, out, err = run_installer(home, bot)
    assert rc == 0, out + err
    assert [v for _, v, _ in _changes(out)] == ["replaced"] * 4
    new = _load(sp)
    assert _managed(new) == FOUR_HOOKS
    for it in new["hooks"]["SessionStart"]:
        assert shlex.split(it["hooks"][0]["command"])[0] == sys.executable


@pytest.mark.parametrize("case", ["仓库挪过位置", "同一脚本旧写两条"])
def test_补装_脚本路径不同_replaced_且该脚本仍只一条(tmp_path, case):
    home, bot, sp = _installed(tmp_path)
    s = _load(sp)
    ss = s["hooks"]["SessionStart"]
    idx = [i for i, it in enumerate(ss) if PRIVATE_HOOK in it["hooks"][0]["command"]][0]
    old = f"{sys.executable} /old/place/scripts/{PRIVATE_HOOK}"
    ss[idx] = _item("compact", old)
    if case == "同一脚本旧写两条":
        ss.insert(idx, _item("compact", f"{sys.executable} /older/place/scripts/{PRIVATE_HOOK}"))
    sp.write_text(json.dumps(s, indent=2), encoding="utf-8")
    rc, out, err = run_installer(home, bot)
    assert rc == 0, out + err
    assert [(m, v) for m, v, _ in _changes(out)] == [("compact", "replaced")]
    new = _load(sp)
    assert _managed(new).count(("compact", PRIVATE_HOOK)) == 1
    cmds = [it["hooks"][0]["command"] for it in new["hooks"]["SessionStart"] if PRIVATE_HOOK in it["hooks"][0]["command"]]
    assert os.path.realpath(shlex.split(cmds[0])[-1]) == os.path.realpath(script_path(PRIVATE_HOOK))


# ---------------- 行为清单 19：dry-run 与错误退出码 ----------------
def test_dry_run_有变化_只打印_不写_不备份_不布防(tmp_path):
    home = make_home(tmp_path)
    bot, sp = make_bot(tmp_path, settings=copy.deepcopy(BASE))
    before = _bytes(sp)
    rc, out, err = run_installer(home, bot, "--dry-run")
    assert rc == 0, out + err
    assert "hooks.SessionStart (before)" in out and "hooks.SessionStart (after)" in out
    assert len(_changes(out)) == 4 and "dry-run: nothing written" in out
    assert _bytes(sp) == before and _backups(bot) == []
    assert not os.path.exists(os.path.join(str(bot), ".claude", "compact-recall.armed"))


@pytest.mark.parametrize("with_recall", [False, True])
def test_dry_run_四条已装_already_installed_不写_带recall不布防(tmp_path, with_recall):
    home, bot, sp = _installed(tmp_path)
    before, nb = _bytes(sp), len(_backups(bot))
    flags = ["--dry-run"] + (["--recall"] if with_recall else [])
    rc, out, err = run_installer(home, bot, *flags)
    assert rc == 0 and "already installed: " in out, out + err
    assert ("dry-run: recall not armed" in out) is with_recall
    assert _bytes(sp) == before and len(_backups(bot)) == nb
    assert not os.path.exists(os.path.join(str(bot), ".claude", "compact-recall.armed"))


def test_dry_run加recall_有变化_输出recall_not_armed_不建标记(tmp_path):
    home = make_home(tmp_path)
    bot, sp = make_bot(tmp_path, settings=copy.deepcopy(BASE))
    before = _bytes(sp)
    rc, out, err = run_installer(home, bot, "--dry-run", "--recall")
    assert rc == 0, out + err
    assert "dry-run: nothing written" in out and "dry-run: recall not armed" in out
    assert "recall armed:" not in out
    assert _bytes(sp) == before and _backups(bot) == []
    assert not os.path.exists(os.path.join(str(bot), ".claude", "compact-recall.armed"))


@pytest.mark.parametrize("case,expect_rc", [("结构不合预期", 3), ("目标是全局设置", 4)])
def test_带recall遇到错误_退出码3或4_不建标记_不写(tmp_path, case, expect_rc):
    home = make_home(tmp_path)
    if case == "结构不合预期":
        bot, sp = make_bot(tmp_path, raw=json.dumps({"hooks": []}))
        marker = os.path.join(str(bot), ".claude", "compact-recall.armed")
    else:
        (home / ".claude").mkdir()
        sp = home / ".claude" / "settings.json"
        sp.write_text("{}", encoding="utf-8")
        bot, marker = home, str(home / ".claude" / "compact-recall.armed")
    before = _bytes(sp)
    rc, out, err = run_installer(home, bot, "--recall")
    assert rc == expect_rc, out + err
    if expect_rc == 4:
        assert "refuse: global settings" in out
    assert _bytes(sp) == before and not os.path.exists(marker)


# ---------------- 行为清单 20：diff 视图不泄露、布防标记、写失败 ----------------
@pytest.mark.parametrize("dry", [True, False])
def test_diff只含SessionStart视图_令牌与别的钩子不出现_占位替代(tmp_path, dry):
    home = make_home(tmp_path)
    s = copy.deepcopy(BASE)
    s["env"] = {"ANTHROPIC_AUTH_TOKEN": FAKE_TOKEN, "ANTHROPIC_BASE_URL": "http://example.invalid"}
    s["hooks"]["SessionStart"] = [{"matcher": "startup", "hooks": [{"type": "command", "command": "echo mem-private-note"}]}]
    bot, sp = make_bot(tmp_path, settings=s)
    rc, out, err = run_installer(home, bot, *(["--dry-run"] if dry else []))
    assert rc == 0, out + err
    both = out + err
    assert FAKE_TOKEN not in both and "ANTHROPIC_AUTH_TOKEN" not in both and "example.invalid" not in both
    assert "echo mem-private-note" not in both and "echo pre" not in both and '"haiku"' not in both
    assert "<hook not managed here, hidden>" in out
    assert "hooks.SessionStart (before)" in out and "hooks.SessionStart (after)" in out


def test_recall_写布防标记_一行时刻_权限0600_先于备份与写入打印(tmp_path):
    home = make_home(tmp_path)
    bot, sp = make_bot(tmp_path, settings=copy.deepcopy(BASE))
    marker = os.path.join(str(bot), ".claude", "compact-recall.armed")
    rc, out, err = run_installer(home, bot, "--recall")
    assert rc == 0, out + err
    text = open(marker, encoding="utf-8").read()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}([+-]\d{2}:\d{2})?\n?", text), repr(text)
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(marker).st_mode) == 0o600
    lines = out.splitlines()
    idx = {k: next(i for i, l in enumerate(lines) if l.startswith(k)) for k in ("recall armed: ", "backup: ", "written: ")}
    assert lines[idx["recall armed: "]] == f"recall armed: {marker}"
    assert idx["recall armed: "] < idx["backup: "] < idx["written: "]
    assert not [n for n in os.listdir(os.path.dirname(marker)) if ".tmp-" in n]      # 临时名已 replace 掉


def test_不带recall_不布防(tmp_path):
    home = make_home(tmp_path)
    bot, sp = make_bot(tmp_path, settings=copy.deepcopy(BASE))
    rc, out, err = run_installer(home, bot)
    assert rc == 0, out + err
    assert _managed(_load(sp)) == FOUR_HOOKS                # 命令确实跑完了四条（避免"什么都没做所以没布防"的空转）
    assert not os.path.exists(os.path.join(str(bot), ".claude", "compact-recall.armed")) and "recall armed" not in out


@skip_on_windows
def test_recall_标记已存在_重跑即刷新时刻_四条已装也布防(tmp_path):
    home, bot, sp = _installed(tmp_path)
    marker = os.path.join(str(bot), ".claude", "compact-recall.armed")
    with open(marker, "w", encoding="utf-8") as f:
        f.write("2020-01-01T00:00:00\n")
    rc, out, err = run_installer(home, bot, "--recall")
    assert rc == 0 and "already installed" in out and f"recall armed: {marker}" in out, out + err
    t = datetime.datetime.fromisoformat(open(marker, encoding="utf-8").read().strip())
    now = datetime.datetime.now(t.tzinfo) if t.tzinfo else datetime.datetime.now()
    assert abs((now - t).total_seconds()) < 300


@skip_if_root
def test_recall_标记写不成_退出5_settings字节不变_也没备份(tmp_path):
    home = make_home(tmp_path)
    bot, sp = make_bot(tmp_path, settings=copy.deepcopy(BASE))
    marker = os.path.join(str(bot), ".claude", "compact-recall.armed")
    os.makedirs(marker)                                    # 标记路径被一个非空目录占着：新建/改名都会失败
    open(os.path.join(marker, "x"), "w").close()
    before = _bytes(sp)
    rc, out, err = run_installer(home, bot, "--recall")
    assert rc == 5, out + err
    assert re.search(r"^write failed: [A-Za-z_]\w*$", out, re.M), out
    assert _bytes(sp) == before and _backups(bot) == []    # 标记先写、标记失败时 settings 与备份都没动
    assert "written:" not in out


# ---------------- 公开函数 merge_all（接口文件：不改入参、不触盘、已是结果则无变化） ----------------
def _ich():
    spec = importlib.util.spec_from_file_location("install_compact_hook_pp", INSTALLER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_merge_all_空设置_四条按序_不改入参():
    ich = _ich()
    src = copy.deepcopy(BASE)
    snapshot = copy.deepcopy(src)
    new, changes = ich.merge_all(src)
    assert src == snapshot                                  # 不改入参
    assert _managed(new) == FOUR_HOOKS and len(changes) == 4
    assert new["hooks"]["PreToolUse"] == BASE["hooks"]["PreToolUse"] and new["model"] == "haiku"


def test_merge_all_已是结果_再合并无变化():
    ich = _ich()
    once, _ = ich.merge_all(copy.deepcopy(BASE))
    twice, changes = ich.merge_all(once)
    assert twice == once and len(changes) == 0
