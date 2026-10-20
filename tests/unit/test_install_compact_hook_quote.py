# -*- coding: utf-8 -*-
"""install_compact_hook 的 hook command 引号规则（纯函数）。

Windows 上解释器默认在 %LOCALAPPDATA%\\Programs\\Python（含用户名），用户名/仓路径含空格时裸拼接会断；
不含特殊字符的 POSIX 路径形态必须保持不变（验收锁定测试按 command.split()[0] == sys.executable 断言）。
"""
import importlib.util
import shlex
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "install_compact_hook.py"
_spec = importlib.util.spec_from_file_location("install_compact_hook", _SCRIPT)
ich = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ich)


def test_posix_plain_path_unchanged():
    assert ich.quote_hook_path("/opt/homebrew/bin/python3.12", windows=False) == "/opt/homebrew/bin/python3.12"


@pytest.mark.parametrize("p", ["/Users/john smith/venv/bin/python", "/repo/a&b/scripts/compact_group_context.py"])
def test_posix_special_path_quoted_and_roundtrips(p):
    q = ich.quote_hook_path(p, windows=False)
    assert q != p and shlex.split(q) == [p]


@pytest.mark.parametrize("p", [
    r"C:\Users\John Smith\AppData\Local\Programs\Python\Python312\python.exe",
    r"C:\Users\zhang\AppData\Local\Programs\Python\Python312\python.exe",
])
def test_windows_always_double_quoted(p):
    assert ich.quote_hook_path(p, windows=True) == f'"{p}"'
    # Git Bash / sh -c 下双引号内反斜杠保留（后随普通字母），路径不会被吃成 C:Usersx
    assert shlex.split(ich.quote_hook_path(p, windows=True), posix=True) == [p]


def test_hook_command_two_segments_with_spaces(monkeypatch):
    monkeypatch.setattr(ich.sys, "executable", r"C:\Users\John Smith\Python\python.exe")
    monkeypatch.setattr(ich, "HOOK_SCRIPT", r"C:\Users\John Smith\claudebotlife\scripts\compact_group_context.py")
    cmd = ich.hook_command(windows=True)
    assert cmd == '"C:\\Users\\John Smith\\Python\\python.exe" "C:\\Users\\John Smith\\claudebotlife\\scripts\\compact_group_context.py"'


def test_our_command_recognizes_quoted_posix_paths_with_spaces(monkeypatch):
    # 幂等识别（_our_command）仍认得带引号形态；反斜杠形态的 basename 只有 ntpath 会切，这里用 POSIX 路径验
    monkeypatch.setattr(ich.sys, "executable", "/Users/john smith/venv/bin/python")
    monkeypatch.setattr(ich, "HOOK_SCRIPT", "/Users/john smith/repo/scripts/compact_group_context.py")
    cmd = ich.hook_command(windows=False)
    assert cmd == "'/Users/john smith/venv/bin/python' '/Users/john smith/repo/scripts/compact_group_context.py'"
    assert ich._our_command({"hooks": [{"type": "command", "command": cmd}]}) == cmd


def test_template_command_still_recognized_as_ours():
    tmpl = 'python3 "$CLAUDEBOTLIFE_REPO/scripts/compact_group_context.py"'
    assert ich._our_command({"hooks": [{"type": "command", "command": tmpl}]}) == tmpl
