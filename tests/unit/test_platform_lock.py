# -*- coding: utf-8 -*-
"""platform_lock 跨平台锁单测。

POSIX 本地实跑；Windows 专用用例按 ``sys.platform`` 跳过并注明 T02 已验的基本行为。
锁文件只落在 ``tmp_path``，不碰真实配置、不连服务、不起 8767。
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import platform_lock

REPO_ROOT = Path(platform_lock.__file__).resolve().parent


def open_fd(path) -> int:
    return os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)


def _read_log(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "<无输出>"


# ---------------------------------------------------------------- 基本契约

def test_锁文件不存在时创建并加锁(tmp_path):
    lock = tmp_path / "features.json.lock"
    assert not lock.exists()
    with platform_lock.file_lock(lock) as fd:
        assert isinstance(fd, int)
        assert lock.exists()
    if os.name == "posix":
        mode = stat.S_IMODE(os.stat(lock).st_mode)
        assert mode & 0o600 == 0o600
        assert mode & 0o077 == 0


def test_传入fd不关闭调用方的描述符(tmp_path):
    lock = tmp_path / "fd.lock"
    fd = open_fd(lock)
    try:
        with platform_lock.file_lock(fd) as got:
            assert got == fd
        os.fstat(fd)  # 仍打开：传 fd 时生命周期归调用方
        assert platform_lock.acquire(fd, blocking=False) is True
        platform_lock.release(fd)
    finally:
        os.close(fd)


def test_两个文件描述符互斥(tmp_path):
    lock = tmp_path / "contend.lock"
    fd1, fd2 = open_fd(lock), open_fd(lock)
    try:
        assert platform_lock.acquire(fd1, blocking=False) is True
        assert platform_lock.acquire(fd2, blocking=False) is False
        platform_lock.release(fd1)
        assert platform_lock.acquire(fd2, blocking=False) is True
        platform_lock.release(fd2)
    finally:
        os.close(fd1)
        os.close(fd2)


def test_非阻塞被占用立即抛LockBusy(tmp_path):
    lock = tmp_path / "busy.lock"
    with platform_lock.file_lock(lock):
        started = time.monotonic()
        with pytest.raises(platform_lock.LockBusy):
            with platform_lock.file_lock(lock, blocking=False):
                pass
        assert time.monotonic() - started < 1.0


def test_超时抛LockBusy(tmp_path):
    lock = tmp_path / "timeout.lock"
    holding = threading.Event()
    release_holder = threading.Event()

    def holder():
        with platform_lock.file_lock(lock, timeout=10):
            holding.set()
            release_holder.wait(10)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    try:
        assert holding.wait(5), "辅助线程没拿到锁"
        started = time.monotonic()
        with pytest.raises(platform_lock.LockBusy):
            with platform_lock.file_lock(lock, timeout=0.2):
                pass
        elapsed = time.monotonic() - started
        assert 0.15 <= elapsed < 2.0
    finally:
        release_holder.set()
        thread.join(5)


def test_阻塞等待期间对方释放后能拿到锁(tmp_path):
    lock = tmp_path / "wait.lock"
    holding = threading.Event()

    def holder():
        with platform_lock.file_lock(lock, timeout=10):
            holding.set()
            time.sleep(0.2)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    try:
        assert holding.wait(5)
        started = time.monotonic()
        with platform_lock.file_lock(lock, timeout=3):
            pass
        assert time.monotonic() - started < 3
    finally:
        thread.join(5)


def test_上下文异常也释放锁(tmp_path):
    lock = tmp_path / "exception.lock"
    with pytest.raises(ValueError):
        with platform_lock.file_lock(lock):
            raise ValueError("boom")
    with platform_lock.file_lock(lock, timeout=0.5):
        pass


def test_负超时报ValueError(tmp_path):
    with pytest.raises(ValueError):
        with platform_lock.file_lock(tmp_path / "neg.lock", timeout=-0.1):
            pass


def test_父目录不存在(tmp_path):
    with pytest.raises(FileNotFoundError):
        with platform_lock.file_lock(tmp_path / "missing" / "x.lock"):
            pass


@pytest.mark.skipif(os.name == "nt", reason="Windows 的 chmod 不构成写权限边界")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root 不受权限位限制")
def test_目录不可写(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o500)
    try:
        with pytest.raises(PermissionError):
            with platform_lock.file_lock(ro / "x.lock"):
                pass
    finally:
        os.chmod(ro, 0o700)


# ---------------------------------------------------------------- 共享锁

@pytest.mark.skipif(os.name != "posix", reason="POSIX flock 共享锁实跑；Windows 分支见 Windows 条件用例")
def test_共享锁可并发且排他锁被挡(tmp_path):
    lock = tmp_path / "shared.lock"
    fd1, fd2, fd3 = open_fd(lock), open_fd(lock), open_fd(lock)
    try:
        assert platform_lock.acquire(fd1, shared=True, blocking=False) is True
        assert platform_lock.acquire(fd2, shared=True, blocking=False) is True
        assert platform_lock.acquire(fd3, blocking=False) is False
        platform_lock.release(fd1)
        platform_lock.release(fd2)
        assert platform_lock.acquire(fd3, blocking=False) is True
    finally:
        platform_lock.release(fd3)
        for fd in (fd1, fd2, fd3):
            os.close(fd)


def test_共享锁不支持时降级为排他锁(monkeypatch):
    calls = []

    def fake_lock_once(fd, shared):
        calls.append(shared)
        if shared:
            raise platform_lock.LockUnsupported("shared unsupported")
        return True

    monkeypatch.setattr(platform_lock, "_lock_once", fake_lock_once)
    assert platform_lock._try_lock(0, shared=True) is True
    assert calls == [True, False]


def test_平台不支持时抛LockUnsupported(monkeypatch):
    monkeypatch.setattr(platform_lock, "_backend_name", lambda: None)
    with pytest.raises(platform_lock.LockUnsupported):
        platform_lock.acquire(0, blocking=False)


# ---------------------------------------------------------------- 跨进程

CHILD_HOLD_LOCK = """
import sys, time
from pathlib import Path
import platform_lock

lock, ready, go = sys.argv[1], sys.argv[2], sys.argv[3]
with platform_lock.file_lock(lock, timeout=30):
    Path(ready).write_text("1", encoding="utf-8")
    deadline = time.monotonic() + 30
    while not Path(go).exists():
        if time.monotonic() > deadline:
            raise SystemExit("wait go timeout")
        time.sleep(0.01)
"""


def _spawn_holder(tmp_path):
    lock = tmp_path / "proc.lock"
    ready = tmp_path / "ready"
    go = tmp_path / "go"
    log = tmp_path / "child.log"
    log_f = open(log, "w", encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", CHILD_HOLD_LOCK, str(lock), str(ready), str(go)],
        cwd=str(REPO_ROOT), env=env, stdout=log_f, stderr=subprocess.STDOUT,
    )
    return proc, lock, ready, go, log, log_f


def test_两进程争用(tmp_path):
    proc, lock, ready, go, log, log_f = _spawn_holder(tmp_path)
    try:
        deadline = time.monotonic() + 20
        while not ready.exists():
            if proc.poll() is not None:
                pytest.fail("子进程提前退出：\n" + _read_log(log))
            if time.monotonic() > deadline:
                pytest.fail("子进程 20s 内没拿到锁：\n" + _read_log(log))
            time.sleep(0.01)
        with pytest.raises(platform_lock.LockBusy):
            with platform_lock.file_lock(lock, blocking=False):
                pass
        go.write_text("1", encoding="utf-8")
        assert proc.wait(timeout=20) == 0, _read_log(log)
        with platform_lock.file_lock(lock, timeout=2):
            pass
    finally:
        if not go.exists():
            go.write_text("1", encoding="utf-8")
        if proc.poll() is None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        log_f.close()


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows 专用：T02 已在 Windows VM 验证 msvcrt.locking(LK_NBLCK) "
           "两进程争用与 8767 启动；POSIX 上跳过",
)
def test_windows_msvcrt_排他争用(tmp_path):  # pragma: no cover - 仅在 Windows 执行
    assert platform_lock._backend_name() == "windows"
    lock = tmp_path / "win.lock"
    fd1, fd2 = open_fd(lock), open_fd(lock)
    try:
        assert platform_lock.acquire(fd1, blocking=False) is True
        assert platform_lock.acquire(fd2, blocking=False) is False
        platform_lock.release(fd1)
        assert platform_lock.acquire(fd2, blocking=False) is True
        platform_lock.release(fd2)
    finally:
        os.close(fd1)
        os.close(fd2)
