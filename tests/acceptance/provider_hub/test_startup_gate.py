# -*- coding: utf-8 -*-
"""§1.1 开关与默认拒绝启动 —— 进程级契约，只能起子进程验。

安全口径：被起的是**被测门户自己**，不是外部服务；一律 127.0.0.1 + 随机空闲端口，
带硬超时并在 finally 里杀掉；CLAUDE_SETTINGS_PATH 指 tmp。绝不联网、绝不起 cliproxy。
"""
import os
import socket
import subprocess
import sys
import time

import pytest

from conftest import REPO_ROOT

ENTRY = REPO_ROOT / "moments" / "web.py"
PW = "hub-access-pw-12345"


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _run(env_over, settings_path, wait=6.0):
    """起一次门户，返回 (returncode|None, stdout, stderr)。None = 到点还活着（说明启动了）。"""
    if not ENTRY.exists():
        pytest.fail("门户入口不存在：%s（实现未落地，红基线预期内）" % ENTRY)
    env = dict(os.environ)
    env.pop("HUB_ACCESS_PASSWORD", None)
    env.update({"CLAUDE_SETTINGS_PATH": str(settings_path),
                "MOMENTS_WEB_PORT": str(_free_port()), "PYTHONUNBUFFERED": "1"})
    env.update({k: str(v) for k, v in env_over.items()})
    p = subprocess.Popen([sys.executable, str(ENTRY)], cwd=str(REPO_ROOT), env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = p.communicate(timeout=wait)
        return p.returncode, out, err
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate(timeout=10)
        return None, out, err


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.7", "not-a-real-host.invalid"])
def test_无口令且监听非回环_拒绝启动退出码2(settings_path, host):
    """§1.1 第二行：非回环（含**解析失败的主机名**，fail-closed）→ REFUSE + exit(2)，不 bind。"""
    rc, out, err = _run({"MOMENTS_WEB_HOST": host}, settings_path, wait=3.0)
    assert rc == 2, "host=%s 未拒绝启动（rc=%r），公网无口令暴露门户" % (host, rc)
    assert any(l.startswith("REFUSE:") for l in err.splitlines()), \
        "stderr 缺少以 REFUSE: 开头的说明行"


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_无口令但监听回环_正常启动(settings_path, host):
    """§1.1 第一行：老用户零改造，回环 + 无口令必须能起来。"""
    rc, out, err = _run({"MOMENTS_WEB_HOST": host}, settings_path, wait=5.0)
    assert rc != 2, "host=%s 被误判为非回环而拒绝启动：%s" % (host, err[-400:])
    assert "REFUSE:" not in err


def test_设了口令时监听非回环可以启动(settings_path):
    """§1.1 第三行：口令已设 → 门开启，监听哪里都行。"""
    rc, out, err = _run({"MOMENTS_WEB_HOST": "0.0.0.0", "HUB_ACCESS_PASSWORD": PW},
                        settings_path, wait=5.0)
    assert rc != 2, "设了口令仍拒绝启动：%s" % err[-400:]


def test_短口令只打WARN不拒绝启动(settings_path):
    """§1.1 口令强度：<12 位 → 一行 WARN: 开头的警告，不拒绝（不误伤老用户）。"""
    rc, out, err = _run({"MOMENTS_WEB_HOST": "127.0.0.1", "HUB_ACCESS_PASSWORD": "short"},
                        settings_path, wait=5.0)
    assert rc != 2
    assert any(l.startswith("WARN:") for l in (out + err).splitlines()), "短口令未告警"


def test_口令值不进启动日志(settings_path):
    """S8 反向用例：启动横幅不得回显口令明文。"""
    rc, out, err = _run({"MOMENTS_WEB_HOST": "127.0.0.1", "HUB_ACCESS_PASSWORD": PW},
                        settings_path, wait=5.0)
    assert PW not in out and PW not in err
