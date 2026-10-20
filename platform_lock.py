# -*- coding: utf-8 -*-
"""跨平台文件锁（PLAN §8）：POSIX 用 ``fcntl.flock``，Windows 用 ``msvcrt.locking``。

**唯一允许 import fcntl 的模块**。其余模块一律经本模块加锁，移植时不要自己写平台分支。

对外两个层次：

- 上下文管理器：``file_lock(path_or_fd, *, exclusive=True, timeout=3.0, blocking=True)``。
  传路径时自动 ``O_CREAT`` 建锁文件（0600，不删除）；传 fd 时只加锁，不关闭调用方的 fd。
  拿不到锁抛 :class:`LockBusy`；平台/文件系统不支持抛 :class:`LockUnsupported`。
- 底层接口：``acquire(fd, *, shared=False, blocking=True, deadline=None, poll=0.05) -> bool``
  与 ``release(fd) -> None``，供需要自己管 fd 生命周期的调用方使用。

语义约定（与 CBL 现有 11 处 flock 调用一致）：

- 非阻塞只试一次，失败立即返回/抛错；阻塞用短轮询（默认 50ms）等到 ``timeout``。
  ``timeout=None`` 表示无限等；``timeout=0`` 等价于只试一次。
- POSIX 共享锁用 ``LOCK_SH``；Windows 共享锁用 ``LK_NBRLCK``。共享锁不可用时**降级为
  排他锁**（只影响并发读性能，不影响正确性）。
- 锁被占用与"平台不支持"必须分开：前者可重试，后者立即报 ``LockUnsupported``，
  绝不静默当成功继续写。
- 释放失败一律吞掉（与现有 ``contextlib.suppress(OSError)`` 口径一致），不掩盖业务异常。
"""
from __future__ import annotations

import contextlib
import errno
import os
import time

if os.name == "nt":  # pragma: no cover - Windows 分支在 POSIX CI 上不执行
    import msvcrt

    fcntl = None
else:  # pragma: no cover - 平台二选一
    msvcrt = None
    try:
        import fcntl
    except ImportError:  # 极少数 POSIX 变体没有 fcntl
        fcntl = None

__all__ = [
    "LockError",
    "LockBusy",
    "LockUnsupported",
    "file_lock",
    "acquire",
    "release",
]

DEFAULT_TIMEOUT = 3.0
DEFAULT_POLL = 0.05
# 锁 1 个字节就够：Windows 的 msvcrt.locking 必须指明字节数，POSIX flock 不看范围。
_LOCK_BYTES = 1


class LockError(RuntimeError):
    """文件锁相关错误的基类。"""


class LockBusy(LockError):
    """别的进程/文件描述符占着锁，且在超时时间内没有释放。"""


class LockUnsupported(LockError):
    """当前平台或文件系统不支持所需的锁语义。"""


def _errno_set(*names):
    return {getattr(errno, name) for name in names if hasattr(errno, name)}


# POSIX：flock 非阻塞失败通常是 EAGAIN/EWOULDBLOCK；部分系统报 EACCES。
_BUSY_ERRNOS = _errno_set("EACCES", "EAGAIN", "EWOULDBLOCK", "EDEADLK")
# 文件系统不支持 flock/msvcrt 锁时的典型 errno。
_UNSUPPORTED_ERRNOS = _errno_set("ENOLCK", "EOPNOTSUPP", "ENOTSUP", "EINVAL", "ENOSYS")
# Windows：ERROR_LOCK_VIOLATION / ERROR_NOT_LOCKED 视为忙；
# ERROR_INVALID_FUNCTION / ERROR_NOT_SUPPORTED / ERROR_CALL_NOT_IMPLEMENTED 视为不支持。
_BUSY_WINERRORS = {33, 158}
_UNSUPPORTED_WINERRORS = {1, 50, 120}


def _classify_oserror(exc: OSError):
    """把 OSError 归成 ``"busy"`` / ``"unsupported"`` / ``None``（未知，原样抛）。"""
    winerror = getattr(exc, "winerror", None)
    if winerror in _BUSY_WINERRORS:
        return "busy"
    if winerror in _UNSUPPORTED_WINERRORS:
        return "unsupported"
    if exc.errno in _BUSY_ERRNOS:
        return "busy"
    if exc.errno in _UNSUPPORTED_ERRNOS:
        return "unsupported"
    return None


def _backend_name():
    if os.name == "nt":
        return "windows"
    if os.name == "posix" and fcntl is not None:
        return "posix"
    return None


def _posix_lock_once(fd: int, shared: bool) -> bool:
    flags = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
    while True:
        try:
            fcntl.flock(fd, flags)
            return True
        except OSError as exc:
            if exc.errno == errno.EINTR:  # 信号打断不算忙，原样重试
                continue
            kind = _classify_oserror(exc)
            if kind == "busy":
                return False
            if kind == "unsupported":
                raise LockUnsupported(
                    "文件系统不支持 flock（errno=%r）" % (exc.errno,)
                ) from exc
            raise


def _windows_lock_once(fd: int, shared: bool) -> bool:  # pragma: no cover - 见 Windows 条件测试
    # msvcrt.locking 从当前文件位置锁 N 字节；每次先归零，避免上次操作把位置留在 1。
    os.lseek(fd, 0, os.SEEK_SET)
    mode = msvcrt.LK_NBRLCK if shared else msvcrt.LK_NBLCK
    try:
        msvcrt.locking(fd, mode, _LOCK_BYTES)
        return True
    except OSError as exc:
        kind = _classify_oserror(exc)
        if kind == "busy":
            return False
        if kind == "unsupported":
            raise LockUnsupported(
                "文件系统不支持 msvcrt.locking（errno=%r, winerror=%r）"
                % (exc.errno, getattr(exc, "winerror", None))
            ) from exc
        raise


def _lock_once(fd: int, shared: bool) -> bool:
    backend = _backend_name()
    if backend == "windows":
        return _windows_lock_once(fd, shared)
    if backend == "posix":
        return _posix_lock_once(fd, shared)
    raise LockUnsupported("当前平台不支持文件锁：os.name=%r" % (os.name,))


def _try_lock(fd: int, shared: bool) -> bool:
    """共享锁不支持时降级为排他锁；排他锁不支持则原样抛。"""
    try:
        return _lock_once(fd, shared)
    except LockUnsupported:
        if not shared:
            raise
    return _lock_once(fd, False)


def _check_fd(fd) -> int:
    if isinstance(fd, bool) or not isinstance(fd, int):
        raise TypeError("fd 必须是已打开文件的整数描述符")
    return fd


def acquire(fd: int, *, shared: bool = False, blocking: bool = True,
            deadline: float | None = None, poll: float = DEFAULT_POLL) -> bool:
    """尝试给 ``fd`` 加锁。

    返回 ``True`` 表示已加锁，``False`` 表示 ``blocking=False`` 时被占用。
    ``deadline`` 是 ``time.monotonic()`` 时间戳；``blocking=True`` 且为 ``None`` 时无限等。
    平台/文件系统不支持时抛 :class:`LockUnsupported`。
    """
    _check_fd(fd)
    if poll <= 0:
        raise ValueError("poll 必须大于 0")
    while True:
        if _try_lock(fd, shared):
            return True
        if not blocking:
            return False
        now = time.monotonic()
        if deadline is not None and now >= deadline:
            return False
        nap = poll
        if deadline is not None:
            nap = min(nap, max(0.0, deadline - now))
        time.sleep(nap)


def release(fd: int) -> None:
    """释放 ``fd`` 上的锁。锁已不在、fd 已关等错误一律吞掉。"""
    _check_fd(fd)
    if os.name == "nt":  # pragma: no cover - Windows 分支
        with contextlib.suppress(OSError):
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, _LOCK_BYTES)
        return
    if fcntl is not None:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)


def _open_lock_target(target):
    """返回 ``(fd, owns_fd)``。传 fd 时不接管生命周期。"""
    if isinstance(target, int) and not isinstance(target, bool):
        return target, False
    path = os.fspath(target)
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOINHERIT", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        if exc.errno in _UNSUPPORTED_ERRNOS:
            raise LockUnsupported(
                "无法在该路径创建锁文件（errno=%r）：%s" % (exc.errno, path)
            ) from exc
        raise
    return fd, True


def _label(target) -> str:
    if isinstance(target, int) and not isinstance(target, bool):
        return "fd=%d" % target
    try:
        return os.fsdecode(os.fspath(target))
    except TypeError:
        return repr(target)


@contextlib.contextmanager
def file_lock(path_or_fd, *, exclusive: bool = True, timeout: float | None = DEFAULT_TIMEOUT,
              blocking: bool = True, poll: float = DEFAULT_POLL):
    """给路径或已有 fd 加锁的上下文管理器。

    - ``exclusive=False`` 申请共享锁；平台不支持时自动降级排他。
    - ``blocking=False`` 不等待，被占用立即抛 :class:`LockBusy`。
    - ``blocking=True`` 最多等 ``timeout`` 秒；``timeout=None`` 无限等。
    - 传路径时创建锁文件（0600，不删除），退出时关闭自己打开的 fd；传 fd 时 fd 留给调用方。
    """
    if timeout is not None and timeout < 0:
        raise ValueError("timeout 不能为负数（None 表示无限等）")
    fd, owns_fd = _open_lock_target(path_or_fd)
    try:
        deadline = None
        if blocking and timeout is not None:
            deadline = time.monotonic() + timeout
        if not acquire(fd, shared=not exclusive, blocking=blocking,
                       deadline=deadline, poll=poll):
            waited = "不等待" if not blocking else (
                "等待 %ss 超时" % timeout if timeout is not None else "等待超时")
            raise LockBusy("文件锁被占用（%s）：%s" % (waited, _label(path_or_fd)))
        try:
            yield fd
        finally:
            release(fd)
    finally:
        if owns_fd:
            os.close(fd)
