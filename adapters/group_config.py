"""群聊的群清单 ``configs/_group_chat.yml``（约定 v0.7 §5.12.5）：壳、导演、迁移脚本共用。

只用标准库与 PyYAML，不碰旧栈；配置目录与 config_loader 同一接缝 ``CLAUDEBOT_CONFIG_DIR``。
文件不存在 = 没有群配置（导演退回旧名单兜底）。形状：

    groups:
      - chat_id: "-1001234567890"
        allowFrom: ["123456789"]
        requireMention: false
"""
import contextlib
import json
import os
import tempfile
import time

import yaml

import platform_lock

from .base import (GROUP_ALLOW_FROM_MESSAGE, GROUP_CHAT_ID_MESSAGE, GROUP_ITEM_MAX, GROUP_LIST_MAX,
                   valid_chat_id, valid_group_chat_id)

FILE_NAME = "_group_chat.yml"
LOCK_SUFFIX = ".lock"
LOCK_WAIT_SEC = 3.0     # INTERFACE-bot-features 3.10 第 2 条：等 3 秒
LOCK_RETRY_SEC = 0.05
BUSY_MESSAGE = "别的操作正在改群清单，请稍后再试"
LOCK_FAILED_PREFIX = "群设置锁不可用："
UNREADABLE_MESSAGE = "读不出群清单"
NOT_WRITTEN_MESSAGE = "群清单没有写成（原文件不变）"
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HEADER = ("# 群聊的群清单（约定 v0.7 §5.12.5）：有哪些群，以及每个群的默认 allowFrom、requireMention。\n"
           "# 成员身份不在这里：一只 bot 在不在群，看它 access.json 的 groups 里有没有这个群号。\n")


class GroupConfigError(Exception):
    """群清单读不出或形状不对；message 给人看，不含路径。``code`` 给路由层映射：
    ``invalid``（群号/allowFrom 不合规）、``busy``（拿不到锁）、``unreadable``（读不出）、
    ``write``（写失败）；其余情形为 ``internal``。"""

    def __init__(self, message: str, code: str = "internal"):
        super().__init__(message)
        self.code, self.message = code, message


def path() -> str:
    # CLAUDEBOT_GROUP_CHAT_FILE 只管群清单这一个文件（测试用它指到不存在的路径，让导演走旧名单兜底，
    # 又不连带改掉 config_loader 也在用的 CLAUDEBOT_CONFIG_DIR）
    override = os.environ.get("CLAUDEBOT_GROUP_CHAT_FILE")
    if override:
        return override
    return os.path.join(os.environ.get("CLAUDEBOT_CONFIG_DIR") or os.path.join(_REPO, "configs"), FILE_NAME)


def _str_list(value) -> bool:
    return (isinstance(value, list) and len(value) <= GROUP_LIST_MAX
            and all(isinstance(x, str) and 1 <= len(x) <= GROUP_ITEM_MAX for x in value))


def parse(raw) -> list:
    """解析结果 → ``[{"chat_id", "allowFrom", "requireMention"}]``（保持文件里的顺序）。"""
    if raw is None:
        raw = {}
    groups = raw.get("groups") if isinstance(raw, dict) else None
    if not isinstance(groups, list):
        raise GroupConfigError("群清单的 groups 必须是列表")
    out, seen = [], set()
    for g in groups:
        chat_id = g.get("chat_id") if isinstance(g, dict) else None
        if not valid_chat_id(chat_id) or chat_id in seen:
            raise GroupConfigError("群清单里有不合规或重复的群号（群号要写成带引号的字符串）")
        allow, mention = g.get("allowFrom", []), g.get("requireMention", False)
        if not _str_list(allow) or not isinstance(mention, bool):
            raise GroupConfigError("群 %s 的 allowFrom 必须是字符串列表、requireMention 必须是布尔" % chat_id)
        seen.add(chat_id)
        out.append({"chat_id": chat_id, "allowFrom": list(allow), "requireMention": mention})
    return out


def load():
    """``None`` = 没有群清单文件；否则同 ``parse``。读不出、解析不了、形状不对 → GroupConfigError。"""
    try:
        with open(path(), encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        raise GroupConfigError("群清单读不出来")
    return parse(raw)


def find(groups, chat_id):
    return next((g for g in groups or () if g["chat_id"] == chat_id), None)


def dump(groups) -> str:
    """文件全文。值用 JSON 写法（YAML 的子集），群号一定带引号，读回来还是字符串。"""
    parse({"groups": groups})   # 先校验，不合规不生成
    lines = [_HEADER.rstrip("\n"), "groups:"]
    for g in groups:
        lines.append("  - chat_id: %s" % json.dumps(g["chat_id"]))
        lines.append("    allowFrom: %s" % json.dumps(list(g["allowFrom"]), ensure_ascii=False))
        lines.append("    requireMention: %s" % ("true" if g["requireMention"] else "false"))
    return "\n".join(lines) + "\n"


def write(groups) -> None:
    """原子写入（同目录临时文件 + 改名），权限 0600。"""
    target = path()
    text = dump(groups)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix="." + FILE_NAME + ".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def lock():
    """群清单的跨进程锁（INTERFACE-bot-features 3.10 第 2 条）：同目录 ``_group_chat.yml.lock`` 上的
    跨平台锁（T03 platform_lock），等 3 秒；锁文件不存在时以 0600 新建，用完不删。
    只把加锁阶段的失败翻成 GroupConfigError，yield 里的读写异常照原样抛。"""
    lock_path = path() + LOCK_SUFFIX
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as e:
        raise GroupConfigError(LOCK_FAILED_PREFIX + type(e).__name__, "lock_failed")
    try:
        deadline = time.monotonic() + LOCK_WAIT_SEC
        try:
            got = platform_lock.acquire(fd, deadline=deadline, poll=LOCK_RETRY_SEC)
        except platform_lock.LockUnsupported as e:
            raise GroupConfigError(LOCK_FAILED_PREFIX + type(e).__name__, "lock_failed")
        if not got:
            raise GroupConfigError(BUSY_MESSAGE, "busy")
        try:
            yield
        finally:
            platform_lock.release(fd)
    finally:
        os.close(fd)

def add_group(chat_id, allow_from, require_mention=False) -> dict:
    """INTERFACE-bot-features 3.2.5：幂等地往群清单加一个群，返回 ``{"created", "group"}``。
    已存在时 ``created: False``、条目原样不动、文件逐字节不变（请求里的 allowFrom 不覆盖已有条目）。
    新条目只写 chat_id、allowFrom、requireMention 三个键，不写 director（导演开关是另一个文件）。
    读不出 → GroupConfigError code ``unreadable``；写失败 → ``write``；锁忙 → ``busy``；
    群号、allowFrom 不合规 → ``invalid``（路由层先用 valid_group_chat_id / 自己的校验挡住，这里再拦一道）。"""
    chat_id = valid_group_chat_id(chat_id)
    if chat_id is None:
        raise GroupConfigError(GROUP_CHAT_ID_MESSAGE, "invalid")
    if not _str_list(allow_from) or not allow_from:
        raise GroupConfigError(GROUP_ALLOW_FROM_MESSAGE, "invalid")
    if not isinstance(require_mention, bool):
        raise GroupConfigError("requireMention 必须是布尔", "invalid")
    with lock():
        try:
            groups = load() or []
        except GroupConfigError:
            raise GroupConfigError(UNREADABLE_MESSAGE, "unreadable")
        existing = find(groups, chat_id)
        if existing is not None:
            return {"created": False, "group": dict(existing)}
        entry = {"chat_id": chat_id, "allowFrom": list(allow_from), "requireMention": require_mention}
        try:
            write(groups + [entry])
        except OSError:
            raise GroupConfigError(NOT_WRITTEN_MESSAGE, "write")
    return {"created": True, "group": entry}


# ── 导演开关文件（INTERFACE-bot-features 3.4.6；3.10 第 1 条：DP = $HOME/.claude/dispatcher）─────────────
# 只由 PUT /api/console/groups/<chat_id>/director 建或删；进群、退群、任何开关变化都不改它。

def dp_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".claude", "dispatcher")


def director_dir() -> str:
    return os.path.join(dp_dir(), ".director-mode")


def director_path(chat_id: str) -> str:
    return os.path.join(director_dir(), str(chat_id))


def director_on(chat_id: str) -> bool:
    """导演开关开着吗（``DP/.director-mode/<群号>`` 存在）。只读，不建目录。"""
    return os.path.isfile(director_path(chat_id))

