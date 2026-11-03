# -*- coding: utf-8 -*-
"""单只重启命令行在 Linux systemd 用户服务下套 ``systemd-run --user --scope``（安全复审 I-1）。

只改 ``sys.platform``、环境、``shutil.which`` 与 cgroup 文件路径，不起任何进程。
"""
from __future__ import annotations

import os

import pytest

import adapters.claude_proc as claude_proc

BOT = "mybot"
PREFIX = ["systemd-run", "--user", "--scope", "--collect", "--quiet", "--"]
SCRIPT = os.path.join(claude_proc._REPO, "dispatcher", "restart-one.sh")

V2_USER_SERVICE = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/provider-web.service\n"
V1_USER_SERVICE = ("12:pids:/user.slice/user-1000.slice/user@1000.service\n"
                   "1:name=systemd:/user.slice/user-1000.slice/user@1000.service/provider-web.service\n")


def _which_found(name):
    return "/usr/bin/" + name


def _which_missing(name):
    return None


@pytest.fixture
def env(monkeypatch, tmp_path):
    """默认：Linux、在用户服务里、systemd-run 可用、没开测试接缝。用例按需改其中一项。"""
    cg = tmp_path / "cgroup"
    cg.write_text(V2_USER_SERVICE, encoding="utf-8")
    monkeypatch.setattr(claude_proc.sys, "platform", "linux")
    monkeypatch.setattr(claude_proc, "_PROC_CGROUP", str(cg))
    monkeypatch.setattr(claude_proc.shutil, "which", _which_found)
    monkeypatch.delenv("CLAUDEBOT_TEST", raising=False)
    monkeypatch.delenv("CLAUDEBOT_RESTART_ONE_CMD", raising=False)
    return cg


def test_linux_user_service_with_systemd_run_gets_prefix(env):
    assert claude_proc.restart_one_argv(BOT) == PREFIX + ["bash", SCRIPT, BOT]


def test_cgroup_v1_user_service_gets_prefix(env):
    env.write_text(V1_USER_SERVICE, encoding="utf-8")
    assert claude_proc.restart_one_argv(BOT)[:len(PREFIX)] == PREFIX


def test_no_systemd_run_no_prefix(env, monkeypatch):
    monkeypatch.setattr(claude_proc.shutil, "which", _which_missing)
    assert claude_proc.restart_one_argv(BOT) == ["bash", SCRIPT, BOT]


@pytest.mark.parametrize("content", [
    "0::/user.slice/user-1000.slice/session-3.scope\n",                    # ssh / 登录会话
    "0::/user.slice/user-1000.slice/user@1000.service/app.slice/vte-spawn-1.scope\n",  # 桌面终端
    "0::/user.slice/user-1000.slice/user@1000.service/init.scope\n",
    "0::/system.slice/provider-web.service\n",                             # 系统级服务：没有用户管理器
    "0::/\n",                                                              # 容器内
    "3:cpu:/user.slice/user-1000.slice/user@1000.service/x.service\n",    # 非 systemd 层级
    "",
    "garbage\n",
])
def test_not_in_user_service_no_prefix(env, content):
    env.write_text(content, encoding="utf-8")
    assert claude_proc.restart_one_argv(BOT) == ["bash", SCRIPT, BOT]


def test_cgroup_file_missing_no_prefix(env, monkeypatch, tmp_path):
    monkeypatch.setattr(claude_proc, "_PROC_CGROUP", str(tmp_path / "nope"))
    assert claude_proc.restart_one_argv(BOT) == ["bash", SCRIPT, BOT]


def test_invocation_id_alone_is_not_enough(env, monkeypatch):
    """服务里开出的终端也继承 INVOCATION_ID；只看 cgroup。"""
    env.write_text("0::/user.slice/user-1000.slice/session-3.scope\n", encoding="utf-8")
    monkeypatch.setenv("INVOCATION_ID", "0123456789abcdef0123456789abcdef")
    assert claude_proc.restart_one_argv(BOT) == ["bash", SCRIPT, BOT]


@pytest.mark.parametrize("plat", ["darwin", "win32", "cygwin"])
def test_other_platforms_unaffected(env, monkeypatch, plat):
    monkeypatch.setattr(claude_proc.sys, "platform", plat)
    argv = claude_proc.restart_one_argv(BOT)
    assert "systemd-run" not in argv
    if os.name != "nt":
        assert argv == ["bash", SCRIPT, BOT]


def test_test_seam_unchanged_on_linux_service(env, monkeypatch):
    monkeypatch.setenv("CLAUDEBOT_TEST", "1")
    monkeypatch.setenv("CLAUDEBOT_RESTART_ONE_CMD", '["python3", "fake.py"]')
    assert claude_proc.restart_one_argv(BOT) == ["python3", "fake.py", BOT]
