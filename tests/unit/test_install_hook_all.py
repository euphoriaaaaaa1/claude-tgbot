# -*- coding: utf-8 -*-
"""install_compact_hook 的四条钩子：命令拼法、"已装"判定（带空格的引号路径）、diff 视图与 --recall 的退出码。

验收层（tests/acceptance/pub_parity/test_installer_four_hooks.py）已锁命令行行为；这里补纯函数与边角。
"""
import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / "scripts" / "install_compact_hook.py"
_spec = importlib.util.spec_from_file_location("install_compact_hook_all", _SCRIPT)
ich = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ich)

FOUR = [("compact", "compact_group_context.py"), ("compact", "compact_private_context.py"),
        ("resume", "recall_private.py"), ("startup", "clear_recap.py")]


def test_四条顺序与无参兼容():
    assert list(ich.HOOKS) == FOUR
    assert ich.hook_command() == ich.hook_command(script=ich.SCRIPT_NAME)
    assert ich.hook_item() == ich.hook_item("compact", ich.SCRIPT_NAME)


def test_windows命令_两段都带双引号_脚本都在本仓scripts下(monkeypatch):
    monkeypatch.setattr(ich.sys, "executable", r"C:\Users\example user\Python\python.exe")
    monkeypatch.setattr(ich, "HOOK_SCRIPT", "/srv/repo dir/scripts/compact_group_context.py")
    for _, script in FOUR:
        cmd = ich.hook_command(windows=True, script=script)
        assert cmd == rf'"C:\Users\example user\Python\python.exe" "/srv/repo dir/scripts/{script}"'
        assert shlex.split(cmd) == [r"C:\Users\example user\Python\python.exe", f"/srv/repo dir/scripts/{script}"]


def test_仓路径带空格_引号写法也算已装_重复合并没有变化(tmp_path, monkeypatch):
    scripts = tmp_path / "repo with space" / "scripts"
    scripts.mkdir(parents=True)
    for _, script in FOUR:
        (scripts / script).write_text("", encoding="utf-8")
    monkeypatch.setattr(ich, "HOOK_SCRIPT", str(scripts / ich.SCRIPT_NAME))
    once, changes = ich.merge_all({})
    assert len(changes) == 4 and all("'" in c for c in changes)          # POSIX 下带空格的路径被 shlex.quote 包住
    twice, again = ich.merge_all(once)
    assert again == [] and twice == once


def test_同脚本挂在别的matcher下_不算这一条_另行追加():
    s = {"hooks": {"SessionStart": [ich.hook_item("startup", "recall_private.py")]}}
    new, changes = ich.merge_all(s)
    assert new["hooks"]["SessionStart"][0] == s["hooks"]["SessionStart"][0]
    assert [c.split(" ")[0] for c in changes] == ["SessionStart[compact]", "SessionStart[compact]",
                                                    "SessionStart[resume]", "SessionStart[startup]"]


def test_merge_all不检查脚本存在_也不触盘(tmp_path, monkeypatch):
    monkeypatch.setattr(ich, "HOOK_SCRIPT", str(tmp_path / "gone" / "scripts" / ich.SCRIPT_NAME))
    new, changes = ich.merge_all({})
    assert len(new["hooks"]["SessionStart"]) == 4 and len(changes) == 4
    assert not (tmp_path / "gone").exists()


def test_diff视图_只留matcher与hooks_别人的钩子换占位():
    s = {"hooks": {"SessionStart": [
        {"matcher": "startup", "note": "inline-credential", "hooks": [{"type": "command", "command": "curl -H 'k: v' x"}]},
        ich.hook_item("resume", "recall_private.py")]}}
    view = ich._diff_view(s)
    assert view[0] == {"matcher": "startup", "hooks": [ich.HIDDEN]}
    assert view[1] == ich.hook_item("resume", "recall_private.py")
    assert ich._diff_view({}) == []


def _cli(tmp_path, *args):
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(tmp_path / "home"), "USERPROFILE": str(tmp_path / "home")}
    (tmp_path / "home").mkdir(exist_ok=True)
    return subprocess.run([sys.executable, str(_SCRIPT), *args], capture_output=True, text=True, env=env, timeout=30)


def test_recall遇到没有项目设置_退出2_不建任何东西(tmp_path):
    bot = tmp_path / "bot"
    bot.mkdir()
    r = _cli(tmp_path, str(bot), "--recall")
    assert r.returncode == 2 and "no project settings dir, skip" in r.stdout and os.listdir(bot) == []
    (bot / ".claude").mkdir()
    r = _cli(tmp_path, str(bot), "--recall")
    assert r.returncode == 2 and "no settings.json, skip" in r.stdout and os.listdir(bot / ".claude") == []


def test_装完后命令可被shell按两段切开_第一段是当前解释器(tmp_path):
    bot = tmp_path / "bot"
    (bot / ".claude").mkdir(parents=True)
    (bot / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    r = _cli(tmp_path, str(bot))
    assert r.returncode == 0, r.stdout + r.stderr
    ss = json.loads((bot / ".claude" / "settings.json").read_text(encoding="utf-8"))["hooks"]["SessionStart"]
    for it, (matcher, script) in zip(ss, FOUR):
        toks = shlex.split(it["hooks"][0]["command"])
        assert it["matcher"] == matcher and toks == [sys.executable, str(_ROOT / "scripts" / script)]
        assert it["hooks"][0]["timeout"] == 10
