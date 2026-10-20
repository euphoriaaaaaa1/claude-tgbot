# -*- coding: utf-8 -*-
"""路径 → Claude Code project slug：三处消费者必须同源（chat_history / jiwen.tick / memory.memory_inject）。

Windows 路径含反斜杠与盘符冒号；旧写法只换 / 和 .，mac 上碰巧正确所以本机全绿。
"""
import importlib.util
import ntpath
import os
from pathlib import Path

import pytest

import chat_history
from memory import memory_inject

_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def tick():
    spec = importlib.util.spec_from_file_location("jiwen_tick", _ROOT / "jiwen" / "tick.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def windows_paths(monkeypatch):
    """在 POSIX 机器上模拟 Windows 的 abspath（ntpath 的纯 Python 实现不依赖 nt 模块）。"""
    monkeypatch.setattr(os.path, "abspath", ntpath.abspath)


def test_single_source(tick):
    assert tick._project_slug_for is chat_history._project_slug_for
    assert memory_inject._project_slug_for is chat_history._project_slug_for


def test_posix_slug():
    assert chat_history._project_slug_for("/Users/you/.claude/channels/bot") == "-Users-you--claude-channels-bot"


def test_windows_slug_matches_worker_manager(windows_paths):
    # 与 dispatcher/worker-manager.ts projectSlug() 注释里的期望一致
    assert chat_history._project_slug_for(r"C:\Users\you\.claude\channels\bot") == "C--Users-you--claude-channels-bot"


def test_memory_path_uses_windows_slug(windows_paths, monkeypatch):
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", "HOME"))
    p = memory_inject.memory_path(r"C:\Users\x\.claude\channels\b")
    assert p == "HOME/.claude/projects/C--Users-x--claude-channels-b/memory/MEMORY.md"


def test_jiwen_project_dir_uses_windows_slug(tick, windows_paths, monkeypatch):
    seen = []
    monkeypatch.setattr(os.path, "isdir", lambda p: (seen.append(p), False)[1])
    assert tick.find_recent_messages(r"C:\Users\x\.claude\channels\b", 0) == ([], 0)
    assert seen and os.path.basename(seen[-1]) == "C--Users-x--claude-channels-b"
