# -*- coding: utf-8 -*-
"""单只重启命令行在 Linux systemd 用户服务下套 ``systemd-run --user --scope``（安全复审 I-1）。

只改 ``sys.platform``、环境、``shutil.which``、cgroup 文件路径与 ``subprocess.run``（``systemd-run`` 试跑），
不起任何进程。
"""
from __future__ import annotations

import os
import subprocess

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


class _Probe:
    """``subprocess.run`` 替身：记下每次调用，按 ``outcome`` 返回退出码或抛异常。"""

    def __init__(self, outcome=0):
        self.outcome = outcome
        self.calls = []

    def __call__(self, args, **kw):
        self.calls.append((list(args), kw))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return subprocess.CompletedProcess(args, self.outcome)


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
    monkeypatch.setattr(claude_proc.subprocess, "run", _Probe(0))   # 默认试跑成功
    return cg


@pytest.fixture
def probe(monkeypatch):
    """替换试跑，返回替身以便查调用；用例改 ``probe.outcome`` 决定结果。"""
    fake = _Probe(0)
    monkeypatch.setattr(claude_proc.subprocess, "run", fake)
    return fake


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
def test_other_platforms_unaffected(env, probe, monkeypatch, plat):
    monkeypatch.setattr(claude_proc.sys, "platform", plat)
    argv = claude_proc.restart_one_argv(BOT)
    assert "systemd-run" not in argv
    if os.name != "nt":
        assert argv == ["bash", SCRIPT, BOT]
    assert probe.calls == []   # 非 Linux 不试跑


def test_test_seam_unchanged_on_linux_service(env, probe, monkeypatch):
    monkeypatch.setenv("CLAUDEBOT_TEST", "1")
    monkeypatch.setenv("CLAUDEBOT_RESTART_ONE_CMD", '["python3", "fake.py"]')
    assert claude_proc.restart_one_argv(BOT) == ["python3", "fake.py", BOT]
    assert probe.calls == []   # 测试接缝不试跑


def test_probe_success_gets_prefix_and_uses_same_prefix(env, probe):
    assert claude_proc.restart_one_argv(BOT) == PREFIX + ["bash", SCRIPT, BOT]
    assert len(probe.calls) == 1
    args, kw = probe.calls[0]
    assert args == PREFIX + ["true"]            # 与正式调用完全相同的前缀，不经 shell
    assert not kw.get("shell")
    assert kw.get("timeout") == 5
    for stream in ("stdin", "stdout", "stderr"):
        assert kw.get(stream) is subprocess.DEVNULL


@pytest.mark.parametrize("outcome", [
    1,                                                       # 老 systemd 不认 --collect
    4,
    -9,
    subprocess.TimeoutExpired(PREFIX + ["true"], 5),         # 用户管理器卡住
    OSError("exec failed"),
    FileNotFoundError("systemd-run"),
])
def test_probe_failure_no_prefix(env, probe, outcome):
    probe.outcome = outcome
    assert claude_proc.restart_one_argv(BOT) == ["bash", SCRIPT, BOT]
    assert len(probe.calls) == 1


def test_probe_not_cached(env, probe):
    """每次重启都重新试跑：环境变了（管理器恢复/失效）要跟着变。"""
    probe.outcome = 1
    assert claude_proc.restart_one_argv(BOT) == ["bash", SCRIPT, BOT]
    probe.outcome = 0
    assert claude_proc.restart_one_argv(BOT) == PREFIX + ["bash", SCRIPT, BOT]
    assert len(probe.calls) == 2


def test_no_probe_when_criteria_fail(env, probe, monkeypatch):
    """判据没过（不在用户服务里 / 没有 systemd-run）就不试跑。"""
    env.write_text("0::/user.slice/user-1000.slice/session-3.scope\n", encoding="utf-8")
    assert claude_proc.restart_one_argv(BOT) == ["bash", SCRIPT, BOT]
    env.write_text(V2_USER_SERVICE, encoding="utf-8")
    monkeypatch.setattr(claude_proc.shutil, "which", _which_missing)
    assert claude_proc.restart_one_argv(BOT) == ["bash", SCRIPT, BOT]
    assert probe.calls == []
