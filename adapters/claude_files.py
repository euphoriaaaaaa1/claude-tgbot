"""claude 侧归档、恢复、删除的文件内核（PLAN-bot-lifecycle D4、D7、D11；INTERFACE 3.5.2 至 3.5.4，3.7 第 1、2、7、8、13 条）。

只管文件：按编号拼出白名单路径、收集清单、逐项同卷改名与逆序撤回、废纸篓的选择与回落、旧四只的令牌行（移出、追加、
去掉）、删除时密钥文件的抹掉（三份密钥文件与含供应商密钥的 settings 类文件）、按清单收拾中断的操作（``resolve``；
命令行 ``--recover`` 调同一个函数）。停启、平台服务、
供应商绑定、群、朋友圈都不在这里（适配器与编排层做）；调用方持着生命周期锁再调本模块。

- 本模块不导入旧栈（config_loader、bot_stop、claude_local 都不导入；分层守卫只许 claude_local 碰旧栈）。各入口的 ``ctx``
  由 claude_local 每次调用时现组（``context``）：频道根、配置目录、停用目录这三个接缝的当前值与编号规则、bot 名单都由它
  按参数传进来，其余各根在 ``context`` 里按当前 HOME 现算；模块里不存路径（3.7 第 13 条）。每条路径只由编号
  拼接；父目录链上有软链、自己是软链、或真实路径不在它的根下，整单拒绝、一个都不动。
  频道目录里的软链（如 ``skills/novelai-skill``）随目录整份改名，不跟随。
- 清单 ``manifest.json``（0600）在第一次移动之前落盘，每移一项原子重写一次；撤回与收拾一律以文件的实际位置为准
  （两头只有一头有才动、两头都有就停），所以断在任何一处都接得住，重复做也无害。
- 两种失败出口：``FilesError``（带 code、http_status：什么都没动，或改过的已全部撤回，``state`` 说是哪种）；
  ``FilesStuck``（故意不带 code：撤回没做完、停在中间，交给 ``resolve`` 或人工）。
- 令牌：旧四只那一行整行搬动、不解析值；新式 bot 的 ``.env`` 等三份密钥归档时随频道目录走，删除时先挪进
  ``console-ops/<op_id>.pending-destroy/``，提交点之后 unlink。令牌文件的内容不进日志、异常与返回值。
"""
import errno
import json
import logging
import os
import re
import secrets
import stat
import sys
import time

import yaml

import platform_lock

from .base import ARCHIVE_ID_RE, BAD_ARCHIVE_ID, BAD_SHELL_META as BAD_META, BOT_ID_MESSAGE, shell_meta_ok, valid_bot_id
from .guard import TOKEN_RE, SecretToken

# 操作编号规则（T15 的 ops 模块尚未移植时本模块自己也要能校验）。
OP_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")

LEGACY_BOTS = ("yasuna", "bot2", "bot3", "lili")       # 旧四只：令牌在 bot-tokens.env，由 restart-bots.sh 旧段启动  # scan-release: allow legacy-compat
SECRET_NAMES = (".env", ".worker-secrets.env", ".api-token")
SETTINGS_BASES = ("settings.json", "settings.local.json")   # 频道目录 .claude/ 下的 settings 类文件（3.5.4 B-I1）
SECRET_ENV_KEYS = ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")   # 可能存供应商密钥明文的 env 键
TOKEN_FILE = "bot-tokens.env"
LEGACY_COPY = "secrets/legacy-token.env"
MANIFEST = "manifest.json"
ITEMS = "items"
DELETED_SUFFIX = "-已删除"
PROJECT_NAME_MAX = 200                                  # 超过这么长 Claude Code 改用截断加散列（3.7 第 2 条）
ARCHIVE_KINDS = ("channel", "config", "projects", "tg_media", "state", "logs", "launchd", "moved", "legacy_token")
DELETE_KINDS = ARCHIVE_KINDS[:-1] + ("media", "secret")
_ITEM_NAME_RE = re.compile(r"[0-9]{3}-[^/]+")
_PROJECT_NAME_RE = re.compile(r"[A-Za-z0-9-]{1,200}")
_MANIFEST_MAX = 1 << 20
_TOKEN_MAX = 64 << 10
_BACKUPS_KEEP = 2

_rename = os.rename      # 注入点：单测让第 k 次改名抛错，或在改名前后模拟进程退出
_unlink = os.unlink      # 注入点：只用于提交点之后抹密钥
_log = logging.getLogger(__name__)


def _lock_fd(fd: int) -> None:
    """给已打开的令牌文件加排他锁（T03 跨平台锁），被占时按“文件没动”拒绝。"""
    try:
        if not platform_lock.acquire(fd, deadline=time.monotonic() + 3.0):
            raise FilesError("step_failed", 500, "令牌文件被别的程序占着，没有动任何文件")
    except platform_lock.LockError:
        raise FilesError("step_failed", 500, "令牌文件被别的程序占着，没有动任何文件") from None

NO_BOT = "这个底层没有这只 bot"
NO_ARCHIVE = "没有这份归档"
BAD_OP_ID = "操作编号不合规"
UNSAFE = "%s（%s）是符号链接或不在规定的目录下，没有动任何文件"
NOT_STOPPED = "它还没停用，没有动任何文件"
UNKNOWN_LAUNCHER = "认不出它的启动方式（既不是登记表 bot，也不是旧四只），没有动任何文件"
BAD_CONFIG = "它的配置文件读不出来，没有动任何文件"
SHARED_CHANNEL = "频道目录同时是别的 bot（%s）的，没有动任何文件"
HALF_ARCHIVED = "这只 bot 还有一份没做完的归档或恢复（%s），先重新收拾那一次，没有动任何文件"
TOKEN_COUNT = "令牌文件里它的令牌行有 %d 行（应当恰好 1 行），没有动任何文件"
TOKEN_PRESENT = "令牌文件里已经有它的令牌行，没有恢复"
TOKEN_CHANGED = "令牌文件在改写时被别的程序动过，没有改"
EXISTS = "原处已经有同名 bot（claude/%s），没有恢复"
MISSING = "归档里缺了一项（%s），没有恢复"
BAD_MANIFEST = "清单读不出来或内容不对，没有动任何文件"
RESTORING = "这份归档上次恢复没做完，请先重新收拾那一次（%s）"
MOVE_FAILED = "移动文件时出错（%s），没做成"
NO_TRASH = "系统废纸篓和 ~/.claude/bot-trash 都写不进，没有删除"
TAKEN = "目标位置已经有东西（%s），没有覆盖"
STUCK = "没能撤回干净（%s），文件停在中间；请点“重新收拾”，或运行 python3 -m adapters.claude_files --recover %s"
WIPE_FAILED = "已移进废纸篓，但令牌没抹干净（%s）；点“重新收拾”会再抹一次"
LONG_PROJECT = "会话目录名超过 200 个字符，Claude Code 改用截断加散列命名，本系统不复算，没有处理"
MEDIA_ALIAS = "生图目录名与编号不同，认不准是不是这只 bot 的，没有处理"


class FilesError(Exception):
    """没做成：什么都没动（``state="unchanged"``）或改过的已全部撤回（``"rolled_back"``）。code、http_status 照
    INTERFACE 3.0 第 17 条那张表，message 是给人看的一句（不含令牌）。"""

    def __init__(self, code: str, http_status: int, message: str, *, state: str = "unchanged"):
        super().__init__(message)
        self.code, self.http_status, self.message, self.state = code, http_status, message, state


class FilesStuck(Exception):
    """撤回没做完，文件停在中间（不是安全状态）。故意不带 code、http_status：后台任务框架会把它当作结果未知去复核，
    而不会当成"没做成且没留下改动"去撤别的步骤。message 写清下一步。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# ── 小工具 ──────────────────────────────────────────────────────────────────

def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _stamp() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())


def _reason(exc) -> str:
    """异常给人看的原因：只要类名或 errno 名，不要异常文本（里面可能有路径以外的东西）。"""
    if isinstance(exc, (FilesError, FilesStuck)):
        return exc.message
    if isinstance(exc, OSError) and exc.errno in errno.errorcode:
        return errno.errorcode[exc.errno]
    return type(exc).__name__


def _tilde(path: str, home: str) -> str:
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def _fsync_dir(path: str) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _exists(path: str) -> bool:
    return os.path.lexists(path)


def _read_fd(fd: int, limit: int, *, own: bool = False):
    """读已打开的 fd：不是普通文件、超过 limit、（own 时）属主不是自己 → None。"""
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_size > limit or (own and st.st_uid != os.getuid()):
        return None
    chunks, size = [], 0
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)


def _read_small(path: str, limit: int, label: str, *, own: bool = False) -> bytes:
    """以 O_NOFOLLOW 读一个不大的普通文件。软链、不是普通文件、太大 → FilesError；不存在照常抛 FileNotFoundError。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise FilesError("step_failed", 500, "%s是符号链接，没有动任何文件" % label)
        raise
    try:
        data = _read_fd(fd, limit, own=own)
    finally:
        os.close(fd)
    if data is None:
        raise FilesError("step_failed", 500, "%s不是普通文件、太大或不归当前用户，没有动任何文件" % label)
    return data


def _write_new(path: str, data: bytes, mode: int = 0o600) -> None:
    """新建文件写入（O_EXCL、不跟随软链、权限照给定值，不受 umask 影响）并 fsync；中途失败把半截文件删掉。"""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    ok = False
    try:
        os.fchmod(fd, mode)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
        ok = True
    finally:
        os.close(fd)
        if not ok:
            _unlink_quiet(path)


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _write_json(path: str, data) -> None:
    """同目录临时文件 + fsync + 改名 + 目录 fsync。临时文件名以 ``.<名>.tmp-`` 开头，收拾时认得出、删得掉。"""
    folder = os.path.dirname(path)
    tmp = os.path.join(folder, ".%s.tmp-%s" % (os.path.basename(path), secrets.token_hex(4)))
    _write_new(tmp, json.dumps(data, ensure_ascii=False, indent=1).encode("utf-8"))
    try:
        _rename(tmp, path)
    except Exception:
        _unlink_quiet(tmp)
        raise
    _fsync_dir(folder)


def _save(base: str, manifest: dict) -> None:
    _write_json(os.path.join(base, MANIFEST), manifest)


# ── 根与白名单 ───────────────────────────────────────────────────────────────

def context(*, channels: str, config: str, disabled: str, bot_id_re, non_bot_dirs, known_bots) -> dict:
    """本次调用的路径根与旧栈信息（3.5 路径与接缝、3.7 第 13 条），各入口的 ``ctx`` 就是它。claude_local 每次调用时
    传进三个接缝的当前值（``config_loader.channels_dir()``、``config_dir()``、``bot_stop.DISABLED_DIR``）、yml 文件名的编号
    规则 ``bot_id_re``、不算 bot 的频道子目录 ``non_bot_dirs``、列出 bot 名单的函数 ``known_bots``；其余各根按当前 HOME
    现算。HOME 与三个接缝取真实路径。"""
    home = os.path.realpath(os.path.expanduser("~"))
    claude = os.path.join(home, ".claude")
    return {
        "home": home,
        "dispatcher": os.path.join(claude, "dispatcher"),
        "channels": os.path.realpath(channels),
        "config": os.path.realpath(config),
        "disabled": os.path.realpath(disabled),
        "projects": os.path.join(claude, "projects"),
        "tg_media": os.path.join(claude, "channels", "media"),
        "media": os.path.join(home, "resource", "media"),
        "archive": os.path.join(claude, "bot-archive"),
        "system": os.path.join(home, ".Trash"),
        "own": os.path.join(claude, "bot-trash"),
        "ops": os.path.join(claude, "console-ops"),
        "bot_id_re": bot_id_re,
        "non_bot_dirs": frozenset(non_bot_dirs),
        "known_bots": known_bots,
    }


def _safe_id(bot_id, r: dict) -> bool:
    """编号：壳的口径（valid_bot_id）与 yml 文件名的口径（字母数字开头、不含点）都要过。"""
    return valid_bot_id(bot_id) and r["bot_id_re"].fullmatch(bot_id) is not None


def _check(path: str, root: str, label: str, home: str) -> None:
    """path 是从 root 拼出来的：落在 root 下、父目录链上没有软链、自己不是软链。不满足 → 整单拒绝。"""
    parent = os.path.dirname(path)
    if (not path.startswith(root + os.sep) or os.path.realpath(parent) != parent or os.path.islink(path)
            or os.path.realpath(path) != path):
        raise FilesError("step_failed", 500, UNSAFE % (label, _tilde(path, home)))


def _ensure_dir(path: str, r: dict, label: str) -> None:
    """建家目录下的一个公共目录（0700；已在就用），它和父目录链都不许是软链。"""
    _check(path, r["home"], label, r["home"])
    os.makedirs(path, 0o700, exist_ok=True)
    if os.path.islink(path) or not os.path.isdir(path):
        raise FilesError("step_failed", 500, UNSAFE % (label, _tilde(path, r["home"])))


def _bot(bot_id, r: dict) -> str:
    if not _safe_id(bot_id, r):
        raise FilesError("invalid", 422, BOT_ID_MESSAGE)
    return bot_id


def _op(op_id) -> str:
    if not isinstance(op_id, str) or OP_ID_RE.fullmatch(op_id) is None:
        raise FilesError("invalid", 422, BAD_OP_ID)
    return op_id


def project_dir_name(channel_dir: str) -> str:
    """会话目录名（3.7 第 2 条）：频道目录真实绝对路径里每个不在 [A-Za-z0-9] 的字符换成一个 "-"。
    与 ``chat_history._project_slug_for``、``worker_launch`` 里写死的前缀规则不同，不复用。"""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(channel_dir))


def _load_yml(path: str):
    """这只 bot 的 yml（不跟随软链、不超过 1 MiB）。不存在 → None。"""
    try:
        raw = _read_small(path, _MANIFEST_MAX, "配置文件")
    except FileNotFoundError:
        return None
    except OSError:
        raise FilesError("step_failed", 500, BAD_CONFIG)
    try:
        cfg = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        raise FilesError("step_failed", 500, BAD_CONFIG)
    if not isinstance(cfg, dict):
        raise FilesError("step_failed", 500, BAD_CONFIG)
    return cfg


def _channel_dir(bot: str, cfg: dict, r: dict, known: list) -> str:
    """频道目录：yml 的 bot_channel_path（没写时是 channels/<编号>，与 config_loader 同口径）。真实路径必须恰好在频道根下
    一层、名字合编号规则、不是 media 这类共用目录、自己不是软链，也不许同时是别的 bot 的频道目录。返回真实路径。"""
    raw = cfg.get("bot_channel_path")
    if raw in (None, ""):
        path = os.path.join(r["channels"], bot)
    elif isinstance(raw, str):
        path = os.path.abspath(os.path.expanduser(raw))
    else:
        raise FilesError("step_failed", 500, BAD_CONFIG)
    name, real = os.path.basename(path.rstrip(os.sep)), os.path.realpath(path)
    if (os.path.islink(path.rstrip(os.sep)) or os.path.dirname(real) != r["channels"] or os.path.basename(real) != name
            or not r["bot_id_re"].fullmatch(name) or name in r["non_bot_dirs"]):
        raise FilesError("step_failed", 500, UNSAFE % ("频道目录", _tilde(path, r["home"])))
    for item in known:
        other = item.get("bot_dir")
        if item.get("bot_id") != bot and isinstance(other, str) and os.path.realpath(os.path.expanduser(other)) == real:
            raise FilesError("step_failed", 500, SHARED_CHANNEL % item["bot_id"])
    return real


def _settings_name(name: str) -> bool:
    """settings 类文件：settings.json、settings.local.json 本身，以及同名前缀的备份文件（``settings.json.bak-…`` 等）。"""
    return any(name == base or name.startswith(base + ".") for base in SETTINGS_BASES)


def _settings_has_secret(path: str) -> bool:
    """settings 类文件的 ``env`` 下有供应商密钥键 → True（3.5.4 B-I1）。读不出、解析不了、形状不对一律按有密钥处理：
    宁可多抹一份 settings，也不把供应商密钥留在废纸篓里。"""
    try:
        data = json.loads(_read_small(path, _MANIFEST_MAX, "settings 文件").decode("utf-8"))
    except (OSError, FilesError, UnicodeDecodeError, ValueError, RecursionError):
        return True
    if not isinstance(data, dict):
        return True
    env = data.get("env")
    if env is None:
        return False
    return not isinstance(env, dict) or any(k in env for k in SECRET_ENV_KEYS)


def _plan(bot: str, purpose: str, r: dict) -> dict:
    """算出这只 bot 的清单，只看不动（D7 表、3.7 第 7 条）。purpose 为 "archive" 或 "delete"。
    返回 roots、launcher、display_name、channel、items（[(kind, 路径)]，按移动顺序）、secrets（删除时先抹的密钥文件）、
    unhandled。"""
    home, disp = r["home"], r["dispatcher"]
    for base, m in _scan(r["archive"], r, prefix=bot + "-"):
        if m["bot_id"] == bot and (m.get("complete") is not True or m.get("restoring")):
            # 再动原处剩下的，这只 bot 就会散在两处（3.0 第 6 条"安全状态"）
            raise FilesError("step_failed", 500, HALF_ARCHIVED % os.path.basename(base))
    yml = os.path.join(r["config"], bot + ".yml")
    _check(yml, r["config"], "配置文件", home)
    cfg = _load_yml(yml)
    if cfg is None:
        if _exists(os.path.join(r["channels"], bot)):
            raise FilesError("step_failed", 500, UNKNOWN_LAUNCHER)
        raise FilesError("unknown_bot", 404, NO_BOT)
    # 旧四只一律按旧式处理（bot-launch.sh 也硬拒它们）；其余只认 yml 里写明 launcher: registry 的
    launcher = "legacy" if bot in LEGACY_BOTS else ("registry" if cfg.get("launcher") == "registry" else None)
    if launcher is None:
        raise FilesError("step_failed", 500, UNKNOWN_LAUNCHER)
    known = r["known_bots"]()
    ch = _channel_dir(bot, cfg, r, known)
    # 前缀 "<编号>-" 会顺带命中编号以 "<编号>-" 开头的别的 bot 的文件，那些要剔掉
    others = [b["bot_id"] for b in known if b.get("bot_id") != bot and str(b.get("bot_id")).startswith(bot + "-")]
    items, unhandled, secret_files = [], [], []

    def add(kind, path, root, label):
        if _exists(path):
            _check(path, root, label, home)
            items.append((kind, path))

    def add_prefixed(kind, folder, prefix, root, label):
        if folder != root:
            if not _exists(folder):
                return
            _check(folder, root, label, home)
        if not os.path.isdir(folder):
            return
        for name in sorted(os.listdir(folder)):
            if name.startswith(prefix) and not any(name.startswith(o + "-") for o in others):
                add(kind, os.path.join(folder, name), root, label)

    if purpose == "delete":
        for name in SECRET_NAMES:
            p = os.path.join(ch, name)
            if _exists(p):
                _check(p, r["channels"], "密钥文件", home)
                if not os.path.isfile(p):
                    raise FilesError("step_failed", 500, UNSAFE % ("密钥文件", _tilde(p, home)))
                secret_files.append(p)
        # .claude/settings*.json 里可能有供应商密钥明文（provider_config 写入）：按密钥处理，不随频道目录进废纸篓
        sdir = os.path.join(ch, ".claude")
        if os.path.isdir(sdir) and not os.path.islink(sdir):
            try:
                names = sorted(os.listdir(sdir))
            except OSError as e:
                raise FilesError("step_failed", 500, MOVE_FAILED % _reason(e))
            for name in names:
                p = os.path.join(sdir, name)
                if not _settings_name(name) or not _exists(p) or not os.path.isfile(p):
                    continue
                _check(p, r["channels"], "settings 文件", home)
                if _settings_has_secret(p):
                    secret_files.append(p)
    add("channel", ch, r["channels"], "频道目录")
    add("config", yml, r["config"], "配置文件")
    add("config", yml + ".life.lock", r["config"], "配置文件锁")
    add_prefixed("config", r["config"], bot + ".yml.bak-", r["config"], "配置备份")
    proj_name = project_dir_name(ch)
    proj = os.path.join(r["projects"], proj_name)
    if len(proj_name) > PROJECT_NAME_MAX:
        unhandled.append({"kind": "projects", "path": _tilde(proj, home), "reason": LONG_PROJECT})
    else:
        add("projects", proj, r["projects"], "会话目录")
    add("tg_media", os.path.join(r["tg_media"], bot), r["tg_media"], "附件目录")
    if purpose == "delete":
        add("media", os.path.join(r["media"], bot), r["media"], "生图目录")
        alias = os.path.basename(ch)
        if alias != bot and _exists(os.path.join(r["media"], alias)):
            unhandled.append({"kind": "media", "path": _tilde(os.path.join(r["media"], alias), home),
                              "reason": MEDIA_ALIAS})
    add_prefixed("state", os.path.join(disp, ".self-initiate-state"), bot + "-", disp, "主动消息状态")
    add_prefixed("state", os.path.join(disp, ".jiwen-state"), bot + "-", disp, "积温状态")
    for name in (".provfp-" + bot, ".watchdog-last-restart." + bot, ".watchdog-fail-count." + bot):
        add("state", os.path.join(disp, name), disp, "调度器状态")
    add("state", os.path.join(r["disabled"], "promise-" + bot), r["disabled"], "承诺标记")
    add("moved", os.path.join(disp, ".moved", bot), disp, "已迁出标记")
    add("logs", os.path.join(disp, "logs", "dispatcher-%s.log" % bot), disp, "日志")
    if purpose == "delete" and launcher == "registry":
        # 新式 bot 的停用标记随删除件走，放最后：前面断了它仍是停用态（旧四只的留作墓碑）
        add("state", os.path.join(r["disabled"], bot), r["disabled"], "停用标记")
    display = cfg.get("display_name")
    return {"roots": r, "launcher": launcher, "display_name": display if isinstance(display, str) and display else bot,
            "channel": ch, "items": items, "secrets": secret_files, "unhandled": unhandled}


def _allowed(kind: str, path, bot: str, r: dict, channel) -> bool:
    """清单里的原位置是否在这只 bot 的白名单里（按当前 HOME 与接缝现算）：防止被改过的清单把东西放到别处。"""
    if not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path:
        return False
    folder, name = os.path.split(path)
    disp = r["dispatcher"]
    if kind == "channel":
        return folder == r["channels"] and r["bot_id_re"].fullmatch(name) is not None and name not in r["non_bot_dirs"]
    if kind == "config":
        return folder == r["config"] and (name in (bot + ".yml", bot + ".yml.life.lock")
                                          or name.startswith(bot + ".yml.bak-"))
    if kind == "projects":
        return folder == r["projects"] and _PROJECT_NAME_RE.fullmatch(name) is not None
    if kind == "state":
        if folder in (os.path.join(disp, ".self-initiate-state"), os.path.join(disp, ".jiwen-state")):
            return name.startswith(bot + "-")
        return path in (os.path.join(disp, ".provfp-" + bot), os.path.join(disp, ".watchdog-last-restart." + bot),
                        os.path.join(disp, ".watchdog-fail-count." + bot),
                        os.path.join(r["disabled"], "promise-" + bot), os.path.join(r["disabled"], bot))
    if kind == "logs":
        return path == os.path.join(disp, "logs", "dispatcher-%s.log" % bot)
    exact = {"tg_media": os.path.join(r["tg_media"], bot), "media": os.path.join(r["media"], bot),
             "moved": os.path.join(disp, ".moved", bot)}
    if kind in exact:
        return path == exact[kind]
    if kind == "secret":
        if channel is None:
            return False
        if folder == channel:
            return name in SECRET_NAMES
        return folder == os.path.join(channel, ".claude") and _settings_name(name)
    if kind == "legacy_token":
        return bot in LEGACY_BOTS and path == os.path.join(disp, TOKEN_FILE)
    return False


def _read_manifest(base: str, r: dict) -> dict:
    """读并校验一份清单（不跟随软链、不超过 1 MiB）：编号、launcher、items 的形状。不存在照常抛 FileNotFoundError。"""
    try:
        m = json.loads(_read_small(os.path.join(base, MANIFEST), _MANIFEST_MAX, "清单").decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise FilesError("step_failed", 500, BAD_MANIFEST)
    ok = (isinstance(m, dict) and _safe_id(m.get("bot_id"), r) and m.get("launcher") in ("registry", "legacy")
          and isinstance(m.get("op_id"), str) and OP_ID_RE.fullmatch(m["op_id"]) and isinstance(m.get("items"), list)
          and all(isinstance(it, dict) and isinstance(it.get("kind"), str) and isinstance(it.get("to"), str)
                  and isinstance(it.get("done"), bool) for it in m["items"]))
    if not ok:
        raise FilesError("step_failed", 500, BAD_MANIFEST)
    return m


def _pairs(m: dict, base: str, r: dict, kinds) -> list:
    """清单的每一项 → (项, 在归档区或删除件里的位置, 原位置)；任何一项不在白名单里 → 整份不认。"""
    bot = m["bot_id"]
    channel = next((it.get("from") for it in m["items"] if it["kind"] == "channel"), None)
    pending = os.path.join(r["ops"], m["op_id"] + ".pending-destroy")
    out = []
    for it in m["items"]:
        kind, to, src = it["kind"], it["to"], it.get("from")
        if kind not in kinds or not _allowed(kind, src, bot, r, channel):
            raise FilesError("step_failed", 500, BAD_MANIFEST)
        if kind == "legacy_token" and to == LEGACY_COPY:
            held = os.path.join(base, LEGACY_COPY)
        elif kind == "secret" and to == os.path.join(pending, os.path.basename(src)):
            held = to
        elif (kind not in ("legacy_token", "secret") and to.startswith(ITEMS + "/")
              and _ITEM_NAME_RE.fullmatch(to[len(ITEMS) + 1:])):
            held = os.path.join(base, to)
        else:
            raise FilesError("step_failed", 500, BAD_MANIFEST)
        out.append((it, held, src))
    return out


# ── 移动与撤回 ───────────────────────────────────────────────────────────────

def _move(src: str, dst: str) -> bool:
    """把 src 同卷改名到 dst，以实际位置为准：只有 src 在 → 改名；只有 dst 在 → 已经动过；两头都没有 → 不用动；
    两头都在 → 停下不覆盖。返回是否真的改了名。"""
    if _exists(dst):
        if _exists(src):
            raise FilesError("step_failed", 500, TAKEN % os.path.basename(dst))
        return False
    if not _exists(src):
        return False
    _rename(src, dst)
    return True


def _drop_dir(base: str) -> None:
    """删掉一份撤回完了的归档目录或删除件：子目录必须已经空了（不空说明还有东西没撤回，os.rmdir 会报错停下），
    清单最后删，免得删了清单却还有东西在里面。"""
    for sub in (os.path.join(base, "secrets"), os.path.join(base, ITEMS)):
        if os.path.isdir(sub) and not os.path.islink(sub):
            os.rmdir(sub)
    for name in os.listdir(base):
        if name.startswith("." + MANIFEST + ".tmp-"):
            os.unlink(os.path.join(base, name))
    _unlink_quiet(os.path.join(base, MANIFEST))
    os.rmdir(base)


def _new_dir(parent: str, make_name) -> str:
    """在 parent 下建一个以当前 UTC 秒命名的新目录（0700）。同一秒里已有同名目录就等到下一秒再建，最多三次。"""
    for _ in range(3):
        path = os.path.join(parent, make_name(_stamp()))
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            time.sleep(1.05 - time.time() % 1)
            continue
        os.chmod(path, 0o700)
        return path
    raise FilesError("step_failed", 500, MOVE_FAILED % "EEXIST")


def _abort(exc, touched: bool, undo, op_id: str):
    """提交点之前出错：撤回，再按是否改动过原处抛 FilesError；撤不回就抛 FilesStuck。"""
    _log.warning("文件操作 %s 出错，撤回：%s", op_id, _reason(exc))
    try:
        undo()
    except Exception as e:
        _log.error("文件操作 %s 撤回出错：%s", op_id, _reason(e))
        raise FilesStuck(STUCK % (_reason(e), op_id)) from None
    state = "rolled_back" if touched else "unchanged"
    if isinstance(exc, FilesError):
        raise FilesError(exc.code, exc.http_status, exc.message, state=state) from None
    raise FilesError("step_failed", 500, MOVE_FAILED % _reason(exc), state=state) from None


def _require_stopped(bot: str, r: dict) -> None:
    """停用的唯一判定：yml 顶层 ``enabled: false``（T04）。旧 ``.disabled`` 标记只读兼容。"""
    if _exists(os.path.join(r["disabled"], bot)):
        return
    cfg = _load_yml(os.path.join(r["config"], bot + ".yml")) or {}
    value = cfg.get("enabled")
    if value is not None and not bool(value):
        return
    raise FilesError("step_failed", 500, NOT_STOPPED)


def _precheck(bot: str, purpose: str, r: dict) -> dict:
    """归档、删除动手前的核对（只看不动）：清单算得出、停用标记在、旧四只的令牌行恰好 1 行。返回 _plan 的结果。"""
    plan = _plan(bot, purpose, r)
    _require_stopped(bot, r)
    if plan["launcher"] == "legacy":       # 先只读核对令牌行恰好 1 行：不合就什么都不动
        count = len(_hits(_read_tokens(r), bot))
        if count != 1:
            raise FilesError("step_failed", 500, TOKEN_COUNT % count)
    return plan


def precheck(bot_id, purpose: str, *, ctx) -> dict:
    """只看不动：``archive``、``delete`` 动手之前会拒绝的情形在这里先查一遍，抛同样的 ``FilesError``（state 恒为
    unchanged），供调用方在停启动项、解绑之前就把单次拒绝挡下。purpose 为 "archive" 或 "delete"。
    返回 ``{"launcher", "display_name"}``。真做时还会再查一遍（两次之间别人可能动过）。"""
    if purpose not in ("archive", "delete"):
        raise ValueError(purpose)
    plan = _precheck(_bot(bot_id, ctx), purpose, ctx)
    return {"launcher": plan["launcher"], "display_name": plan["display_name"]}


def _json_meta(value):
    """归档清单里的 shell_meta：None 或 base.shell_meta_ok 认可的对象（与 dsh 侧、壳入口同一条规则）。"""
    if value is None:
        return None
    if not shell_meta_ok(value):
        raise FilesError("invalid", 422, BAD_META)
    return value


def _binding_of(value):
    """只记 3.5.2 的三个键（base_url、密钥指纹这些不进清单）。"""
    if not isinstance(value, dict):
        return None
    return {k: value.get(k) for k in ("provider_id", "model", "effort")}


# ── 旧四只的令牌行（3.7 第 1 条；D4）──────────────────────────────────────────

def _token_path(r: dict) -> str:
    return os.path.join(r["dispatcher"], TOKEN_FILE)


def _token_prefix(bot: str) -> bytes:
    return ("TELEGRAM_BOT_TOKEN_%s=" % bot.upper()).encode("ascii")


def _hits(data: bytes, bot: str) -> list:
    """以 "TELEGRAM_BOT_TOKEN_<编号大写>=" 开头的行，各自的 (起, 止)（止含行尾的 \\n）。只按 \\n 切行，不解析值。"""
    prefix, out, pos = _token_prefix(bot), [], 0
    while pos < len(data):
        end = data.find(b"\n", pos)
        end = len(data) if end < 0 else end + 1
        if data.startswith(prefix, pos):
            out.append((pos, end))
        pos = end
    return out


def _valid_line(line: bytes, bot: str) -> bool:
    return line.startswith(_token_prefix(bot)) and b"\n" not in line[:-1]


def _appended(data: bytes, line: bytes) -> bytes:
    """追加到末尾；原文件非空且不以 \\n 结尾时先补一个 \\n。"""
    return data + (b"\n" if data and not data.endswith(b"\n") else b"") + line


def _without(bot: str, line=None, *, missing_ok: bool = False):
    """改写函数：去掉它那一行。匹配恰好 1 行才改（line 给了还要逐字节相同）；0 行在 missing_ok 时不改，否则与 2 行以上
    一样拒绝。"""
    def transform(data: bytes) -> bytes:
        hits = _hits(data, bot)
        if not hits and missing_ok:
            return data
        if len(hits) != 1:
            raise FilesError("step_failed", 500, TOKEN_COUNT % len(hits))
        start, end = hits[0]
        if line is not None and data[start:end] != line:
            raise FilesError("step_failed", 500, TOKEN_CHANGED)
        return data[:start] + data[end:]
    return transform


def _with(bot: str, line: bytes):
    """改写函数：追加它那一行；文件里已经有它的行就拒绝。"""
    def transform(data: bytes) -> bytes:
        if _hits(data, bot):
            raise FilesError("exists", 409, TOKEN_PRESENT)
        return _appended(data, line)
    return transform


def _read_tokens(r: dict) -> bytes:
    """令牌文件当前内容（不存在当空）。只给本模块内部比对用，不往外交。"""
    path = _token_path(r)
    _check(path, r["dispatcher"], "令牌文件", r["home"])
    try:
        return _read_small(path, _TOKEN_MAX, "令牌文件", own=True)
    except FileNotFoundError:
        return b""


# ── 切换时自动转交（D13 第 1、4 条；INTERFACE 3.4.2 E、3.7 第 1 条）──────────────────────────────
TAKE_DENIED = "只能在进行中的切换任务的取令牌步骤里取令牌"
TAKE_ONCE = "这次切换已经取过令牌"
TAKE_UNREADABLE = "读不出它的令牌（文件不存在、格式不对或权限过宽）"


def _running_step(rec: dict):
    for item in rec.get("steps") or ():
        if isinstance(item, dict) and item.get("status") == "running":
            return item.get("step")
    return None


def _token_line_value(line: bytes):
    """令牌行 ``=`` 后的值：去首尾空白，去掉成对的单/双引号；不合令牌形状 → None。"""
    try:
        value = line.split(b"=", 1)[1].decode("utf-8").strip()
    except (IndexError, UnicodeDecodeError):
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value if TOKEN_RE.fullmatch(value) is not None else None


def _read_token_file(path: str) -> bytes:
    """按 D13 的约束读一份令牌文件：O_NOFOLLOW、普通文件、属主是自己、权限不宽于 0600、不超过 64 KiB。"""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise FilesError("step_failed", 500, TAKE_UNREADABLE) from None
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or stat.S_IMODE(st.st_mode) & 0o077):
            raise FilesError("step_failed", 500, TAKE_UNREADABLE)
        data = _read_fd(fd, _TOKEN_MAX)
    finally:
        os.close(fd)
    if data is None:
        raise FilesError("step_failed", 500, TAKE_UNREADABLE)
    return data


def _one_token_line(data: bytes, prefix: bytes):
    """恰好一行以 prefix 开头；多行、零行或值不合形状 → 读不出。"""
    lines = [ln for ln in data.split(b"\n") if ln.startswith(prefix)]
    if len(lines) != 1:
        raise FilesError("step_failed", 500, TAKE_UNREADABLE)
    value = _token_line_value(lines[0])
    if value is None:
        raise FilesError("step_failed", 500, TAKE_UNREADABLE)
    return value


def take_token(bot_id, op, *, ctx) -> SecretToken:
    """D13：只在切换任务的 ``take_token`` 步骤里、由该任务取一次令牌。返回 ``SecretToken``。

    ``op`` 可以是任务句柄（``Task``，带 ``rec``/``data``/``save``）或任务记录（dict）。门不满足就拒绝；
    取到后立刻在任务记录里写 ``token_taken: true``（只有这个布尔，没有令牌）。
    新式 bot 读 ``<频道目录>/.env`` 的 ``TELEGRAM_BOT_TOKEN=`` 行；旧四只读 ``bot-tokens.env`` 里它的那一行。
    """
    r = ctx
    bot = _bot(bot_id, r)
    rec = getattr(op, "rec", op)
    if not isinstance(rec, dict) or rec.get("kind") != "switch" or rec.get("status") != "running" \
            or _running_step(rec) != "take_token" or rec.get("bot_id") != bot_id \
            or rec.get("backend") != "claude":
        raise FilesError("step_failed", 500, TAKE_DENIED)
    inner = rec.get("_internal")
    if not isinstance(inner, dict):
        inner = rec["_internal"] = {}
    data = inner.get("data")
    if not isinstance(data, dict):
        data = inner["data"] = {}
    if data.get("token_taken") is True:
        raise FilesError("step_failed", 500, TAKE_ONCE)
    if bot in LEGACY_BOTS:
        value = _one_token_line(_read_token_file(_token_path(r)), _token_prefix(bot))
    else:
        cfg = _load_yml(os.path.join(r["config"], bot + ".yml")) or {}
        channel = cfg.get("bot_channel_path")
        if not isinstance(channel, str) or not os.path.isabs(channel):
            channel = os.path.join(r["channels"], bot)
        _check(channel, r["channels"], "频道目录", r["home"])
        value = _one_token_line(_read_token_file(os.path.join(channel, ".env")),
                                b"TELEGRAM_BOT_TOKEN=")
    data["token_taken"] = True      # 只记这个布尔；令牌本身不进记录
    if hasattr(op, "data"):
        op.save()
    return SecretToken(value)


def _prune_backups(folder: str) -> None:
    """失败时留作现场的备份，最多留最近 2 份。"""
    try:
        names = [n for n in os.listdir(folder) if n.startswith(TOKEN_FILE + ".")]
        names.sort(key=lambda n: (os.lstat(os.path.join(folder, n)).st_mtime, n))
        for name in names[:-_BACKUPS_KEEP]:
            os.unlink(os.path.join(folder, name))
    except OSError as e:
        _log.warning("修剪令牌备份出错：%s", _reason(e))


def _rewrite_tokens(r: dict, transform) -> tuple:
    """改写 bot-tokens.env：transform(原内容) → 新内容（拒绝就抛 FilesError，什么都不动；新内容等于原内容也不动）。
    在文件锁内：写前把原文件复制到 .token-backups/（0600），同目录临时文件 + fsync，改名前确认原文件没被换掉或改过，
    改名 + 目录 fsync，复核新内容后删掉备份；任何一步失败，原文件不变，备份留作现场（最多 2 份）。
    返回 (原内容, 新内容)，只在调用方内存里用于逐字节撤回。"""
    path, folder = _token_path(r), r["dispatcher"]
    _check(path, folder, "令牌文件", r["home"])
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        fd = None
    except OSError as e:
        raise FilesError("step_failed", 500, UNSAFE % ("令牌文件", _tilde(path, r["home"])) if e.errno == errno.ELOOP
                         else MOVE_FAILED % _reason(e))
    try:
        if fd is None:
            orig, mode, ident = b"", 0o600, None
        else:
            _lock_fd(fd)
            orig = _read_fd(fd, _TOKEN_MAX, own=True)
            if orig is None:
                raise FilesError("step_failed", 500, "令牌文件不是普通文件、太大或不归当前用户，没有动任何文件")
            st = os.fstat(fd)
            mode, ident = (stat.S_IMODE(st.st_mode) & 0o600) or 0o600, (st.st_dev, st.st_ino)
        new = transform(orig)
        if new == orig:
            return orig, new
        backups = os.path.join(folder, ".token-backups")
        _check(backups, folder, "令牌备份目录", r["home"])
        os.makedirs(backups, 0o700, exist_ok=True)
        backup = os.path.join(backups, "%s.%s-%s" % (TOKEN_FILE, _stamp(), secrets.token_hex(4)))
        _write_new(backup, orig)
        tmp = os.path.join(folder, ".%s.tmp-%s" % (TOKEN_FILE, secrets.token_hex(4)))
        try:
            _write_new(tmp, new, mode)
            try:
                cur = os.stat(path, follow_symlinks=False)
                same = (ident is not None and (cur.st_dev, cur.st_ino) == ident
                        and os.pread(fd, _TOKEN_MAX + 1, 0) == orig)
            except FileNotFoundError:
                same = ident is None
            if not same:
                raise FilesError("step_failed", 500, TOKEN_CHANGED)
            _rename(tmp, path)
        except Exception:
            _unlink_quiet(tmp)
            _prune_backups(backups)
            raise
        _fsync_dir(folder)
        if _read_small(path, _TOKEN_MAX, "令牌文件", own=True) != new:
            _prune_backups(backups)
            raise FilesStuck("令牌文件改写后复核不一致，备份留在 ~/.claude/dispatcher/.token-backups/，请人工核对")
        try:                                # 改写已经做成：备份删不掉只记一笔，不把做成的当失败
            os.unlink(backup)
            _fsync_dir(backups)
        except OSError as e:
            _log.warning("令牌备份删不掉：%s", _reason(e))
        return orig, new
    finally:
        if fd is not None:
            os.close(fd)


def _purge_backups(bot: str, r: dict) -> None:
    """删除做完后，失败时留下的现场备份里若还有它的令牌行，整份删掉：删掉的 bot 的令牌不留在任何地方（4.2 关键测试）。"""
    folder = os.path.join(r["dispatcher"], ".token-backups")
    try:
        names = [n for n in os.listdir(folder) if n.startswith(TOKEN_FILE + ".")]
    except FileNotFoundError:
        return
    for name in names:
        path = os.path.join(folder, name)
        try:
            data = _read_small(path, _TOKEN_MAX, "令牌备份", own=True)
        except FileNotFoundError:
            continue
        if _hits(data, bot):
            os.unlink(path)


def _token_out(bot: str, r: dict, base: str) -> dict:
    """归档时搬出那一行：先把它原样写进 <归档>/secrets/legacy-token.env（目录 0700、文件 0600），再从 bot-tokens.env 去掉。
    先写副本后改原文件：断在中间时令牌至少还在一处。"""
    data = _read_tokens(r)
    hits = _hits(data, bot)
    if len(hits) != 1:
        raise FilesError("step_failed", 500, TOKEN_COUNT % len(hits))
    line = data[hits[0][0]:hits[0][1]]
    sdir = os.path.join(base, "secrets")
    os.mkdir(sdir, 0o700)
    os.chmod(sdir, 0o700)
    _write_new(os.path.join(base, LEGACY_COPY), line)
    _fsync_dir(sdir)
    orig, new = _rewrite_tokens(r, _without(bot, line))
    return {"orig": orig, "new": new}


def _token_back(bot: str, r: dict, copy_path: str, tok) -> None:
    """撤回归档时把那一行放回：进程内撤回有原内容就逐字节复原，否则追加到末尾；已经在了就不动。然后删掉副本。"""
    try:
        line = _read_small(copy_path, _TOKEN_MAX, "令牌副本", own=True)
    except FileNotFoundError:
        if not _hits(_read_tokens(r), bot):
            raise FilesStuck("它的令牌行在令牌文件和归档里都找不到了，请人工核对")
        return
    if not _valid_line(line, bot):
        raise FilesStuck("归档里的令牌副本内容不对，请人工核对")
    orig, new = (tok or {}).get("orig"), (tok or {}).get("new")

    def transform(cur: bytes) -> bytes:
        if orig is not None and cur == new:
            return orig
        return cur if _hits(cur, bot) else _appended(cur, line)
    _rewrite_tokens(r, transform)
    os.unlink(copy_path)
    _fsync_dir(os.path.dirname(copy_path))


def _sweep_token_tmp(r: dict) -> None:
    """进程半路没了时留在调度器目录里的令牌临时文件（内容是整份令牌文件），收拾时删掉。"""
    try:
        names = os.listdir(r["dispatcher"])
    except OSError:
        return
    for name in names:
        if name.startswith(".%s.tmp-" % TOKEN_FILE):
            _unlink_quiet(os.path.join(r["dispatcher"], name))


# ── 归档（3.5.2）────────────────────────────────────────────────────────────

def archive(bot_id, *, op_id, ctx, shell_meta=None, provider_binding=None, before_move=None) -> dict:
    """把这只 bot 的文件整份移到 ``~/.claude/bot-archive/<编号>-<UTC>/``（D7 表"归档"一列）。调用方已停下它（停用标记在）、
    已退群；生图目录、停用标记、朋友圈库不动。``shell_meta``、``provider_binding`` 原样记进清单。
    ``before_move``：清单落盘之后、第一次移动之前调一次（调用方在这里 bootout、解绑供应商：断在哪里清单里都查得到这次
    操作和原来的绑定）；它抛错就当这一步没做成，撤回后抛 ``FilesError``，它自己做了一半的由调用方撤。

    成功 → ``{"archive_id", "path", "launcher", "unhandled"}``。失败 → ``FilesError``（已撤回到操作前）或 ``FilesStuck``。"""
    r = ctx
    bot, op_id = _bot(bot_id, r), _op(op_id)
    meta, binding = _json_meta(shell_meta), _binding_of(provider_binding)
    plan = _precheck(bot, "archive", r)
    _ensure_dir(r["archive"], r, "归档区")
    base = _new_dir(r["archive"], lambda s: "%s-%s" % (bot, s))
    items = [{"kind": kind, "from": path, "to": _item_name(i, path), "done": False}
             for i, (kind, path) in enumerate(plan["items"])]
    if plan["launcher"] == "legacy":       # 令牌行最后搬：前面任何一项出错时它还没动
        items.append({"kind": "legacy_token", "from": _token_path(r), "to": LEGACY_COPY, "done": False})
    m = {"archive_id": os.path.basename(base), "bot_id": bot, "person": bot, "display_name": plan["display_name"],
         "archived_at": _now(), "launcher": plan["launcher"], "complete": False, "op_id": op_id, "items": items,
         "provider_binding": binding, "shell_meta": meta, "unhandled": plan["unhandled"]}
    st = {"touched": False, "token": None}
    try:
        _save(base, m)
        if before_move is not None:
            before_move()
        os.mkdir(os.path.join(base, ITEMS), 0o700)
        for it in items:
            if it["kind"] == "legacy_token":
                st["token"] = _token_out(bot, r, base)
                st["touched"] = True
            elif _move(it["from"], os.path.join(base, it["to"])):
                st["touched"] = True
            it["done"] = True
            _save(base, m)
        m["complete"] = True
        _save(base, m)
    except Exception as e:
        _abort(e, st["touched"], lambda: _undo_archive(base, m, r, st["token"]), op_id)
    _log.info("已归档 bot=%s 条目=%d", bot, len(items))
    return {"archive_id": m["archive_id"], "path": base, "launcher": plan["launcher"], "unhandled": plan["unhandled"]}


def _item_name(index: int, path: str) -> str:
    name = os.path.basename(path)
    return "%s/%03d-%s" % (ITEMS, index, name if len(name.encode("utf-8")) <= 240 else "item")


def _undo_archive(base: str, m: dict, r: dict, tok) -> None:
    """撤回归档：逆序放回原处（令牌行最先，逐字节或追加），再删掉归档目录。能重复做，接得住只做了一半。"""
    bot = m["bot_id"]
    for it, held, src in reversed(_pairs(m, base, r, ARCHIVE_KINDS)):
        if it["kind"] == "legacy_token":
            _token_back(bot, r, held, tok)
        else:
            _move(held, src)
        it["done"] = False
    _drop_dir(base)


# ── 恢复（3.5.3）────────────────────────────────────────────────────────────

def _archive_dir(archive_id, r: dict) -> str:
    if not isinstance(archive_id, str) or not ARCHIVE_ID_RE.fullmatch(archive_id) or archive_id.strip(".") == "":
        raise FilesError("invalid", 422, BAD_ARCHIVE_ID)
    base = os.path.join(r["archive"], archive_id)
    if not _exists(base):
        raise FilesError("unknown_archive", 404, NO_ARCHIVE)
    _check(base, r["home"], "归档目录", r["home"])
    if not os.path.isdir(base):
        raise FilesError("unknown_archive", 404, NO_ARCHIVE)
    return base


def _restorable(archive_id, r: dict) -> tuple:
    """恢复前先认这份归档：编号合规、在归档区、清单读得出且是它自己的、不在恢复中。返回 (目录, 清单)。"""
    base = _archive_dir(archive_id, r)
    try:
        m = _read_manifest(base, r)
    except FileNotFoundError:
        raise FilesError("step_failed", 500, BAD_MANIFEST)
    if m.get("archive_id") != archive_id:
        raise FilesError("step_failed", 500, BAD_MANIFEST)
    if m.get("restoring"):
        raise FilesError("step_failed", 500, RESTORING % (m["restoring"] or {}).get("op_id"))
    return base, m


def peek(archive_id, *, ctx) -> dict:
    """只看不动：``restore`` 动手之前会拒绝的情形（编号不合规、没有这份归档、清单不对、恢复中、原处已有同名、归档里缺项、
    令牌行已在）在这里先查一遍，抛同样的 ``FilesError``（state 恒为 unchanged）。供调用方在重新绑定供应商之前就挡下
    单次拒绝。返回 ``{"bot_id", "launcher", "display_name", "provider_binding", "shell_meta"}``。"""
    base, m = _restorable(archive_id, ctx)
    _restore_plan(m, base, ctx)
    return {"bot_id": m["bot_id"], "launcher": m["launcher"], "display_name": m.get("display_name"),
            "provider_binding": m.get("provider_binding"), "shell_meta": m.get("shell_meta")}


def _restore_plan(m: dict, base: str, r: dict) -> tuple:
    """恢复前核对（只看不动）：要放回的每一项归档区里在、原处空着；令牌行副本合规且令牌文件里没有它的行。
    返回 (清单项与位置, 要追加的令牌行或 None)。incomplete 的归档：没移走的本来就在原处，跳过。"""
    bot, pairs, line = m["bot_id"], _pairs(m, base, r, ARCHIVE_KINDS), None
    for it, held, src in pairs:
        in_archive, at_home = _exists(held), _exists(src)
        if it["kind"] == "legacy_token":
            present = bool(_hits(_read_tokens(r), bot))
            if in_archive:
                line = _read_small(held, _TOKEN_MAX, "令牌副本", own=True)
                if not _valid_line(line, bot):
                    raise FilesError("step_failed", 500, BAD_MANIFEST)
                if present:
                    raise FilesError("exists", 409, TOKEN_PRESENT)
            elif it["done"] and not present:
                raise FilesError("step_failed", 500, MISSING % it["kind"])
            continue
        if in_archive and at_home or (it["done"] and at_home):
            raise FilesError("exists", 409, EXISTS % bot)
        if it["done"] and not in_archive:
            raise FilesError("step_failed", 500, MISSING % it["kind"])
    return pairs, line


def restore(archive_id, *, op_id, ctx) -> dict:
    """把一份归档放回原处（incomplete 的也行），结果恒为停用态：停用标记不在就补上。原处任一项已有东西 → ``exists``，
    什么都不动。旧四只的令牌行最后追加回 bot-tokens.env 末尾。全部放回后删掉归档目录（删不干净只进 warnings）。

    成功 → ``{"bot_id", "archive_id", "launcher", "display_name", "provider_binding", "shell_meta", "warnings"}``，
    绑定与说明供调用方重新绑定、重新进群。失败 → ``FilesError``（已撤回）或 ``FilesStuck``。"""
    op_id, r = _op(op_id), ctx
    base, m = _restorable(archive_id, r)
    bot = m["bot_id"]
    pairs, line = _restore_plan(m, base, r)
    marker = os.path.join(r["disabled"], bot)
    _check(marker, r["disabled"], "停用标记", r["home"])
    m["restoring"] = {"op_id": op_id, "made": [], "marker": not _exists(marker)}
    for it, held, _src in pairs:            # 归档改名后、记 done 之前断掉的那一项，实际已在归档区
        if it["kind"] != "legacy_token" and _exists(held):
            it["done"] = True
    st = {"touched": False}
    try:
        _save(base, m)
        if m["restoring"]["marker"]:        # 先成停用态，再放回文件：断在中间也不会被拉起
            os.makedirs(r["disabled"], 0o700, exist_ok=True)
            _write_new(marker, b"")
            st["touched"] = True
        for it, held, src in reversed(pairs):
            if it["kind"] == "legacy_token" or not it["done"]:
                continue
            _make_parents(os.path.dirname(src), m, base)
            if _move(held, src):
                st["touched"] = True
        if line is not None:                # 最后一个改动：之后只剩清理，不再撤回
            _rewrite_tokens(r, _with(bot, line))
            st["touched"] = True
    except Exception as e:
        _abort(e, st["touched"], lambda: _undo_restore(base, m, r), op_id)
    warnings = _finish_restore(base, bot)
    _log.info("已恢复 bot=%s", bot)
    return {"bot_id": bot, "archive_id": archive_id, "launcher": m["launcher"], "display_name": m.get("display_name"),
            "provider_binding": m.get("provider_binding"), "shell_meta": m.get("shell_meta"), "warnings": warnings}


def _make_parents(folder: str, m: dict, base: str) -> None:
    """原处的父目录不在就建（0700），建之前先记进清单，撤回时删得掉。"""
    missing = []
    while folder and not _exists(folder):
        missing.append(folder)
        folder = os.path.dirname(folder)
    for d in reversed(missing):
        m["restoring"]["made"].append(d)
        _save(base, m)
        os.mkdir(d, 0o700)


def _finish_restore(base: str, bot: str) -> list:
    """全部放回后的清理：先删令牌副本，再删归档目录。删不干净不撤回（文件都已到位），只给一条提示。"""
    try:
        _unlink_quiet(os.path.join(base, LEGACY_COPY))
        _drop_dir(base)
    except OSError as e:
        _log.warning("恢复 %s 后清理归档目录出错：%s", bot, _reason(e))
        return ["已恢复，但归档目录没清干净（%s）；点“重新收拾”会再清一次" % _reason(e)]
    return []


def _undo_restore(base: str, m: dict, r: dict) -> None:
    """撤回恢复：把这次放回原处的（归档时移走过、现在原处有而归档区没有）移回归档区，删掉这次建的父目录与停用标记，
    清单回到恢复前。令牌行不用撤：追加是最后一个改动，它做成了就不会走到撤回。"""
    info = m.get("restoring") or {}
    for it, held, src in _pairs(m, base, r, ARCHIVE_KINDS):
        if it["kind"] != "legacy_token" and it["done"]:
            _move(src, held)
    for d in reversed(info.get("made") or []):
        if isinstance(d, str) and os.path.isdir(d) and not os.path.islink(d) and _allowed_parent(d, r):
            os.rmdir(d)
    if info.get("marker"):
        _unlink_quiet(os.path.join(r["disabled"], m["bot_id"]))
    m.pop("restoring", None)
    _save(base, m)


def _allowed_parent(path: str, r: dict) -> bool:
    """清单里记着"这次建的目录"：只认家目录、配置目录、停用目录之下的（防被改过的清单让我们删别处的空目录）。"""
    return any(path.startswith(r[k] + os.sep) for k in ("home", "config", "disabled", "channels"))


# ── 删除（3.5.4）────────────────────────────────────────────────────────────

def delete(bot_id, *, op_id, ctx, provider_binding=None, before_move=None) -> dict:
    """把这只 bot 的文件整份移进废纸篓（先 ``~/.Trash/``，写不进落 ``~/.claude/bot-trash/``），删除件
    ``<编号>-<UTC>-已删除/``。密钥文件（三份密钥与含供应商密钥的 settings 类文件）先挪进
    ``console-ops/<op_id>.pending-destroy/``，整份移完（提交点）后 unlink；
    旧四只在提交点之后从 bot-tokens.env 去掉它那一行，停用标记留作墓碑。

    成功 → ``{"trash": "system"|"own", "trash_path": "~/…", "path", "launcher", "wiped", "message", "unhandled"}``；
    ``wiped`` 为 False 表示已过提交点、令牌没抹干净（``message`` 写原因），``resolve`` 会再抹。
    ``before_move`` 同 ``archive``：删除件的清单落盘之后、挪第一份密钥之前调一次。
    提交点之前失败 → ``FilesError``（已撤回，密钥回到原处）或 ``FilesStuck``。"""
    r = ctx
    bot, op_id = _bot(bot_id, r), _op(op_id)
    binding = _binding_of(provider_binding)
    plan = _precheck(bot, "delete", r)
    pending = os.path.join(r["ops"], op_id + ".pending-destroy")
    items = [{"kind": "secret", "from": p, "to": os.path.join(pending, os.path.basename(p)), "done": False}
             for p in plan["secrets"]]
    items += [{"kind": kind, "from": path, "to": _item_name(i, path), "done": False}
              for i, (kind, path) in enumerate(plan["items"])]
    m = {"archive_id": None, "bot_id": bot, "person": bot, "display_name": plan["display_name"], "archived_at": _now(),
         "launcher": plan["launcher"], "complete": False, "op_id": op_id, "trash": None, "items": items,
         "provider_binding": binding, "shell_meta": None, "unhandled": plan["unhandled"]}
    base = _trash_dir(bot, r, m)
    st = {"touched": False}
    try:
        if before_move is not None:
            before_move()
        if plan["secrets"]:
            _ensure_dir(r["ops"], r, "操作记录目录")
            os.mkdir(pending, 0o700)
            os.chmod(pending, 0o700)
        os.mkdir(os.path.join(base, ITEMS), 0o700)
        for it in items:
            if _move(it["from"], it["to"] if it["kind"] == "secret" else os.path.join(base, it["to"])):
                st["touched"] = True
            it["done"] = True
            _save(base, m)
        m["complete"] = True                # 提交点：令牌以外的全部已在废纸篓
        _save(base, m)
    except Exception as e:
        _abort(e, st["touched"], lambda: _undo_delete(base, m, r), op_id)
    failure = _wipe(m, r)
    _log.info("已删除 bot=%s 条目=%d 令牌已抹=%s", bot, len(items), failure is None)
    return {"trash": m["trash"], "trash_path": _tilde(base, r["home"]), "path": base, "launcher": plan["launcher"],
            "wiped": failure is None, "message": WIPE_FAILED % failure if failure else "",
            "unhandled": plan["unhandled"]}


def _trash_dir(bot: str, r: dict, m: dict) -> str:
    """建删除件并写好清单：系统废纸篓建不了或写不进（EPERM/EACCES）就整份改用自有废纸篓（3.5.4 末条）。"""
    for trash in ("system", "own"):
        base = None
        try:
            _ensure_dir(r[trash], r, "废纸篓")
            base = _new_dir(r[trash], lambda s: "%s-%s%s" % (bot, s, DELETED_SUFFIX))
            m["trash"] = trash
            _save(base, m)
            return base
        except PermissionError:
            if base is not None:
                _drop_quietly(base)
        except OSError as e:
            if base is not None:
                _drop_quietly(base)
            raise FilesError("step_failed", 500, MOVE_FAILED % _reason(e)) from None
    raise FilesError("step_failed", 500, NO_TRASH)


def _drop_quietly(base: str) -> None:
    try:
        _drop_dir(base)
    except OSError as e:
        _log.warning("清理没用上的删除件出错：%s", _reason(e))


def _undo_delete(base: str, m: dict, r: dict) -> None:
    """撤回删除（提交点之前）：逆序放回原处，密钥最后回到频道目录，再删掉空的 pending 目录与删除件。"""
    for it, held, src in reversed(_pairs(m, base, r, DELETE_KINDS)):
        _move(held, src)
        it["done"] = False
    pending = os.path.join(r["ops"], m["op_id"] + ".pending-destroy")
    if os.path.isdir(pending) and not os.path.islink(pending):
        os.rmdir(pending)
    _drop_dir(base)


def _wipe(m: dict, r: dict):
    """提交点之后抹令牌：unlink pending 里的密钥、删掉 pending 目录，旧四只再从 bot-tokens.env 去掉它那一行
    （已经去掉了就不动）。全部做成 → None；否则 → 原因（只是类名或 errno 名），可以再做一次。"""
    pending = os.path.join(r["ops"], m["op_id"] + ".pending-destroy")
    try:
        for it in m["items"]:
            if it["kind"] == "secret" and isinstance(it.get("to"), str) and os.path.dirname(it["to"]) == pending \
                    and _exists(it["to"]):
                _unlink(it["to"])
        if os.path.isdir(pending) and not os.path.islink(pending):
            _fsync_dir(pending)
            os.rmdir(pending)
        if m["launcher"] == "legacy":
            _rewrite_tokens(r, _without(m["bot_id"], missing_ok=True))
            _purge_backups(m["bot_id"], r)
    except (OSError, FilesError, FilesStuck) as e:
        _log.warning("删除 %s 后抹令牌出错：%s", m["bot_id"], _reason(e))
        return _reason(e)
    return None


# ── 归档清单（3.3.2 的 claude 侧来源）──────────────────────────────────────

def list_archives(*, ctx) -> list:
    """``~/.claude/bot-archive/`` 下每份归档 ``{"archive_id", "bot_id", "display_name", "person", "archived_at", "status",
    "shell_meta"}``，按归档时间倒序。``status``：complete，或 incomplete（归档没走完，或恢复做了一半）。读不出的跳过。"""
    r = ctx
    out = []
    for base, m in _scan(r["archive"], r):
        name = os.path.basename(base)
        if m.get("archive_id") != name or not ARCHIVE_ID_RE.fullmatch(name):
            continue
        done = m.get("complete") is True and not m.get("restoring")
        out.append({"archive_id": name, "bot_id": m["bot_id"], "display_name": m.get("display_name") or m["bot_id"],
                    "person": m.get("person") or m["bot_id"], "archived_at": m.get("archived_at"),
                    "status": "complete" if done else "incomplete", "shell_meta": m.get("shell_meta")})
    out.sort(key=lambda a: (a["archived_at"] if isinstance(a["archived_at"], str) else "", a["archive_id"]),
             reverse=True)
    return out


def _scan(root: str, r: dict, *, prefix: str = "", suffix: str = "", strict: bool = False):
    """root 下名字合前后缀的真目录及其清单（没有清单或读不出的跳过）。root 不在 → 空；列不了（系统废纸篓可能不让列）
    → strict 时抛 FilesError（判定不出，不能当"没有"），否则当空。"""
    try:
        names = sorted(os.listdir(root))
    except FileNotFoundError:
        return
    except OSError as e:
        if strict:
            raise FilesError("step_failed", 500, "列不出 %s 里的东西（%s），判定不出做到哪一步" % (root, _reason(e)))
        return
    for name in names:
        base = os.path.join(root, name)
        if not (name.startswith(prefix) and name.endswith(suffix)) or os.path.islink(base) or not os.path.isdir(base):
            continue
        try:
            yield base, _read_manifest(base, r)
        except (FilesError, OSError) as e:
            if not isinstance(e, FileNotFoundError):
                _log.warning("清单读不出，跳过：%s", _reason(e))


# ── 收拾中断的操作（D11；"重新收拾"、重启后收拾、--recover 共用）───────────────

def _fields(op, r: dict) -> dict:
    """op 可以是任务记录（dict）、op_id 或归档编号（只给命令行用）。"""
    if isinstance(op, dict):
        f = {k: op.get(k) for k in ("op_id", "kind", "bot_id", "archive_id")}
        result = op.get("result")
        f["trash_path"] = result.get("trash_path") if isinstance(result, dict) else None
    elif isinstance(op, str) and OP_ID_RE.fullmatch(op):
        f = {"op_id": op, "kind": op.split("-")[1], "bot_id": None, "archive_id": None, "trash_path": None}
    elif isinstance(op, str) and ARCHIVE_ID_RE.fullmatch(op) and op.strip(".") != "":
        f = {"op_id": None, "kind": "archive_id", "bot_id": None, "archive_id": op, "trash_path": None}
    else:
        raise ValueError(BAD_OP_ID)
    if f["kind"] != "archive_id" and (not isinstance(f["op_id"], str) or OP_ID_RE.fullmatch(f["op_id"]) is None):
        raise ValueError(BAD_OP_ID)
    if f["bot_id"] is not None and not _safe_id(f["bot_id"], r):
        f["bot_id"] = None
    return f


def _recorded_trash(value, r: dict):
    """任务记录里记下的删除件位置（``result.trash_path``，家目录写成 ~）：只有落在系统废纸篓或自有废纸篓直接子级的真目录才算，
    被改过的记录不能把我们要找的地方指到别处。认不出 → None（退回两个根去扫）。"""
    if not isinstance(value, str) or not value.startswith("~/"):
        return None
    path = os.path.normpath(os.path.join(r["home"], value[2:]))
    if os.path.islink(path) or not os.path.isdir(path):
        return None
    roots = {os.path.realpath(r["system"]), os.path.realpath(r["own"])}
    if os.path.realpath(os.path.dirname(path)) not in roots:
        return None
    return path


def _locate(f: dict, r: dict):
    """按清单找这项操作留下的东西 → (base, 清单) 或 (None, None)。删除件两处废纸篓（``~/.Trash/`` 与
    ``~/.claude/bot-trash/``）都找：先按任务记录里记下的位置直接找；没有就两个根各自容错地扫，一处列不出不影响另一处，
    两处都列不出才抛（判定不出，3.5.4 B-I2）。"""
    op_id, bot = f["op_id"], f["bot_id"]
    if f["kind"] == "archive":
        for base, m in _scan(r["archive"], r, prefix=(bot + "-") if bot else ""):
            if m["op_id"] == op_id and m.get("archive_id") == os.path.basename(base):
                return base, m
    elif f["kind"] == "delete":
        known = _recorded_trash(f.get("trash_path"), r)
        if known is not None:
            try:
                m = _read_manifest(known, r)
            except (FilesError, OSError):
                m = None
            name = os.path.basename(known)
            if (m is not None and m["op_id"] == op_id and (not bot or m["bot_id"] == bot)
                    and name.startswith((bot + "-") if bot else "") and name.endswith(DELETED_SUFFIX)):
                return known, m
        failed = []
        for trash in ("system", "own"):
            try:
                for base, m in _scan(r[trash], r, prefix=(bot + "-") if bot else "", suffix=DELETED_SUFFIX,
                                     strict=True):
                    if m["op_id"] == op_id:
                        return base, m
            except FilesError as e:
                failed.append(e)
        if len(failed) == 2:
            raise failed[0]
    elif f["kind"] == "restore":
        aid = f["archive_id"]
        if isinstance(aid, str) and ARCHIVE_ID_RE.fullmatch(aid) and aid.strip(".") != "":
            base = os.path.join(r["archive"], aid)
            if os.path.isdir(base) and not os.path.islink(base):
                try:
                    return base, _read_manifest(base, r)
                except FileNotFoundError:
                    return None, None
            return None, None
        for base, m in _scan(r["archive"], r):
            if (m.get("restoring") or {}).get("op_id") == op_id:
                return base, m
    return None, None


def _restore_settled(m: dict, base: str, r: dict) -> bool:
    """这次恢复是否已越过提交点：归档时移走过的都已不在归档区，旧四只的令牌行也已追加回去。"""
    for it, held, _src in _pairs(m, base, r, ARCHIVE_KINDS):
        if it["kind"] == "legacy_token":
            if _exists(held) and not _hits(_read_tokens(r), m["bot_id"]):
                return False
        elif it["done"] and _exists(held):
            return False
    return True


def check(op, *, ctx):
    """只读复核本侧这一步做没做成：True（已过提交点）、False（本侧什么都没动）、None（停在中间或判定不出）。
    "停在中间"要先 ``resolve`` 收拾：它回退后再复核就是 False。"""
    try:
        r = ctx
        f = _fields(op, r)
        if f["kind"] not in ("archive", "delete", "restore"):
            return None
        base, m = _locate(f, r)
        if f["kind"] == "restore":
            if base is None:   # 归档目录没了：原处有它的配置就是恢复完了，否则是还没开始（或本来就没有这份归档）
                bot = f["bot_id"] or (f["archive_id"].rsplit("-", 1)[0] if isinstance(f["archive_id"], str) else None)
                return bool(bot) and _exists(os.path.join(r["config"], bot + ".yml"))
            if (m.get("restoring") or {}).get("op_id") != f["op_id"]:
                return False
            return True if _restore_settled(m, base, r) else None
        if base is None:
            pending = os.path.join(r["ops"], f["op_id"] + ".pending-destroy")
            return None if f["kind"] == "delete" and os.path.isdir(pending) and os.listdir(pending) else False
        return True if m.get("complete") is True else None
    except (FilesError, FilesStuck, OSError, ValueError):
        return None


def resolve(op, *, ctx) -> dict:
    """按本侧清单收拾一项中断的操作，做多少遍结果都一样：提交点之前的撤回到操作前，越过提交点的续完收尾。
    op 是任务记录（dict）或 op_id；命令行另可给归档编号。返回 ``{"state", "message", ...}``，state 取值：
    unchanged（本侧没动过，或已经收拾过）、rolled_back、committed（本侧已做完；删除另带 wiped、trash、trash_path）、
    needs_attention（收拾不动，message 写原因）、unsupported（新建、切换不归本模块）。归档、删除另带 ``kind``，认得出时带
    ``bot_id``；撤回了归档或删除时还带清单上记的 ``provider_binding``（清单随撤回删掉了，调用方凭它重新绑定）。
    调用方持着生命周期锁。"""
    r = ctx
    f = _fields(op, r)
    _sweep_token_tmp(r)
    if f["kind"] == "archive_id":       # 命令行给的归档编号：看清单上记的是哪一次操作
        try:
            m = _read_manifest(_archive_dir(f["archive_id"], r), r)
        except FileNotFoundError:
            return {"state": "needs_attention", "message": BAD_MANIFEST}
        except FilesError as e:
            return {"state": "needs_attention" if e.code != "unknown_archive" else "unchanged", "message": e.message}
        busy = (m.get("restoring") or {}).get("op_id")
        f = {"op_id": busy or m["op_id"], "kind": "restore" if busy else "archive", "bot_id": m["bot_id"],
             "archive_id": f["archive_id"]}
    if f["kind"] not in ("archive", "delete", "restore"):
        return {"state": "unsupported", "message": "这种操作不归 claude 文件内核收拾"}
    out = {"kind": f["kind"]}
    if f["bot_id"]:
        out["bot_id"] = f["bot_id"]
    try:
        base, m = _locate(f, r)
        if f["kind"] == "restore":
            return {**out, **_resolve_restore(f, base, m, r)}
        if base is None:
            return {**out, **_resolve_absent(f, r)}
        out["bot_id"] = m["bot_id"]
        if f["kind"] == "archive":
            if m.get("complete") is True:
                return {**out, "state": "committed", "message": "", "archive_id": m["archive_id"]}
            _undo_archive(base, m, r, None)
            return {**out, "state": "rolled_back", "message": "归档没做完，已放回原处",
                    "provider_binding": m.get("provider_binding")}
        if m.get("complete") is True:
            failure = _wipe(m, r)
            if failure:
                return {**out, "state": "needs_attention", "message": WIPE_FAILED % failure}
            return {**out, "state": "committed", "message": "", "wiped": True, "trash": m.get("trash"),
                    "trash_path": _tilde(base, r["home"]), "path": base}
        _undo_delete(base, m, r)
        return {**out, "state": "rolled_back", "message": "删除没做完，已放回原处",
                "provider_binding": m.get("provider_binding")}
    except (FilesError, FilesStuck, OSError) as e:
        _log.error("收拾 %s 出错：%s", f["op_id"], _reason(e))
        return {**out, "state": "needs_attention", "message": STUCK % (_reason(e), f["op_id"] or f["archive_id"])}


def _resolve_absent(f: dict, r: dict) -> dict:
    """找不到清单：建了目录、还没写成清单就断了（目录是空的或只有清单临时文件），删掉这种空壳；删除时密钥若已挪进
    pending 却没有清单，就不知道原处在哪，只能人工处理。"""
    pending = os.path.join(r["ops"], f["op_id"] + ".pending-destroy")
    if f["kind"] == "delete" and os.path.isdir(pending) and not os.path.islink(pending):
        if os.listdir(pending):
            return {"state": "needs_attention", "message": "密钥已挪进 ~/.claude/console-ops/%s.pending-destroy/，"
                                                           "但找不到删除件的清单，请人工放回频道目录" % f["op_id"]}
        os.rmdir(pending)
    roots = [r["archive"]] if f["kind"] == "archive" else [r["system"], r["own"]]
    suffix = "" if f["kind"] == "archive" else DELETED_SUFFIX
    for root in roots:
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            base = os.path.join(root, name)
            if (f["bot_id"] and name.startswith(f["bot_id"] + "-") and name.endswith(suffix) and os.path.isdir(base)
                    and not os.path.islink(base) and _shell_only(base)):
                _drop_dir(base)
    return {"state": "unchanged", "message": ""}


def _shell_only(base: str) -> bool:
    try:
        return all(n.startswith("." + MANIFEST + ".tmp-") or n == ITEMS and not os.listdir(os.path.join(base, n))
                   for n in os.listdir(base))
    except OSError:
        return False


def _resolve_restore(f: dict, base, m, r: dict) -> dict:
    if base is not None:                 # 写清单写到一半断掉留下的临时文件
        for name in os.listdir(base):
            if name.startswith("." + MANIFEST + ".tmp-"):
                os.unlink(os.path.join(base, name))
    if base is None or (m.get("restoring") or {}).get("op_id") != f["op_id"]:
        settled = check(f, ctx=r)
        return {"state": "committed" if settled else "unchanged", "message": ""}
    bot = {"bot_id": m["bot_id"]}
    if _restore_settled(m, base, r):
        warnings = _finish_restore(base, m["bot_id"])
        if warnings:
            return {**bot, "state": "needs_attention", "message": warnings[0]}
        return {**bot, "state": "committed", "message": "", "archive_id": f["archive_id"] or os.path.basename(base)}
    _undo_restore(base, m, r)
    return {**bot, "state": "rolled_back", "message": "恢复没做完，已放回归档区"}


def main(argv=None) -> int:
    """手工兜底：``python3 -m adapters.claude_files --recover <op_id|archive_id>``。先拿生命周期锁（管理台正在做操作就
    不动），再调 claude 适配器的 ``resolve``：与"重新收拾"同一个函数，文件之外的启动项、供应商绑定、端口表也一并收拾。
    适配器经 ``registry`` 取（与壳同一个入口，本模块不导入旧栈）。只打印一行结论（state、message），不打印任何文件内容。"""
    import argparse

    from . import registry
    parser = argparse.ArgumentParser(prog="python3 -m adapters.claude_files",
                                     description="按清单收拾中断的归档、恢复、删除")
    parser.add_argument("--recover", required=True, metavar="OP_ID|ARCHIVE_ID")
    args = parser.parse_args(argv)
    value = args.recover
    if not (OP_ID_RE.fullmatch(value) or ARCHIVE_ID_RE.fullmatch(value) and value.strip(".") != ""):
        print(json.dumps({"state": "invalid", "message": "要给操作编号或归档编号"}, ensure_ascii=False))
        return 2
    import platform_lock

    lock_path = os.path.join(os.path.expanduser("~"), ".claude", "console-ops", ".recover.lock")
    try:
        os.makedirs(os.path.dirname(lock_path), mode=0o700, exist_ok=True)
        with platform_lock.file_lock(lock_path, timeout=0.2):
            out = registry.adapter("claude").resolve(value)
    except platform_lock.LockBusy:
        print(json.dumps({"state": "busy", "message": "管理台正在做别的操作"}, ensure_ascii=False))
        return 1
    print(json.dumps({"state": out["state"], "message": out.get("message", "")}, ensure_ascii=False))
    return 0 if out["state"] in ("unchanged", "rolled_back", "committed") else 1


if __name__ == "__main__":
    sys.exit(main())
