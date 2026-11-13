# -*- coding: utf-8 -*-
"""moments.shell_path：发给 bot 照抄的命令里，解释器与脚本路径各自加引号后仍是完整一段。"""
import shlex

from moments import shell_path


def test_posix_path_with_space_stays_one_segment():
    py = "/Users/A B/venv/bin/python3"
    script = "/Users/A B/my repo/scripts/moment_reply.py"
    cmd = f'{shell_path(py, windows=False)} {shell_path(script, windows=False)} 12 0 "<你的评论>"'
    assert shlex.split(cmd) == [py, script, "12", "0", "<你的评论>"]


def test_windows_path_keeps_backslash_and_space():
    py = r"C:\Users\A B\AppData\Local\Programs\Python\Python312\python.exe"
    script = r"C:\Users\A B\claudebotlife\scripts\moment_like.py"
    cmd = f"{shell_path(py, windows=True)} {shell_path(script, windows=True)} 7"
    # Git Bash 按 POSIX 规则拆分：双引号内反斜杠后跟普通字母时原样保留
    assert shlex.split(cmd, posix=True) == [py, script, "7"]


def test_safe_path_unchanged():
    p = "/opt/claudebotlife/scripts/moment_like.py"
    assert shell_path(p, windows=False) == p
