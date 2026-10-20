"""线下聚会模式的状态文件（INTERFACE-party-camera 2.1、PLAN CBL-1）。

一只群一个文件：``<DISPATCHER_PARTY_DIR || DP/.party-mode>/<群号>.json``，存在且 ``on=true`` 即开启。
本模块只做文件 IO 与形状校验，不依赖 Flask、旧栈或任何适配器；director 与 8767 管理台共用同一份读写。

约定：
- 目录 0700、文件 0600，同目录临时文件 + 原子改名；读取上限 4 KiB。
- 坏文件、形状不对、``opened_at_ms`` 比 now 超前超过 60 秒 → 按关闭处理并尽力删除。
- ``opened_at_ms`` 满 6 小时 → 过期关闭，懒删除（没有后台定时器）。
- 不加跨进程锁，last-writer-wins。
- 测试时钟接缝 ``DISPATCHER_TEST_NOW_MS``（仅测试态生效，INTERFACE 3.1）；生产一律走真实时钟。
"""
import contextlib
import json
import logging
import os
import re
import tempfile
import time

V = 1
EXPIRE_MS = 21_600_000        # 6 小时
CLOCK_SKEW_MS = 60_000        # 允许比 now 超前 60 秒
MAX_READ = 4 * 1024           # 4 KiB
NOTE_MAX = 80
EXCLUDE_MAX = 20
SOURCES = ("bot", "director", "console")
# 群号：无 -0、无前导零、最多 20 位数字（INTERFACE 2.1）
CHAT_RE = re.compile(r"^-[1-9][0-9]{0,19}$")
# 不参加的 bot id：与 dispatcher 的 BOT_NAME 同一命名空间（INTERFACE 2.2）
BOT_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

_log = logging.getLogger(__name__)


class StateUnreadable(Exception):
    """状态文件读不出/形状不对/时钟回拨；``reason`` 只用于日志（state_unreadable/state_clock_skew）。"""

    def __init__(self, reason="state_unreadable"):
        super().__init__(reason)
        self.reason = reason


class StateWriteError(Exception):
    """状态文件写不进去（目录不可写、原子改名失败等）。调用方回固定文案，状态不变。"""


def state_dir() -> str:
    """状态根：``DISPATCHER_PARTY_DIR``（必须是绝对路径）否则 ``~/.claude/dispatcher/.party-mode``。"""
    override = os.environ.get("DISPATCHER_PARTY_DIR", "")
    if override and os.path.isabs(override):
        return override
    return os.path.join(os.path.expanduser("~"), ".claude", "dispatcher", ".party-mode")


def path(chat_id: str) -> str:
    return os.path.join(state_dir(), "%s.json" % chat_id)


def valid_chat_id(value):
    """群号规范化：只收字符串且匹配 ``^-[1-9]\\d{0,19}$``；其他返回 None。"""
    return value if isinstance(value, str) and CHAT_RE.fullmatch(value) is not None else None


def normalize_exclude(value):
    """``exclude`` 校验：字符串列表、每项是 bot id、按原顺序去重。不合规返回 None。
    长度先按**原始列表**判（>20 即不合法，21 个重复项也一样），再去重（INTERFACE 2.1）。"""
    if not isinstance(value, list):
        return None
    if len(value) > EXCLUDE_MAX:      # 原始条数，不是去重后的条数
        return None
    out = []
    for item in value:
        if not isinstance(item, str) or BOT_ID_RE.fullmatch(item) is None:
            return None
        if item not in out:
            out.append(item)
    return out


def normalize_note(value):
    """``note`` 校验：字符串、最多 80 个字符、不含换行。不合规返回 None。"""
    if not isinstance(value, str) or len(value) > NOTE_MAX:
        return None
    if "\n" in value or "\r" in value:
        return None
    return value


def _fixed_now_ms():
    """测试态固定时钟。CBL 侧的测试开关是 CLAUDEBOTLIFE_TEST；同时认 dispatcher 的 CLAUDEBOT_TEST。"""
    if os.environ.get("CLAUDEBOTLIFE_TEST") != "1" and os.environ.get("CLAUDEBOT_TEST") != "1":
        return None
    try:
        value = int(os.environ.get("DISPATCHER_TEST_NOW_MS", ""))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def now_ms() -> int:
    """当前 epoch 毫秒；测试态按 ``DISPATCHER_TEST_NOW_MS`` 固定。"""
    fixed = _fixed_now_ms()
    return fixed if fixed is not None else int(time.time() * 1000)


def _drop(chat_id: str) -> None:
    """尽力删除；删不掉只影响下次读取，不能把当前请求打挂。"""
    with contextlib.suppress(OSError):
        os.unlink(path(chat_id))


def _unreadable(chat_id: str, reason="state_unreadable") -> StateUnreadable:
    """记一行只含群号与原因码的日志，返回给调用方抛出（不记文件内容）。"""
    if reason == "state_clock_skew":
        _log.warning("state_clock_skew chat=%s", chat_id)
    else:
        _log.warning("party_state_unreadable chat=%s", chat_id)
    return StateUnreadable(reason)


def read(chat_id: str, now_ms_value=None):
    """读群状态：不存在/已过期 → None；坏文件/时钟回拨 → StateUnreadable（文件已尽力删除）。
    ``now_ms_value`` 显式给定时钟（导演写状态时用 tick 的 now）；不给走 :func:`now_ms`。"""
    chat = valid_chat_id(chat_id)
    if chat is None:
        raise ValueError("bad chat_id")
    fixed = now_ms_value if now_ms_value is not None else now_ms()
    target = path(chat)
    try:
        size = os.stat(target).st_size
    except FileNotFoundError:
        return None
    except OSError:
        raise _unreadable(chat)
    if size > MAX_READ:
        _drop(chat)
        raise _unreadable(chat)
    try:
        with open(target, "rb") as f:
            raw = f.read(MAX_READ + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raise _unreadable(chat)
    if len(raw) > MAX_READ:
        _drop(chat)
        raise _unreadable(chat)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        _drop(chat)
        raise _unreadable(chat)
    if not isinstance(data, dict):
        _drop(chat)
        raise _unreadable(chat)
    if data.get("v") != V or data.get("chat_id") != chat or data.get("on") is not True:
        _drop(chat)
        raise _unreadable(chat)
    source = data.get("source")
    opened = data.get("opened_at_ms")
    exclude = normalize_exclude(data.get("exclude", []))
    note = normalize_note(data.get("note", ""))
    if (source not in SOURCES or isinstance(opened, bool) or not isinstance(opened, int)
            or opened <= 0 or exclude is None or note is None):
        _drop(chat)
        raise _unreadable(chat)
    if opened > fixed + CLOCK_SKEW_MS:
        _drop(chat)
        raise _unreadable(chat, "state_clock_skew")
    if fixed - opened >= EXPIRE_MS:
        _drop(chat)
        return None
    return {"v": V, "chat_id": chat, "on": True, "source": source,
            "opened_at_ms": opened, "exclude": exclude, "note": note}


def is_active(chat_id: str, now_ms_value=None) -> bool:
    """这个群现在有没有有效的聚会状态；坏文件、过期、回拨都按关闭。"""
    try:
        return read(chat_id, now_ms_value=now_ms_value) is not None
    except StateUnreadable:
        return False
    except ValueError:
        return False


def public_state(chat_id: str, now_ms_value=None) -> dict:
    """管理台 GET 用的对外形状（INTERFACE 2.6）：开启 5 个键；关闭 ``{"on": false}``；
    读不出 ``{"on": false, "error": "state_unreadable"}``（回拨同样按读不出）。"""
    try:
        st = read(chat_id, now_ms_value=now_ms_value)
    except StateUnreadable:
        return {"on": False, "error": "state_unreadable"}
    except ValueError:
        return {"on": False, "error": "state_unreadable"}
    if st is None:
        return {"on": False}
    return {"on": True, "source": st["source"], "opened_at_ms": st["opened_at_ms"],
            "exclude": list(st["exclude"]), "note": st["note"]}


def _atomic_write(chat_id: str, obj: dict) -> None:
    directory = state_dir()
    tmp = None
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".party-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path(chat_id))
        tmp = None
    except OSError:
        raise StateWriteError("state_write_failed")
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def open_party(chat_id: str, source: str, exclude=None, note=None, now_ms_value=None) -> dict:
    """开启（或重复开启）聚会状态。重复开启保留首次的 ``opened_at_ms``/``source``；
    ``exclude``/``note`` 给了就按本次请求更新，没给（None）就保留原值。写失败抛 StateWriteError。"""
    chat = valid_chat_id(chat_id)
    if chat is None or source not in SOURCES:
        raise ValueError("bad party state")
    if exclude is not None:
        exclude = normalize_exclude(exclude)
        if exclude is None:
            raise ValueError("bad exclude")
    if note is not None:
        note = normalize_note(note)
        if note is None:
            raise ValueError("bad note")
    now = now_ms_value if now_ms_value is not None else now_ms()
    try:
        current = read(chat, now_ms_value=now)
    except StateUnreadable:
        current = None          # 坏文件/回拨按关闭：直接覆盖
    if current is not None:
        opened = current["opened_at_ms"]
        keep_source = current["source"]
        exclude = current["exclude"] if exclude is None else exclude
        note = current["note"] if note is None else note
    else:
        opened = now
        keep_source = source
        exclude = [] if exclude is None else exclude
        note = "" if note is None else note
    state = {"v": V, "chat_id": chat, "on": True, "source": keep_source,
             "opened_at_ms": opened, "exclude": list(exclude), "note": note}
    _atomic_write(chat, state)
    return state


def close_party(chat_id: str) -> bool:
    """关闭：删文件。文件本来就不存在也返回 True（幂等）；删失败抛 StateWriteError。"""
    chat = valid_chat_id(chat_id)
    if chat is None:
        raise ValueError("bad chat_id")
    try:
        os.unlink(path(chat))
        return True
    except FileNotFoundError:
        return True
    except OSError:
        raise StateWriteError("state_write_failed")
