# -*- coding: utf-8 -*-
"""生命周期操作的公共状态与后台任务（INTERFACE §2.4、§5、§8.6、§9）。

- 生命周期锁：``~/.claude/console-ops/.lifecycle.lock`` 上的非阻塞文件锁，锁文件内容为当前 op_id。
  跨平台锁走 ``platform_lock``（POSIX flock / Windows msvcrt），本模块不直接调用平台锁原语。
  进程退出时内核自动释放，壳崩溃不会把锁永远留住。
- 操作记录 ``~/.claude/console-ops/<op_id>.json``（0600，目录 0700）：每走一步原子写一次，落盘前整份打码；
  只记编号、步骤、结果、错误码与文字，令牌只在内存（``Task.token``）。终态只留最近 20 条，
  needs_attention 一直留。
- 步骤与提交点：``submit`` 照登记的顺序在后台线程里执行各 ``Step``。提交点（``Step.commit``）之前失败，
  逆序撤销已做的步骤，任务 failed（unchanged / rolled_back），后面的步骤保持 pending；之后的失败不撤销，
  只记这一步 failed、收尾照做，任务 needs_attention（``warn_only`` 的只进 warnings）。
- 结果未知：步骤抛 ``ResultUnknown``（写类调用用 ``write_call`` 包一层）或意外异常时，有 ``check`` 就复核：
  做成了按做成继续，没做成按失败处理，判定不出就停在原地 needs_attention，绝不报 rolled_back。
- 收拾：重启后收拾与“重新收拾”共用 ``_recover``。壳启动时 ``start_recovery`` 起线程，逐条以记录的 op_id
  拿锁、拿到才动。

对外形状按 INTERFACE §2.4：``state`` 为 running / done / failed / needs_attention。
"""
import concurrent.futures
import contextlib
import dataclasses
import json
import logging
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from typing import Callable, Optional

import platform_lock

from .base import AdapterUnavailable
from .guard import scrub

LOCK_NAME = ".lifecycle.lock"
BUSY_MESSAGE = "管理台正在执行另一项操作，稍后再试"
PERSON_BUSY_MESSAGE = "这只 bot 正在执行另一项操作，稍后再试"
OP_ID_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z-[a-z]{4,7}-[0-9a-f]{8}")
_KIND_RE = re.compile(r"[a-z]{4,7}")

RECOVERING_MESSAGE = "管理台正在收拾上次没做完的操作，稍后再试"

_persons_mu = threading.Lock()
_busy_persons = {}
_recovering = threading.Event()
_log = logging.getLogger(__name__)

# O_NOFOLLOW / O_CLOEXEC 在 Windows 上不存在；O_NOINHERIT 是 Windows 的对应物。
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOINHERIT", 0)


class LifecycleBusy(Exception):
    """生命周期锁被占（本进程另一个线程或别的进程），或本进程正在做重启后收拾 → 壳回 409 busy。"""
    code = "busy"

    def __init__(self, message: str = BUSY_MESSAGE):
        super().__init__(message)
        self.message = message


def ops_dir() -> str:
    """操作记录目录 ``~/.claude/console-ops``（每次按当前 HOME 现算）。"""
    return os.path.join(os.path.expanduser("~"), ".claude", "console-ops")


def lock_path() -> str:
    return os.path.join(ops_dir(), LOCK_NAME)


def new_op_id(kind: str) -> str:
    """生成 op_id：``<UTC 时间>-<种类>-<8 位随机十六进制>``。"""
    if not isinstance(kind, str) or _KIND_RE.fullmatch(kind) is None:
        raise ValueError("操作种类只许 4 到 7 个小写字母")
    return "%s-%s-%s" % (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), kind, secrets.token_hex(4))


def _read_all(fd: int, limit: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    chunks, total = [], 0
    while total <= limit:
        chunk = os.read(fd, min(65536, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _write_all(fd: int, data: bytes, *, offset: int = 0) -> None:
    os.lseek(fd, offset, os.SEEK_SET)
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _ensure_lock_byte(fd: int) -> None:
    """Windows 的 msvcrt.locking 从当前文件位置锁 1 个字节；空文件先写一个占位字节。

    布局：第 0 字节专供锁（内容无意义），op_id 从第 1 字节起。这样即使两个进程在首次创建时
    同时补第 0 字节，也碰不到另一个执行者写的 op_id。
    """
    if os.fstat(fd).st_size < 1:
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, b"\0")


def _read_op_id(fd: int) -> str:
    """读锁文件里的 op_id（第 1 字节起）；没有或读不出返回空串。"""
    try:
        if os.fstat(fd).st_size <= 1:
            return ""
        os.lseek(fd, 1, os.SEEK_SET)
        raw = os.read(fd, 64)
    except OSError:
        return ""
    return raw.decode("ascii", "replace").strip("\0").strip()


class LifecycleLock:
    """持有生命周期锁的凭据，由 ``acquire`` 给出；可跨线程交接，也可 ``with`` 着用。"""

    def __init__(self, fd: int, op_id: str, persons: tuple):
        self._fd = fd
        self._mu = threading.Lock()
        self.op_id = op_id
        self.persons = persons

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        """清掉本次登记的 person 并放锁；重复调用无害。锁文件不删。"""
        with self._mu:
            fd, self._fd = self._fd, None
        if fd is None:
            return
        with _persons_mu:
            for person in self.persons:
                if _busy_persons.get(person) == self.op_id:
                    del _busy_persons[person]
        try:
            platform_lock.release(fd)
        finally:
            os.close(fd)

    def add_person(self, person: str) -> None:
        if not isinstance(person, str) or not person:
            raise ValueError("person 必须是非空字符串")
        with self._mu:
            if self._fd is None:
                return
            with _persons_mu:
                if person not in self.persons:
                    self.persons = self.persons + (person,)
                _busy_persons[person] = self.op_id

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def acquire(op_id: str, *, persons=()) -> LifecycleLock:
    """非阻塞地拿生命周期锁，把 op_id 写进锁文件，并登记这次操作涉及的 person。"""
    if _recovering.is_set():
        raise LifecycleBusy(RECOVERING_MESSAGE)
    return _acquire(op_id, persons)


def _acquire(op_id: str, persons) -> LifecycleLock:
    if not isinstance(op_id, str) or OP_ID_RE.fullmatch(op_id) is None:
        raise ValueError("操作编号不合规")
    if isinstance(persons, (str, bytes)):
        raise ValueError("persons 要给名字的列表")
    persons = tuple(persons)
    if not all(isinstance(p, str) and p for p in persons):
        raise ValueError("person 必须是非空字符串")
    d = ops_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    path = os.path.join(d, LOCK_NAME)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | _O_NOFOLLOW | _O_CLOEXEC, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("生命周期锁文件不是普通文件")
        _ensure_lock_byte(fd)
        try:
            got = platform_lock.acquire(fd, blocking=False)
        except platform_lock.LockBusy:
            got = False
        if not got:
            raise LifecycleBusy()
        # 拿到锁之后才改内容：拿不到时绝不能动持锁者写的 op_id。
        # 第 0 字节留给锁，op_id 从第 1 字节起，见 _ensure_lock_byte。
        _write_all(fd, op_id.encode("ascii"), offset=1)
        os.ftruncate(fd, 1 + len(op_id))
    except BaseException:
        os.close(fd)   # 拿到锁之后才失败的，关掉也就放了锁
        raise
    lock = LifecycleLock(fd, op_id, persons)
    with _persons_mu:
        for person in persons:
            _busy_persons[person] = op_id
    return lock


ACTIVE_STATUSES = ("queued", "running")
MAX_RECORD_BYTES = 1024 * 1024


def _read_record(op_id: str):
    """读 ``<op_id>.json``；不在、是软链、不是普通文件、超长、不是 JSON 对象 → None。只读。"""
    try:
        fd = os.open(os.path.join(ops_dir(), op_id + ".json"), os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_RECORD_BYTES:
            return None
        raw = _read_all(fd, MAX_RECORD_BYTES)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(raw) > MAX_RECORD_BYTES:
        return None
    try:
        rec = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return rec if isinstance(rec, dict) else None


def _foreign_active_op():
    """别的打开者此刻持着生命周期锁、锁文件所指的记录未结束 → 返回该记录，否则 None。只读。"""
    try:
        fd = os.open(lock_path(), os.O_RDONLY | _O_NOFOLLOW | _O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        op_id = _read_op_id(fd)
        if OP_ID_RE.fullmatch(op_id) is None:
            return None
        rec = _read_record(op_id)
        if rec is None or rec.get("status") not in ACTIVE_STATUSES:
            return None
        try:
            got = platform_lock.acquire(fd, shared=True, blocking=False)
        except platform_lock.LockError:
            return None
        if got:
            platform_lock.release(fd)
            return None
        return rec
    except OSError:
        return None
    finally:
        os.close(fd)


def person_busy(person) -> bool:
    if not isinstance(person, str) or not person:
        return False
    with _persons_mu:
        if person in _busy_persons:
            return True
    rec = _foreign_active_op()
    return rec is not None and rec.get("person") == person


def any_person_busy() -> bool:
    with _persons_mu:
        if _busy_persons:
            return True
    return _foreign_active_op() is not None


# ── 任务记录、步骤、提交点、结果未知、重启后收拾、重新收拾 ──────────────────────────────────────────

TOKEN_LOST_MESSAGE = "管理台重启过，令牌没有保存，请重新提交"
NOT_FOUND_MESSAGE = "没有这项操作记录"
NOT_RESOLVABLE_MESSAGE = "只有需要处理的操作才能重新收拾"
BAD_OP_ID_MESSAGE = "操作编号不合规"
LIMIT_MESSAGE = "limit 要是 1 到 20 的整数"
INTERNAL_MESSAGE = "服务器内部错误，请稍后再试"
RESTARTED_MESSAGE = "管理台重启过，这项操作没做完，已撤回；需要的话请重新提交"
RESTART_UNKNOWN_MESSAGE = "管理台重启过，没法确认中断的那一步做没做成；点‘重新收拾’再收拾一次"
UNKNOWN_MESSAGE = "没法确认这一步做没做成（%s）；那边恢复后点‘重新收拾’再收拾一次"
UNEXPECTED_MESSAGE = "这一步出了意外，没法确认做没做成；点‘重新收拾’再收拾一次"
UNDO_FAILED_MESSAGE = "撤回没做完（%s）；点‘重新收拾’再试"
NO_RECIPE_MESSAGE = "管理台不认得这种操作的步骤，没法自动收拾；请手动处理，或更新管理台后点‘重新收拾’"
CHECKED_DONE = "复核过：已经做成"
CHECKED_NOT_DONE = "复核过：没有做成"
TIMEOUT_REASON = "等不到回应"

STATUSES = ("queued", "running", "succeeded", "failed", "needs_attention")
LISTED_ACTIVE = ("queued", "running", "needs_attention")
TERMINAL_PRUNED = ("succeeded", "failed")
KEEP_TERMINAL = 20
DEFAULT_LIMIT, MAX_LIMIT = 10, 20
RECOVERY_WAIT = 600.0
RECOVERY_POLL = 1.0
_PUBLIC_STATE = {"queued": "running", "running": "running", "succeeded": "done",
                 "failed": "failed", "needs_attention": "needs_attention"}
_INTERNAL = "_internal"
_UNKNOWN_ERRORS = (TimeoutError, ConnectionError, concurrent.futures.TimeoutError)


class StepSkipped(Exception):
    """步骤里抛：这一步不用做（记 skipped）。"""

    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message


class StepFailed(Exception):
    """步骤里抛：这一步没做成，它自己动过的已经撤回（或什么都没动）。"""

    def __init__(self, code: str, http_status: int, message: str, *, rolled_back: bool = False, note: str = None):
        super().__init__(message)
        self.code, self.http_status, self.message = code, http_status, message
        self.rolled_back, self.note = rolled_back is True, note


class ResultUnknown(Exception):
    """步骤里抛：写类调用超时或断连，不知道做没做成。"""

    def __init__(self, message: str, *, code: str = "step_failed", http_status: int = 500, reason: str = None,
                 rolled_back: bool = False):
        super().__init__(message)
        self.message, self.code, self.http_status = message, code, http_status
        self.reason = reason or message
        self.rolled_back = rolled_back is True


class StepStuck(Exception):
    """步骤里抛：底层明说停在了中间（LifecycleError needs_attention）。"""

    def __init__(self, message: str, *, committed: bool = False, code: str = "step_failed", http_status: int = 500):
        super().__init__(message)
        self.message, self.committed = message, committed is True
        self.code, self.http_status = code, http_status


class Kept:
    """``undo`` 的返回值：已到安全状态，但没能按原状态撤回。"""

    def __init__(self, note: str):
        self.note = note


class OpNotFound(Exception):
    """没有这项操作记录 → 404 op_not_found。"""
    code, http_status = "op_not_found", 404

    def __init__(self):
        super().__init__(NOT_FOUND_MESSAGE)
        self.message = NOT_FOUND_MESSAGE


class NotResolvable(Exception):
    """要重新收拾的不是 needs_attention 的任务 → 422 not_resolvable。"""
    code, http_status = "not_resolvable", 422

    def __init__(self):
        super().__init__(NOT_RESOLVABLE_MESSAGE)
        self.message = NOT_RESOLVABLE_MESSAGE


@dataclasses.dataclass(frozen=True)
class Step:
    key: str
    run: Callable
    undo: Optional[Callable] = None
    check: Optional[Callable] = None
    commit: bool = False
    warn_only: bool = False
    needs_token: bool = False


class _Recipe(tuple):
    finish = None
    settle = None


_kinds = {}


def register(kind: str, steps, *, finish: Optional[Callable] = None,
             settle: Optional[Callable] = None) -> None:
    """登记一种任务的步骤（编排层在导入时调；重复登记以后一次为准）。"""
    steps = tuple(steps)
    keys = [s.key for s in steps if isinstance(s, Step)]
    if (not isinstance(kind, str) or _KIND_RE.fullmatch(kind) is None or not steps or len(keys) != len(steps)
            or len(set(keys)) != len(keys) or sum(s.commit for s in steps) > 1
            or not (finish is None or callable(finish)) or not (settle is None or callable(settle))):
        raise ValueError("任务种类或步骤登记不合规")
    recipe = _Recipe(steps)
    recipe.finish = finish
    recipe.settle = settle
    _kinds[kind] = recipe


class Task:
    """一项后台任务的句柄。令牌只在内存，任务结束即丢，从不落盘。"""

    op_id = property(lambda self: self.rec["op_id"])
    kind = property(lambda self: self.rec.get("kind"))
    backend = property(lambda self: self.rec.get("backend"))
    bot_id = property(lambda self: self.rec.get("bot_id"))
    person = property(lambda self: self.rec.get("person"))
    archive_id = property(lambda self: self.rec.get("archive_id"))
    target = property(lambda self: self.rec.get("target"))
    target_backend = property(lambda self: (
        self.rec.get("target", {}).get("backend") if isinstance(self.rec.get("target"), dict)
        else self.rec.get("target")))

    def __init__(self, rec: dict, *, token=None, restarted: bool = False, rate_key=None, payload=None):
        self.rec, self.token, self.restarted, self.rate_key = rec, token, restarted, rate_key
        self.payload = payload
        self.lock = None

    def claim(self, *, bot_id=None, person=None) -> None:
        if bot_id is not None:
            self.rec["bot_id"] = bot_id
        if person is not None:
            self.rec["person"] = person
            if self.lock is not None:
                self.lock.add_person(person)

    @property
    def data(self) -> dict:
        return _inner(self.rec)["data"]

    @property
    def result(self) -> dict:
        if not isinstance(self.rec.get("result"), dict):
            self.rec["result"] = {}
        return self.rec["result"]

    def warn(self, message) -> None:
        text = scrub(message)
        warnings = self.rec.get("warnings")
        if not isinstance(warnings, list):
            warnings = self.rec["warnings"] = []
        if text and text not in warnings:
            warnings.append(text)

    def final_attention(self, message) -> None:
        text = scrub(message)
        if not text:
            return
        inner = _inner(self.rec)
        prev = inner.get("final_attention")
        inner["final_attention"] = prev + "；" + text if isinstance(prev, str) and prev and prev != text else text

    def save(self) -> None:
        _write(self.rec)


def _now() -> str:
    sec, ms = divmod(int(time.time() * 1000), 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(sec)) + ".%03dZ" % ms


def _error(code: str, http_status: int, message: str) -> dict:
    return {"code": code, "http_status": http_status, "message": message}


def _inner(rec: dict) -> dict:
    inner = rec.get(_INTERNAL)
    if not isinstance(inner, dict):
        inner = rec[_INTERNAL] = {}
    for key in ("committed", "rolling_back", "changed"):
        inner[key] = inner.get(key) is True
    for key in ("notes", "kept"):
        if not isinstance(inner.get(key), list):
            inner[key] = []
    if not isinstance(inner.get("data"), dict):
        inner["data"] = {}
    for key in ("doubt", "failure", "unknown"):
        inner.setdefault(key, None)
    return inner


def _scrubbed(value):
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {k: _scrubbed(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrubbed(v) for v in value]
    return value


def _ensure_ops_dir() -> str:
    d = ops_dir()
    os.makedirs(d, mode=0o700, exist_ok=True)
    st = os.lstat(d)
    if not stat.S_ISDIR(st.st_mode):
        raise OSError("操作记录目录不是普通目录")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(d, 0o700)
    return d


def _write(rec: dict) -> None:
    """原子写 ``<op_id>.json``：临时文件 + fsync + 改名 + 目录 fsync。"""
    rec["updated_at"] = _now()
    raw = json.dumps(_scrubbed(rec), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    d = _ensure_ops_dir()
    fd, tmp = tempfile.mkstemp(prefix="." + rec["op_id"] + ".", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, os.path.join(d, rec["op_id"] + ".json"))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def _load(op_id):
    if not isinstance(op_id, str) or OP_ID_RE.fullmatch(op_id) is None:
        return None
    rec = _read_record(op_id)
    if rec is None or rec.get("op_id") != op_id or rec.get("status") not in STATUSES:
        return None
    return rec


def _all_records() -> list:
    try:
        names = os.listdir(ops_dir())
    except OSError:
        return []
    recs = (_load(n[:-len(".json")]) for n in names if n.endswith(".json"))
    return [r for r in recs if r is not None]


def _order_key(rec: dict):
    created = rec.get("created_at")
    return (created if isinstance(created, str) else "", rec["op_id"])


def public(rec: dict) -> dict:
    """对外的任务形状（INTERFACE §2.4）：内部 status 映射成 state。"""
    status = rec.get("status")
    error = rec.get("error") if isinstance(rec.get("error"), dict) else {}
    state = _PUBLIC_STATE.get(status, "failed")
    code = "needs_attention" if status == "needs_attention" else (
        error.get("code") if status == "failed" else None)
    steps = rec.get("steps") if isinstance(rec.get("steps"), list) else []
    result = rec.get("result") if isinstance(rec.get("result"), dict) else None
    out = {"op_id": rec.get("op_id"), "kind": rec.get("kind"), "backend": rec.get("backend"),
           "bot_id": rec.get("bot_id"), "person": rec.get("person"),
           "archive_id": rec.get("archive_id"), "target": rec.get("target"),
           "state": state, "code": code,
           "message": rec.get("message") if isinstance(rec.get("message"), str) else "",
           "result": result, "steps": steps,
           "needs_attention": status == "needs_attention",
           "created_at": rec.get("created_at"), "updated_at": rec.get("updated_at")}
    # 记录落盘时已整份打码；对外再打一次是纵深防御（例如测试或旧记录直接构造）。
    return _scrubbed(out)


def get_op(op_id):
    """单条任务（对外形状）；没有 → None。"""
    rec = _load(op_id)
    return None if rec is None else public(rec)


def list_ops(*, active: bool = False, limit: int = DEFAULT_LIMIT) -> list:
    """按 created_at 倒序的任务（对外形状）；``active`` 只列 queued、running、needs_attention。"""
    recs = [r for r in _all_records() if not active or r["status"] in LISTED_ACTIVE]
    recs.sort(key=_order_key, reverse=True)
    return [public(r) for r in recs[:max(0, limit)]]


def _prune(keep: str) -> None:
    done = sorted((r for r in _all_records() if r["status"] in TERMINAL_PRUNED and r["op_id"] != keep),
                  key=_order_key, reverse=True)
    for rec in done[KEEP_TERMINAL - 1:]:
        try:
            os.unlink(os.path.join(ops_dir(), rec["op_id"] + ".json"))
        except FileNotFoundError:
            pass
        except OSError as e:
            _log.warning("删旧操作记录 %s 失败：%s", rec["op_id"], type(e).__name__)


def _step_of(rec: dict, key: str):
    for s in rec.get("steps") or ():
        if isinstance(s, dict) and s.get("step") == key:
            return s
    return None


def _status_of(rec: dict, key: str) -> str:
    s = _step_of(rec, key)
    return s.get("status", "pending") if s else "pending"


def _set_step(rec: dict, key: str, status: str, message=None) -> None:
    s = _step_of(rec, key)
    if s is None:
        s = {"step": key, "status": "pending", "message": "", "at": _now()}
        rec.setdefault("steps", []).append(s)
    s["status"], s["at"] = status, _now()
    if message is not None:
        s["message"] = message


def _close_running(rec: dict) -> None:
    for s in rec.get("steps") or ():
        if isinstance(s, dict) and s.get("status") == "running":
            s["status"], s["at"] = "failed", _now()


def _succeed(task: Task) -> None:
    rec = task.rec
    _inner(rec)["doubt"] = None
    result = rec.get("result") if isinstance(rec.get("result"), dict) and rec.get("result") else None
    msg = (result or {}).get("message")
    rec.update(status="succeeded", state=None, error=None, result=result, message=msg if isinstance(msg, str) else "")
    _prune(rec["op_id"])
    _write(rec)
    _finish(task)


def _fail(task: Task) -> None:
    rec, inner = task.rec, _inner(task.rec)
    err = inner["failure"] if isinstance(inner["failure"], dict) else _error("step_failed", 500, RESTARTED_MESSAGE)
    _close_running(rec)
    rec.update(status="failed", state="rolled_back" if inner["changed"] else "unchanged", error=dict(err),
               result=None, message=str(err.get("message") or "") + "".join("；" + str(n) for n in inner["notes"]))
    _prune(rec["op_id"])
    _write(rec)
    _finish(task)


def _finish(task: Task) -> None:
    fn = getattr(_kinds.get(task.kind), "finish", None)
    if fn is None:
        return
    try:
        fn(task)
    except Exception as e:
        _log.warning("操作 %s 收尾清理出错：%s", task.op_id, type(e).__name__)


def _attention(task: Task, message: str) -> None:
    rec = task.rec
    _close_running(rec)
    rec.update(status="needs_attention", state="needs_attention", message=message,
               error=_error("step_failed", 500, message), result=rec.get("result") or None)
    _write(rec)


def complete(task: Task) -> None:
    _inner(task.rec)["final_attention"] = None
    _succeed(task)


def attention(task: Task, message: str) -> None:
    _attention(task, message)


def failed(task: Task, message: str, *, code: str = "step_failed", http_status: int = 500) -> None:
    inner = _inner(task.rec)
    inner["final_attention"] = None
    inner["failure"] = _error(code, http_status, message)
    _fail(task)


def done_step(task: Task, key: str, message=None) -> None:
    _set_step(task.rec, key, "done", message)


def _failure_of(exc):
    code, status = getattr(exc, "code", None), getattr(exc, "http_status", None)
    if not isinstance(code, str) or not code or not isinstance(status, int) or isinstance(status, bool):
        return None
    message = getattr(exc, "message", None)
    return _error(code, status, scrub(message if isinstance(message, str) else str(exc)))


def _check(task: Task, d: Step):
    try:
        verdict = d.check(task)
    except Exception as e:
        _log.error("操作 %s 复核 %s 出错：%s", task.op_id, d.key, type(e).__name__)
        return None
    return verdict if isinstance(verdict, bool) else None


def _attempt(task: Task, d: Step):
    try:
        out = d.run(task)
        return "done", out if isinstance(out, str) else None
    except StepSkipped as e:
        return "skipped", e.message
    except StepFailed as e:
        return "failed", dict(_error(e.code, e.http_status, e.message), rolled_back=e.rolled_back, note=e.note)
    except StepStuck as e:
        return "stuck", e
    except ResultUnknown as e:
        doubt = {"message": e.message, "not_done": dict(_error(e.code, e.http_status, e.reason), rolled_back=e.rolled_back)}
    except Exception as e:
        failure = _failure_of(e)
        if failure is not None:
            return "failed", failure
        _log.error("操作 %s 的 %s 步出错：%s", task.op_id, d.key, type(e).__name__)
        doubt = {"message": UNEXPECTED_MESSAGE, "not_done": _error("internal", 500, INTERNAL_MESSAGE)}
    if d.check is not None:
        verdict = _check(task, d)
        if verdict is True:
            return "done", CHECKED_DONE
        if verdict is False:
            return "failed", doubt["not_done"]
    return "unknown", doubt


def _forward(task: Task, steps) -> None:
    rec, inner = task.rec, _inner(task.rec)
    attention = None
    for d in steps:
        if _status_of(rec, d.key) in ("done", "skipped"):
            continue
        if d.needs_token and task.token is None:
            if not inner["committed"]:
                inner["failure"] = _error("step_failed", 500, TOKEN_LOST_MESSAGE)
                return _rollback(task, steps)
            _set_step(rec, d.key, "failed", TOKEN_LOST_MESSAGE)
            attention = attention or TOKEN_LOST_MESSAGE
            continue
        _set_step(rec, d.key, "running", "")
        inner["doubt"], inner["unknown"] = d.key, None
        _write(rec)
        outcome, value = _attempt(task, d)
        if outcome == "unknown":
            inner["unknown"] = value
            _set_step(rec, d.key, "failed", value["message"])
            return _attention(task, value["message"])
        if outcome == "stuck" and not value.committed:
            inner["unknown"] = {"message": value.message,
                                "not_done": dict(_error(value.code, value.http_status, value.message), rolled_back=True)}
            _set_step(rec, d.key, "failed", value.message)
            return _attention(task, value.message)
        inner["doubt"] = None
        if outcome == "stuck":
            inner["committed"] = True
            _set_step(rec, d.key, "failed", value.message)
            attention = attention or value.message
            _write(rec)
            continue
        if outcome == "done":
            _set_step(rec, d.key, "done", value)
            inner["committed"] = inner["committed"] or d.commit
        elif outcome == "skipped":
            _set_step(rec, d.key, "skipped", value)
        else:
            _set_step(rec, d.key, "failed", value.get("note") or value["message"])
            if not inner["committed"]:
                inner["failure"] = _error(value["code"], value["http_status"], value["message"])
                inner["changed"] = inner["changed"] or value.get("rolled_back") is True
                if value.get("note"):
                    inner["notes"].append(value["note"])
                return _rollback(task, steps)
            if d.warn_only:
                task.warn(value["message"])
            else:
                attention = attention or value["message"]
        _write(rec)
    if attention:
        return _attention(task, attention)
    final = _inner(rec).get("final_attention")
    if isinstance(final, str) and final:
        return _attention(task, final)
    return _succeed(task)


def _rollback(task: Task, steps) -> None:
    rec, inner = task.rec, _inner(task.rec)
    inner["rolling_back"] = True
    _write(rec)
    for d in reversed(steps):
        status = _status_of(rec, d.key)
        if d.key in inner["kept"] or not (status in ("done", "running") or inner["doubt"] == d.key):
            continue
        if d.undo is None:
            if status == "running":
                _set_step(rec, d.key, "failed")
            if inner["doubt"] == d.key:
                inner["doubt"] = None
            continue
        try:
            out = d.undo(task)
        except Exception as e:
            failure = _failure_of(e)
            if failure is None:
                _log.error("操作 %s 撤回 %s 出错：%s", task.op_id, d.key, type(e).__name__)
            message = UNDO_FAILED_MESSAGE % (failure["message"] if failure else "出了意外")
            _set_step(rec, d.key, status, message)
            return _attention(task, message)
        if isinstance(out, Kept):
            _set_step(rec, d.key, "done", out.note)
            inner["kept"].append(d.key)
            inner["notes"].append(out.note)
        else:
            _set_step(rec, d.key, "undone", out if isinstance(out, str) and out else None)
        inner["changed"] = True
        if inner["doubt"] == d.key:
            inner["doubt"] = None
        _write(rec)
    final = inner.get("final_attention")
    if isinstance(final, str) and final:
        return _attention(task, final)
    return _fail(task)


def _not_done(inner: dict) -> dict:
    unknown = inner["unknown"]
    if isinstance(unknown, dict) and isinstance(unknown.get("not_done"), dict):
        nd = unknown["not_done"]
        if nd.get("rolled_back") is True:
            inner["changed"] = True
        return _error(nd.get("code") or "step_failed", nd.get("http_status") or 500, nd.get("message") or "")
    return _error("step_failed", 500, RESTARTED_MESSAGE)


def _token_gone(task: Task, steps) -> bool:
    return task.token is None and any(d.needs_token and _status_of(task.rec, d.key) not in ("done", "skipped")
                                      for d in steps)


def _recover(task: Task) -> None:
    """重启后收拾与“重新收拾”共用；调用方已以这条记录的 op_id 拿着锁。"""
    rec = task.rec
    steps = _kinds.get(rec.get("kind"))
    if steps is None:
        return _attention(task, NO_RECIPE_MESSAGE)
    settle = getattr(steps, "settle", None)
    if settle is not None:
        rec.update(status="running", message="")
        _write(rec)
        return settle(task)
    inner = _inner(rec)
    rec.update(status="running", message="")
    _write(rec)
    d = next((s for s in steps if s.key == inner["doubt"]), None)
    if d is not None and d.check is not None:
        verdict = _check(task, d)
        if verdict is None:
            unknown = inner["unknown"] if isinstance(inner["unknown"], dict) else {}
            return _attention(task, unknown.get("message") or RESTART_UNKNOWN_MESSAGE)
        inner["doubt"] = None
        if verdict:
            _set_step(rec, d.key, "done", CHECKED_DONE)
            inner["committed"] = inner["committed"] or d.commit
        else:
            _set_step(rec, d.key, "failed", CHECKED_NOT_DONE)
            if not inner["committed"]:
                inner["rolling_back"] = True
                inner["failure"] = inner["failure"] or _not_done(inner)
        _write(rec)
    if inner["committed"]:
        return _forward(task, steps)
    token_gone = _token_gone(task, steps)
    if inner["rolling_back"] or task.restarted or inner["doubt"] is not None or token_gone:
        if not inner["failure"]:
            inner["failure"] = _error("step_failed", 500, TOKEN_LOST_MESSAGE) if token_gone else _not_done(inner)
        return _rollback(task, steps)
    return _forward(task, steps)


def _persons_of(rec: dict) -> tuple:
    person = rec.get("person")
    return (person,) if isinstance(person, str) and person else ()


def _in_background(task: Task, lock: LifecycleLock, body) -> None:
    task.lock = lock

    def main():
        try:
            body()
        except Exception as e:
            _log.error("操作 %s 中途出错：%s", task.op_id, type(e).__name__)
            with contextlib.suppress(Exception):
                _attention(task, UNEXPECTED_MESSAGE)
        finally:
            task.token = None
            task.payload = None
            lock.release()

    threading.Thread(target=main, name="console-op-" + task.op_id, daemon=True).start()


def _first_run(task: Task, steps) -> None:
    task.rec["status"] = "running"
    _write(task.rec)
    _forward(task, steps)


def submit(lock: LifecycleLock, kind: str, *, backend: str, bot_id=None, person=None, archive_id=None,
           target=None, token=None, rate_key=None, data=None, payload=None) -> str:
    """受理检查全部通过、已拿到锁之后调：写任务记录（queued）、起后台线程，返回 op_id。"""
    try:
        steps = _kinds.get(kind)
        if steps is None:
            raise ValueError("没有登记这种任务")
        if not isinstance(lock, LifecycleLock) or not lock.held or lock.op_id.split("-")[1] != kind:
            raise ValueError("锁与任务种类对不上")
        rec = _new_record(lock.op_id, kind, backend, bot_id, person, archive_id, target, [d.key for d in steps])
        if isinstance(data, dict):
            _inner(rec)["data"].update(data)
        _write(rec)
        task = Task(rec, token=token, rate_key=rate_key, payload=payload)
        try:
            _in_background(task, lock, lambda: _first_run(task, steps))
        except BaseException:
            _inner(rec)["failure"] = _error("internal", 500, INTERNAL_MESSAGE)
            with contextlib.suppress(Exception):
                _fail(task)
            raise
    except BaseException:
        lock.release()
        raise
    return rec["op_id"]


def _new_record(op_id, kind, backend, bot_id, person, archive_id, target, keys) -> dict:
    now = _now()
    rec = {"op_id": op_id, "kind": kind, "backend": backend, "bot_id": bot_id, "person": person,
           "archive_id": archive_id, "target": target, "status": "queued", "state": None,
           "steps": [{"step": k, "status": "pending", "message": "", "at": now} for k in keys],
           "result": None, "warnings": [], "error": None, "message": "", "created_at": now, "updated_at": now}
    _inner(rec)
    return rec


def resolve(op_id: str) -> str:
    """重新收拾：存在 → needs_attention → 拿锁 → 后台按同一规则再收拾一次。"""
    rec = _load(op_id)
    if rec is None:
        raise OpNotFound()
    if rec["status"] != "needs_attention":
        raise NotResolvable()
    lock = acquire(op_id, persons=_persons_of(rec))
    task = None
    try:
        rec = _load(op_id)
        if rec is None:
            raise OpNotFound()
        if rec["status"] != "needs_attention":
            raise NotResolvable()
        previous = rec.get("message") or UNEXPECTED_MESSAGE
        task = Task(rec)
        rec.update(status="running", message="")
        _write(rec)
        _in_background(task, lock, lambda: _recover(task))
    except BaseException:
        if task is not None and rec.get("status") == "running":
            with contextlib.suppress(Exception):
                _attention(task, previous)
        lock.release()
        raise
    return op_id


def recover_orphans(*, wait: float = RECOVERY_WAIT, poll: float = RECOVERY_POLL) -> list:
    """收拾上次没做完的任务，返回收拾过的 op_id。"""
    deadline = time.monotonic() + max(0.0, wait)
    handled = []
    while True:
        pending = sorted((r for r in _all_records() if r["status"] in ACTIVE_STATUSES and r["op_id"] not in handled),
                         key=_order_key)
        if not pending:
            return handled
        for rec in pending:
            try:
                lock = _acquire(rec["op_id"], _persons_of(rec))
            except LifecycleBusy:
                continue
            try:
                fresh = _load(rec["op_id"])
                if fresh is not None and fresh["status"] in ACTIVE_STATUSES:
                    task = Task(fresh, restarted=True)
                    task.lock = lock
                    try:
                        _recover(task)
                    except Exception as e:
                        _log.error("重启后收拾 %s 出错：%s", task.op_id, type(e).__name__)
                        with contextlib.suppress(Exception):
                            _attention(task, UNEXPECTED_MESSAGE)
                    handled.append(task.op_id)
            finally:
                lock.release()
        if time.monotonic() >= deadline:
            _log.warning("还有 %d 条没结束的操作没能收拾：生命周期锁一直被别的进程占着", len(pending))
            return handled
        time.sleep(poll)


def start_recovery(*, wait: float = RECOVERY_WAIT, poll: float = RECOVERY_POLL):
    """壳启动时调一次：有 queued、running 的记录才置“收拾中”并起后台线程。"""
    if not any(r["status"] in ACTIVE_STATUSES for r in _all_records()):
        return None
    _recovering.set()

    def main():
        try:
            recover_orphans(wait=wait, poll=poll)
        except Exception as e:
            _log.error("重启后收拾出错：%s", type(e).__name__)
        finally:
            _recovering.clear()

    worker = threading.Thread(target=main, name="console-ops-recovery", daemon=True)
    try:
        worker.start()
    except BaseException:
        _recovering.clear()
        raise
    return worker


def recovering() -> bool:
    return _recovering.is_set()


def write_call(fn):
    """包住一次写类适配器调用：超时或连接中断改抛 ``ResultUnknown``，由引擎按 check 复核。"""
    try:
        return fn()
    except AdapterUnavailable as e:
        if e.status != "down":
            raise
        reason, code, status = e.message, "backend_down", 502
    except _UNKNOWN_ERRORS:
        reason, code, status = TIMEOUT_REASON, "step_failed", 500
    raise ResultUnknown(UNKNOWN_MESSAGE % reason, code=code, http_status=status, reason=reason)
