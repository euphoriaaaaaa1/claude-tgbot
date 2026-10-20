"""每只 bot 各用各的 provider —— 配置内核（无 Flask 依赖）。

单一事实源 ``configs/providers.json``；生效落点 = ``<bot_dir>/.claude/settings.json`` 的
``env``（7 键恒在 + 白名单扩展键按固定序追加；INTERFACE-tg-provider-v2 §4）与顶层
``effortLevel``（思考强度，批2）。本模块是**唯一**的 settings.json 写入点，负责备份/回滚/
路径校验/按 bot 串行（INTERFACE §1.1、§3.2）。
"""
import contextlib
import copy
import hashlib
import itertools
import json
import os
import secrets
import shutil
import sys
import time
from urllib.parse import urlparse

import yaml

import bots_registry as bot_registry
import config_loader
import retry_backoff
from platform_lock import LockBusy, LockError, file_lock

# ── 测试接缝（INTERFACE §1.1 集中表）────────────────────────────────────────


class SeamsNotConfigured(RuntimeError):
    """接缝类错误的专用类型（rev8）：导入期的成组抛错与只读态的写入口抛错都用它。

    是 ``RuntimeError`` 的子类（既有用法与文案不变）；HTTP 层**只捕这一类**转 503，
    绝不 catch 宽的 RuntimeError，以免把别的 bug 吞成 503。
    """


# 成组判据的**触发集只取本轮新增的两个接缝**（rev7）：真正决定"写到哪个目录"的是
# CLAUDEBOT_CHANNELS_ROOT（写盘前缀校验基准）与 CLAUDEBOT_PROVIDER_PATH（清单落点），
# 所以设了它们之一却没设全 → 导入期 fail-closed。
# 既有的 CLAUDEBOT_CONFIG_DIR（config_loader.py:33-34，本轮之前就有）单独设是合法用法
# （本仓既有 4 处：desync/bot_stop/birthday_grade/config_loader_security）→ 导入放行，
# 但 provider_config 进**只读模式**：读入口正常、一切写入口抛同一条 SeamsNotConfigured。
# 理由：那种环境下 yml 可被重定向到真 yml 副本、bot_channel_path 仍指真目录，
# CHANNELS_ROOT 前缀校验会放过它 → "读到真 yml 却写进真目录"必须在写口挡住。
_SEAM_KEYS = ("CLAUDEBOT_CONFIG_DIR", "CLAUDEBOT_CHANNELS_ROOT", "CLAUDEBOT_PROVIDER_PATH")
_NEW_SEAM_KEYS = ("CLAUDEBOT_CHANNELS_ROOT", "CLAUDEBOT_PROVIDER_PATH")
_SEAM_ERROR = "测试接缝必须成组设置"
_SEAMS_SET = frozenset(k for k in _SEAM_KEYS if os.environ.get(k))
if (_SEAMS_SET & set(_NEW_SEAM_KEYS)) and len(_SEAMS_SET) != len(_SEAM_KEYS):
    raise SeamsNotConfigured(_SEAM_ERROR)

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

PROVIDERS_PATH: str = os.environ.get("CLAUDEBOT_PROVIDER_PATH") or os.path.join(
    _REPO_ROOT, "configs", "providers.json")
CHANNELS_ROOT: str = os.environ.get("CLAUDEBOT_CHANNELS_ROOT") or os.path.expanduser(
    "~/.claude/channels")
GLOBAL_SETTINGS_PATH: str = os.path.expanduser("~/.claude/settings.json")

def _channels_dir() -> str:
    """channels 根目录；每次现读接缝，保证测试重定向与运行期改 env 都生效。"""
    return os.environ.get("CLAUDEBOT_CHANNELS_ROOT") or os.path.expanduser("~/.claude/channels")


def _derive_bot_ports() -> dict:
    """现读一张新的端口表：configs/*.yml 的 dispatcher_port，经登记表 bot_registry 读（D5）。
    yml 损坏由 registry 扫描静默跳过（既有语义）；缺 dispatcher_port 或不是合法端口的 yml 不进表。"""
    return {e["bot_id"]: e["dispatcher_port"] for e in bot_registry.entries() if e["dispatcher_port"] is not None}


# 与 moments/web.py、self-initiate.sh、director.py 一致；唯一定义处（J2）。
# C-4 起改为派生：新 bot 建 configs/<id>.yml（带 dispatcher_port）即自动进表。
# 第二批起由登记表派生（D5 逐处处理第 13 行）：它就是 bot_registry 的那一张表，bot_registry.reload() 在原处刷新，
# moments.web 里的 _BOT_PORTS、_PC_BOT_PORTS 指向同一个 dict，一起看到新内容。导入时按当前配置刷新一次（同既有时机）。
BOT_PORTS: dict[str, str] = bot_registry.reload()

ALLOWED_ENV_KEYS: tuple[str, ...] = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
)
# 白名单扩展键（批2；INTERFACE-tg-provider-v2 §4）：7 键恒在且保持原序，扩展键按本元组
# 固定顺序**条件追加**（无值不落键）。这是既有「bot settings 不写任何 CLAUDE_CODE_*」原则的
# **契约级例外**：显式允许表只放行下面这一个键，其余前缀禁令不变。
EXT_CONTEXT_ENV_KEY = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"
_EXT_ENV_KEYS: tuple[str, ...] = (EXT_CONTEXT_ENV_KEY,)

# 思考强度（批2；INTERFACE §3/§4）：落地键 = settings **顶层**``effortLevel``（不进 env）；
# 受理词表实测合法值（本侧无 off/minimal）；写入前白名单校验，非法值绝不落盘。
EFFORT_KEY = "effortLevel"
EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

# local 层出现这些键 = 会盖掉面板写入（local > project，M0 实测）
PROVIDER_KEYS: tuple[str, ...] = (
    "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL")

_BACKUPS_KEEP = 2   # 用户 2026-10-15：备份一律只留 2 份

# settings.json 的跨进程锁（PLAN-bot-features D3；INTERFACE 3.8.3）：<频道目录>/.claude/settings.json.lock。
# 壳、命令行、开关同步都经 _write_settings；等满 SETTINGS_LOCK_WAIT_SEC 拿不到就回 SETTINGS_BUSY_MESSAGE，
# 调用方按这句区分"锁被占"与"写失败"（后者仍是原来那几句）。
SETTINGS_LOCK_WAIT_SEC = 3.0
SETTINGS_LOCK_RETRY_SEC = 0.05
SETTINGS_BUSY_MESSAGE = "别的操作正在改这只 bot 的设置，请稍后再试"


def seams_redirected() -> bool:
    """三个接缝是否全被重定向（完整测试态）。生产（一个都不设）→ False。

    **不参与任何闸**（rev8 改名，原名 `seams_complete` 的 `complete` 在两种语境下含义相反）：
    它只表达"测试态是否配齐"，与"该不该起收敛器"的判据（`seams_read_only()`）语义相反。
    """
    return len(_SEAMS_SET) == len(_SEAM_KEYS)


def seams_read_only() -> bool:
    """只设了既有 ``CLAUDEBOT_CONFIG_DIR`` → True：读放行、写入口抛 SeamsNotConfigured、收敛器不启动。

    也即"接缝不全"这一危险态——注意**生产态（一个都不设）不是**接缝不全，
    它的路径就是生产路径，读写与后台收敛都应当正常工作。**闸只认这个函数。**
    """
    return _SEAMS_SET == {"CLAUDEBOT_CONFIG_DIR"}


class SafetyGateTripped(RuntimeError):
    """测试态安全闸（9.4/R7）：写端点回 500 ``测试接缝必须成组设置``，零写入。"""


def _real_default_paths() -> tuple[str, str]:
    """「真实默认位置」（唯一定义在 provider_proxy.real_default_paths）。单测替换本函数来测请求侧闸。"""
    import provider_proxy
    return provider_proxy.real_default_paths()


def _has_proxy(data) -> bool:
    return isinstance(data, dict) and any(
        isinstance(p, dict) and p.get("proxy") is True for p in data.get("providers") or [])


def safety_gate_tripped(data: dict | None = None) -> bool:
    """请求侧安全闸，只对清单支返回 True（500 零写入）。触及判定：现清单或待写清单有 proxy=true，或运行目录已有 config.yaml；
    清单读不出来时按「会触及」保守处理。"""
    import provider_proxy

    def touches() -> bool:
        if _has_proxy(data) or os.path.exists(provider_proxy.config_path()):
            return True
        try:
            return _has_proxy(load_providers())
        except ProvidersCorrupt:
            return True
    # 勘误 8：只有清单支 500 零写入；运行目录支由 provider_proxy.sync 跳过代理重建并记 last_error
    return provider_proxy.gate_branch(provider_proxy.gate_env_active(), PROVIDERS_PATH, provider_proxy.proxy_dir(),
                                      touches, _real_default_paths()) == provider_proxy.GATE_MANIFEST


def _require_writable(data: dict | None = None) -> None:
    """写闸：只读模式（接缝不成组）→ 503；测试态安全闸 → 500。都排在汇口确保 key 之前。"""
    if seams_read_only():
        raise SeamsNotConfigured(_SEAM_ERROR)
    if safety_gate_tripped(data):
        raise SafetyGateTripped(_SEAM_ERROR)


class ProvidersCorrupt(Exception):
    """providers.json 解析失败（写操作必须拒写且不改文件）。"""


def local_conflict_message(bot_id: str, keys: list) -> str:
    """local 冲突的**函数层**文案（INTERFACE §1.1 末条）——唯一构造处。"""
    return ("%s 的 settings.local.json 里存在 provider 键（local 层优先于项目层），"
            "会覆盖面板写入，请先手工清理: %s" % (bot_id, ", ".join(keys)))


class LocalConflict(RuntimeError):
    """settings.local.json 命中 provider 键（local > project）→ 写入口拒绝（R16）。

    带 ``bot_id`` / ``keys`` 两个字段，供 HTTP 层**按类型**取键名列表映射结构化 409，
    不必去匹配错误文案——文案一改就会静默降级成 400、连 UI 要用的 keys 一起丢。
    函数层（``set_bot_provider`` / ``clear_bot_provider``）仍按 INTERFACE 契约把它转成
    ``(值, 文案)`` 返回，故对这个异常不改变公开返回值形状。
    """

    def __init__(self, bot_id: str, keys: list):
        self.bot_id = bot_id
        self.keys = list(keys)
        super().__init__(local_conflict_message(bot_id, self.keys))


# ── 锁（Y5）：providers.json 一切读改写一把；同一 bot 的「写+kill+spawn」一把 ──
import threading  # noqa: E402  （放在常量之后，保持文件头可读）

_PROVIDERS_LOCK = threading.Lock()
_BOT_LOCKS: dict[str, threading.Lock] = {}
_BOT_LOCKS_GUARD = threading.Lock()
_id_seq = itertools.count()
_LAST_SNAPSHOT: dict[str, dict] = {}


def bot_lock(bot_id: str) -> threading.Lock:
    with _BOT_LOCKS_GUARD:
        lock = _BOT_LOCKS.get(bot_id)
        if lock is None:
            lock = _BOT_LOCKS[bot_id] = threading.Lock()
        return lock


# ── providers.json 读写 ────────────────────────────────────────────────────

def load_providers() -> dict:
    """读 providers.json。缺失/空 → 空结构；解析失败 → ProvidersCorrupt。

    **返回值含明文 api_key**：仅供本模块内部与 set_bot_provider 的 provider 入参使用，
    任何 Flask 视图不得直接序列化它（HTTP 出口一律用 upsert_provider 的脱敏形状）。
    """
    try:
        with open(PROVIDERS_PATH, encoding="utf-8") as f:
            raw = f.read()
    except FileNotFoundError:
        return {"providers": [], "bindings": {}}
    if not raw.strip():
        return {"providers": [], "bindings": {}}
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ProvidersCorrupt("providers.json 解析失败") from e
    if not isinstance(data, dict):
        raise ProvidersCorrupt("providers.json 解析失败")
    data.setdefault("providers", [])
    data.setdefault("bindings", {})
    if not isinstance(data["providers"], list):
        data["providers"] = []
    if not isinstance(data["bindings"], dict):
        data["bindings"] = {}
    return data


def _load_providers_safe() -> dict:
    """读侧容错：坏文件当空（effective/list_bots_status 不抛异常，§9.3）。"""
    try:
        return load_providers()
    except ProvidersCorrupt:
        return {"providers": [], "bindings": {}}


def save_providers(data: dict) -> None:
    """原子落盘 0600：同目录 tmp + fsync + os.replace。"""
    _require_writable()
    with _manifest_lock():
        _commit_locked(data, ensure_key=False)   # 通用落盘 API 不是请求汇口，不代生成 key


_LOCK_STATE = threading.local()


class ProvidersLockTimeout(RuntimeError):
    """跨进程文件锁等不到（10 秒）：回执文案「别的操作正在改供应商，请稍后再试」。

    单列一类（不是裸 RuntimeError）：HTTP 层**按类型**接住转 409 文案，面板的 500 兜底
    与其它异常语义一字不变。
    """


_LOCK_RETRY_SEC = 0.1        # 等锁重试间隔
_LOCK_WAIT_SEC = 10.0        # 等锁上限
_LOCK_STALE_MS = 120_000     # 残留锁年龄上限（兜底：持锁 pid 被复用时靠它）


def _providers_lock_path() -> str:
    """跨进程锁文件：与 providers.json **同目录**（测试接缝指到 tmp 时自动跟着走）。"""
    return os.path.join(os.path.dirname(PROVIDERS_PATH) or ".", "providers.lock")


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:   # 进程在，只是不归我们
        return True
    except OSError:
        return False
    return True


def _read_lock(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _try_create_lock(path: str, content: str) -> bool:
    """「不存在才创建」；已存在 → False（不碰别人的那份）。"""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError:
        _cleanup(path)
        raise
    return True


def _clear_stale_lock(path: str) -> bool:
    """残留锁才清掉。判据：持锁进程已不在（主），或锁龄 > 120 秒（兜底，pid 被复用时靠它）。
    清法：先改名成唯一名字，确认还是刚才看到的那份再删——两个进程同时清时，不会把对方刚
    抢到的新锁删掉（改名拿到的已是别人的新锁就放回去；link 不覆盖已有文件）。"""
    seen = _read_lock(path)
    if seen is None:
        return False
    try:
        info = json.loads(seen)
    except ValueError:
        info = None
    at = info.get("at") if isinstance(info, dict) else None
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        try:   # 内容还没写进去（刚创建）或写坏了：按文件修改时间算年龄
            at = os.stat(path).st_mtime * 1000
        except OSError:
            return False
    pid = info.get("pid") if isinstance(info, dict) else None
    known = isinstance(pid, int) and not isinstance(pid, bool)
    # 年轻锁（<120s）只在"确认持有者已死"时才敢清；pid 读不到（O_EXCL 创建与写入之间的
    # 微秒窗口）一律不碰——否则会把刚创建的活锁误清，两边同时"持锁"。
    if time.time() * 1000 - at <= _LOCK_STALE_MS and (not known or _pid_alive(pid)):
        return False
    aside = "%s.stale-%d-%s" % (path, os.getpid(), secrets.token_hex(4))
    try:
        os.rename(path, aside)
    except OSError:
        return False
    if _read_lock(aside) == seen:
        _cleanup(aside)
        return True
    try:   # 改名时拿走的已是别人的新锁：放回锁位置（硬链接不会覆盖已有文件），丢掉这份
        os.link(aside, path)
    except OSError:
        pass
    _cleanup(aside)
    return False


@contextlib.contextmanager
def _providers_file_lock():
    """providers.lock 跨进程互斥：每 100ms 重试，最多等 ``_LOCK_WAIT_SEC``；
    等不到抛 ProvidersLockTimeout。**锁内只做清单读写**（网络拉取一律在锁外）。
    结束（含抛错）只删自己那份——内容对不上说明锁已被别人换过，不动。"""
    _require_writable()   # 写闸先过：拒绝时必须**零副作用**（不得先建目录锁文件，见 9.4 零写入断言）
    path = _providers_lock_path()
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    deadline = time.monotonic() + _LOCK_WAIT_SEC
    mine = ""
    while True:
        mine = json.dumps({"pid": os.getpid(), "at": int(time.time() * 1000)})
        if _try_create_lock(path, mine):
            break
        if _clear_stale_lock(path):
            continue
        if time.monotonic() >= deadline:
            raise ProvidersLockTimeout("别的操作正在改供应商，请稍后再试")
        time.sleep(_LOCK_RETRY_SEC)
    try:
        yield
    finally:
        if _read_lock(path) == mine:
            _cleanup(path)


@contextlib.contextmanager
def _manifest_lock():
    """取清单锁并在本线程记账。锁序（R6/§9.2）：先清单锁，**释放后**才进配置锁；
    持有配置锁时只读清单、不写清单（provider_proxy 从不 import 本模块，结构上保证）。

    批1-A：进程内 threading.Lock 之外再套一把**跨进程文件锁**（``providers.lock``）——
    TG（CLI 子进程）与面板是两个进程，只靠 threading.Lock 会互相盖写（丢更新）。"""
    with _PROVIDERS_LOCK:
        _LOCK_STATE.held = True
        try:
            with _providers_file_lock():
                yield
        finally:
            _LOCK_STATE.held = False


def _commit_locked(data: dict, ensure_key: bool = True) -> None:
    """清单唯一落盘汇口（调用方须已持清单锁）。第 0 步：确保顶层 ``proxy.instance_key`` 非空——
    已有则原样保留（I2），缺则新生成，不论有无 proxy provider。
    失败态记账（note_error/record_failure/rollback 等）传 ``ensure_key=False``：4xx/500 路径不得生成 key。"""
    _require_writable(data)   # 兜底：任何落盘都先过只读闸与安全闸（只读优先，保持 503 语义）
    if ensure_key:
        meta = data.get("proxy")
        if not isinstance(meta, dict):
            meta = data["proxy"] = {}
        key = meta.get("instance_key")
        if not (isinstance(key, str) and key):
            meta["instance_key"] = secrets.token_urlsafe(32)
        meta["port"] = _current_proxy_port()   # 与 config.yaml 端口对齐（原 ensure_proxy_meta 语义）
    _write_providers_locked(data)


def _current_proxy_port() -> int:
    import provider_proxy   # 调用期导入：单向依赖
    return provider_proxy.proxy_port()


def ensure_instance_key() -> str:
    """写路径（显式 sync / sync_all 等本身不改清单的 2xx 请求）用：缺 key 才落盘，返回 key。"""
    _require_writable()
    with _manifest_lock():
        data = load_providers()
        key = (data.get("proxy") or {}).get("instance_key") if isinstance(data.get("proxy"), dict) else None
        if not (isinstance(key, str) and key) or data["proxy"].get("port") != _current_proxy_port():
            _commit_locked(data)
            key = data["proxy"]["instance_key"]
        return key


def _write_providers_locked(data: dict) -> None:
    _require_writable()  # 所有 providers.json 写盘的唯一出口，只读模式在此挡住
    d = os.path.dirname(PROVIDERS_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d or ".", ".%s.tmp-%d-%d" % (
        os.path.basename(PROVIDERS_PATH), os.getpid(), int(time.time() * 1000)))
    fd = _open_private(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, PROVIDERS_PATH)
    except BaseException:
        # 不只 OSError：序列化到一半抛出的任何异常（如写不成 UTF-8 的串）都会留下清单的
        # 前半截，里面通常有明文密钥，且文件名带毫秒戳、可无限累积——一律清掉再原样抛出。
        _cleanup(tmp)
        raise


# 旧判定键（provider 协议改 provider 级前的历史键）：任意一次 PUT 会从条目删除（INTERFACE §1/§2.5）
_LEGACY_KEYS = ("protocols", "unavailable_models")


def _write_private_copy(src_path: str, dst_path: str) -> None:
    """把 ``src_path`` 逐字节复制到 ``dst_path``，**目标从创建那一刻起就是 0600**。

    SECURITY-REVIEW-provider-protocol-level-r2 重要-1：``shutil.copy2`` 先用默认 umask(022)
    建出 0644 的目标 inode 并往里写明文 key，之后才 ``copystat`` 回源模式；那个窗口里同机
    其他用户即可读到全部 api_key，且若拷贝中途抛错，0644 的半截文件会永久留下。改走
    ``_open_private``（``O_CREAT|O_EXCL|0600``）后"任何字节落盘之前模式已是 0600"，与 umask
    无关；失败时清掉半截目标。
    """
    with open(src_path, "rb") as src:
        fd = _open_private(dst_path)
        try:
            with os.fdopen(fd, "wb") as dst:
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
        except OSError:
            _cleanup(dst_path)
            raise


def _pre_migration_snapshot_path() -> str:
    """一次性迁移快照的固定路径。**故意不带 ``.bak-<纯数字>`` 形**：``_prune_backups`` 只裁
    ``<base>.bak-<纯数字>`` 的备份，故该固定名天然不参与裁剪（而非靠字典序侥幸留在最后）。"""
    return PROVIDERS_PATH + ".pre-protocol-migration.bak"


def _snapshot_pre_migration() -> None:
    """首次触发迁移备份时落一份固定名快照（0600），保留"旧键齐全"的完整回退点。

    SECURITY-REVIEW-provider-protocol-level-r2 建议-2：带时间戳的 ``.bak-<ts>`` 会被
    ``_prune_backups`` 裁到 ``_BACKUPS_KEEP`` 份；第 3 个含旧键的 provider 被 PUT 时，最早
    那份"旧键齐全"的快照已被裁掉。此快照只在此刻（尚无任何迁移发生）生成一次，之后永不
    触碰，故任何时刻都能用它整份回退。已存在则跳过（幂等）。
    """
    path = _pre_migration_snapshot_path()
    if os.path.exists(path):
        return
    _write_private_copy(PROVIDERS_PATH, path)


def _backup_providers_before_migration() -> None:
    """迁移（删旧键 ``protocols``/``unavailable_models``）前对 providers.json 落一份 0600 备份。

    SECURITY-REPORT-provider-protocol-level 重要-1：旧键一旦被 PUT 删掉，磁盘上就没有文件级
    回退点。备份命名 ``providers.json.bak-<ts>``、权限 **创建即 0600**（不是事后 chmod，见
    ``_write_private_copy``）、与 providers.json 同目录，并复用 ``_prune_backups`` 保留最近
    ``_BACKUPS_KEEP`` 份。另在首次迁移前额外落一份固定名一次性快照（``_snapshot_pre_migration``，
    不参与裁剪）。

    **机会性**：与 ``_write_settings`` 同口径，备份失败不得阻断正常写流程，仅留 stderr 线索。
    内容为原文件**逐字节副本**，不截断、不改写任何字段。
    """
    if not os.path.exists(PROVIDERS_PATH):
        return
    try:
        _snapshot_pre_migration()
        backup_path = "%s.bak-%d" % (PROVIDERS_PATH, int(time.time()))
        _write_private_copy(PROVIDERS_PATH, backup_path)
        _prune_backups(PROVIDERS_PATH)
    except OSError as e:
        print("providers.json 备份失败，迁移继续: %s" % e, file=sys.stderr)


# ── provider 清单 ──────────────────────────────────────────────────────────

def _key_fp(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def _mask(value: str) -> str:
    if not value:
        return ""
    return ("****" + value[-4:]) if len(value) >= 4 else "****"


def _public_entry(entry: dict) -> dict:
    """脱敏形状：绝不含 api_key，只有 key_masked / has_key。

    ``default_model`` 是本字段**唯一的对外出口**，而契约要求它「类型恒为 str、从不 null」
    （旧 INTERFACE §1.2）。故这里按**类型**归一化，不用 ``or ""`` —— 后者只兜假值，
    清单被手工改成 ``123``／``["m1"]``／``{"id":"m1"}`` 时会把原值直接透给前端。

    本轮键集（INTERFACE-provider-protocol-level §1，恰 13 键、顺序即此）：**移除**
    ``protocols`` / ``unavailable_models``，**新增** provider 级 ``protocol``（可能是
    读取时派生的值，见 ``entry_protocol``）。``proxy`` 只认 ``is True``（缺省 false）。
    """
    key = entry.get("api_key") or ""
    default_model = entry.get("default_model")
    return {
        "id": entry.get("id"),
        "name": entry.get("name"),
        "base_url": entry.get("base_url"),
        "models": entry.get("models") or [],
        "default_model": default_model if isinstance(default_model, str) else "",
        "key_masked": _mask(key) if key else "",
        "has_key": bool(key),
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
        "proxy": entry.get("proxy") is True,
        "protocol": entry_protocol(entry),
        "anthropic_base_url": (entry.get("anthropic_base_url")
                               if isinstance(entry.get("anthropic_base_url"), str) else ""),
        "openai_base_url": (entry.get("openai_base_url")
                            if isinstance(entry.get("openai_base_url"), str) else ""),
    }


_PROTOCOLS = ("anthropic", "openai")


def entry_protocol(entry: dict) -> str:
    """provider 级**有效协议**（INTERFACE-provider-protocol-level §1 / §2.5）。

    优先级：条目显式 ``protocol`` 键 ∈ {anthropic, openai} → 原样返回；否则**只读派生**：
    仅当该 provider 的**每个**模型都已在旧 ``protocols`` 中判定为 anthropic/openai
    且取值**全部一致**时返回该唯一值，否则返回 ``""``（未选）。

    **绝不**把 ``unknown``／未判定模型按某个协议一并发出——那会造成静默误路由。
    派生值只读：磁盘上不写该键，直到用户下一次 PUT（见 ``upsert_provider``）才固化。
    """
    raw = entry.get("protocol")
    if raw in _PROTOCOLS:
        return raw
    protocols = entry.get("protocols")
    if not isinstance(protocols, dict):
        return ""
    seen = set()
    for mid in _model_ids(entry):
        value = protocols.get(mid)
        if value not in _PROTOCOLS:
            return ""
        seen.add(value)
    if len(seen) != 1:
        return ""
    return seen.pop()


def _find(data: dict, provider_id: str) -> dict | None:
    return next((p for p in data.get("providers", []) if p.get("id") == provider_id), None)


# upsert_provider 用它区分「body 里没有这个键」与「提供了什么值」——同一件事只留一个判据，
# 免得「是否提供」既写成 `x is None` 又写成 `"x" in body`，任一处改动就静默漂移
# （而 default_model 的漂移方向恰好会撞上 §1.4「"" ⇒ 显式清空」）。
_UNSET: object = object()


def new_provider_id(data: dict) -> str:
    """生成不重复的 provider id（不依赖 data 是否已落盘）。"""
    taken = {p.get("id") for p in (data.get("providers") or [])}
    while True:
        cand = "p_%d_%d" % (int(time.time()), next(_id_seq))
        if cand not in taken:
            return cand


def _valid_base_url(v) -> bool:
    """http(s) 且含主机名、无 userinfo —— **唯一判据**（provider_routes 的拉模型复用同一个）。

    只写 ``^https?://`` 会放过 ``http://`` 这种无主机名地址：provider 能存能绑，把
    ``ANTHROPIC_BASE_URL="http://"`` 写进 bot 的 settings.json，worker 重启后每次调用都失败，
    而面板仍报 applied/in_sync。与 ``urlparse().hostname`` 的接受度必须一致。
    """
    if not isinstance(v, str) or not (0 < len(v) <= 500) or "@" in v:
        return False
    try:
        u = urlparse(v)
        return u.scheme in ("http", "https") and bool(u.hostname)
    except ValueError:      # 方括号不成对（如 ``http://[::1``）时 urlparse 直接抛：按不合法处理
        return False


# 名字规则（批1-D；INTERFACE §3「校验分工」）。保留字表——这些词在 TG 命令面是子命令
# （/provider add|refresh|remove|list|global|reset|default|help），当供应商名会让命令歧义。
RESERVED_PROVIDER_NAMES = frozenset(
    ("add", "refresh", "remove", "list", "global", "reset", "default", "help"))


def _valid_name(v) -> bool:
    """名字规则的**唯一判据**（只有新建/改名路径调用；存量条目读取不判罪）。

    1–32 字符、无空白/控制字符、不以 ``-``/``_`` 开头、允许非 ASCII（"小米"合法）；
    保留字（``RESERVED_PROVIDER_NAMES``）拒绝，不分大小写（命令面孔一律不分大小写）。
    """
    if not isinstance(v, str) or not (1 <= len(v) <= 32):
        return False
    for ch in v:
        if ch.isspace() or ord(ch) < 32 or 0x7F <= ord(ch) <= 0x9F:
            return False
    if v[0] in "-_":
        return False
    return v.lower() not in RESERVED_PROVIDER_NAMES


def _norm_default_model(raw) -> str | None:
    """``default_model`` 的**唯一**判据：合法 → strip 后的值；非法 → None。

    合法 = str 且（空串，或 strip 后非空且长度 ≤200）。故 ``""`` 合法（= 显式清空），
    而只含空白的 ``"   "`` **非法**（含糊输入不猜）；长度按 **strip 后**的值判定，
    所以 ``" " + "x"*200`` 合法。上限与 R7 的 ``model``（1..200）同为 200。
    """
    if not isinstance(raw, str):
        return None
    if raw and not raw.strip():
        return None
    value = raw.strip()
    return value if len(value) <= 200 else None


def _norm_models(raw):
    """输入 list[str] 或 list[{"id","label"}] → list[{"id","label"}]；非法 → None。"""
    if not isinstance(raw, list) or len(raw) > 500:
        return None
    out = []
    for m in raw:
        if isinstance(m, str):
            if len(m) > 500:
                return None
            out.append({"id": m, "label": m})
        elif isinstance(m, dict) and isinstance(m.get("id"), str):
            if len(m["id"]) > 500:
                return None
            out.append({"id": m["id"], "label": m.get("label") or m["id"]})
        else:
            return None
    return out


# 准入判定的三条 400 文案（INTERFACE-provider-protocol-level §2；唯一构造处，测试逐字比较）
_ADM_PROTOCOL = "请先为该 provider 选择协议（anthropic 或 openai）"
_ADM_NOMODEL = "该 provider 还没有模型，请先拉取模型"
_ADM_ADDR = "代理上游地址无法自动推导，请填写 anthropic_base_url"
_ADM_NAME_DUP = "客户端名或裸名在多个代理 provider 间重复，请先关闭其中一个"


def _model_ids(entry: dict) -> list:
    """条目的模型 id 列表（对本地可手改的 JSON 容错：元素可能是 str 或 {"id":...}）。"""
    out = []
    for m in entry.get("models") or []:
        mid = m.get("id") if isinstance(m, dict) else (m if isinstance(m, str) else None)
        if isinstance(mid, str) and mid:
            out.append(mid)
    return out


def _registered_names(entry: dict) -> set:
    """该 provider 在实例里会注册的名字集合 N_p（INTERFACE §1.2 重名门）。

    ``N_p = { <pid>/<m> } ∪ { m }``——**两项都取全部模型**（INTERFACE §1.2 / §1.5；
    V0-11 实测：``claude-api-key`` 与 ``openai-compatibility`` 两个块**都**注册裸名、
    与模型属哪个桶无关）。原 R7「anthropic 块只注册前缀名」系误判（当时两块用同一模型名，
    路由到先注册的块，掩盖真实行为）。

    **冲突已解决（2026-09-27，用户授权）**：本轮锁定套件里 `test_客户端名撞客户端名_永不冲突`
    等用例原以「anthropic 桶只注册前缀名」为前提（= R7 旧结论，已被 V0-11 推翻），与本并集判据
    抵触。用户授权按 V0-11 修正那些用例的期望（**保留本并集判据**——裸名确实会撞且实例
    **静默路由到先注册的那块**，见 V0-5）。授权与审计记录见
    `.devflow/evidence-provider-openai-compat/LOCKED-TEST-AMENDMENT.md`。
    """
    pid = entry.get("id")
    names = set()
    for mid in _model_ids(entry):
        if isinstance(pid, str) and pid:
            names.add(pid + "/" + mid)
        names.add(mid)
    return names


def _proxy_admission_error(entry: dict, data: dict) -> str | None:
    """``proxy=true`` 的准入判定（INTERFACE-provider-protocol-level §2）：通过 → ``None``，否则错误文案。

    门顺序**固定**：未选协议 → 无模型 → 地址问题 → 重名。更具体的诊断优先——
    未选协议时谈地址无意义；地址问题由 ``_gate0_error``（网络，须在锁外）判定。
    """
    return _admission_local_error(entry) or _api_admission_remote_error(entry, data)


def _api_admission_remote_error(entry: dict, data: dict) -> str | None:
    """序 2（地址，网络）+ 序 3（重名，本地）。锁内调用，须先过 ``_admission_local_error``。"""
    return _gate0_error(entry) or _name_dup_error(entry, data)


def _admission_local_error(entry: dict) -> str | None:
    """序 0/1：未选协议 / 无模型（本地、无网络、可在锁外预检）。"""
    if entry_protocol(entry) not in _PROTOCOLS:
        return _ADM_PROTOCOL
    if not _model_ids(entry):
        return _ADM_NOMODEL
    return None


def _name_dup_error(entry: dict, data: dict) -> str | None:
    """序 3：重名 —— 与**另一个** proxy=true provider 的名字集合有交集即拒。

    依据 V0-5 实测：同名模型会被实例**静默路由到先注册的那个、不报错**，用户得到
    「配好了、却调到了别的 provider」这种无提示的错误结果。
    """
    mine = _registered_names(entry)
    for other in data.get("providers") or []:
        if other is entry or not isinstance(other, dict) or other.get("proxy") is not True:
            continue
        if mine & _registered_names(other):
            return _ADM_NAME_DUP
    return None


_ERR_PROXY_BASE_URL = "代理开启时不能修改 base_url，请先关闭代理"
_ERR_CONCURRENT = "provider 正在被并发修改，请重试"
_ERR_BOUND = "provider 仍被绑定，请先解绑再修改代理相关设置: %s"
_CONC_KEYS = ("base_url", "api_key", "anthropic_base_url", "openai_base_url", "models", "proxy")
_TRIPLE = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL")


class ProviderConflict(RuntimeError):
    """PUT 的 409（9.6）：并发改动 / 统一绑定守卫。``bound_bots`` 非 None 时路由原样带出。"""
    def __init__(self, msg: str, bound_bots: list | None = None):
        super().__init__(msg)
        self.bound_bots = bound_bots


# ── E-15（审计条目修法二）：地址探针 404 → 可见 warning（探针 404 判据不变）────────
# 探针收到 404 = host 可达但 <送入值>/chat/completions 没有这个端点：按既有设计不当
# 「地址错」拒保存，但要给用户一个**不再静默**的提示——保存/绑定响应带 warning、面板显示。
WARN_OPENAI_UNVERIFIED = ("openai 上游地址未验证（探测 404）：若调用失败，"
                          "请在面板手填 openai_base_url")
_REQ_WARN = threading.local()   # 请求级旁路；线程复用，须在请求边界清（reset_request_warn_state）


def take_request_warning() -> str | None:
    """取出并清空本线程的 warning 文案（保存类响应出口用）。"""
    w = getattr(_REQ_WARN, "value", None)
    _REQ_WARN.value = None
    return w


def reset_request_warn_state() -> None:
    """请求边界清理（线程复用）：丢弃上一请求残留的 warning 与未验证标记。"""
    _REQ_WARN.value = None
    _REQ_WARN.unverified = None


def _take_unverified_mark():
    """本次请求对条目「未验证」标记的动作：True=置位 / False=清除 / None=不动。"""
    mark = getattr(_REQ_WARN, "unverified", None)
    _REQ_WARN.unverified = None
    return mark


def _admission_with_probe(cand: dict) -> str | None:
    """锁外准入（E-12：序 0/1 本地 → 序 2 探针）；探针跑过时把结论记进请求旁路（E-15 修法二）。

    - 探针 404 → warning 文案 + 条目记「未验证」（**不**算地址错、**不**拒保存，判据不变）；
    - 探针跑过且非 404 → 清除旧标记（地址已验证）。
    （显式覆盖地址的清除不在这里：那种情形锁外条件根本不进来，由 upsert 锁内兜底。）
    ``cand`` 无覆盖地址是调用方条件保证的（否则本函数不会被调）。
    """
    err = _admission_local_error(cand)
    if err:
        return err
    from scripts import proxy_upstream
    proxy_upstream.reset_openai_probe_status()
    err = _gate0_error(cand)
    if err is not None:
        return err
    status = proxy_upstream.last_openai_probe_status()
    if status == 404:
        _REQ_WARN.value = WARN_OPENAI_UNVERIFIED
        _REQ_WARN.unverified = True
    elif status is not None:
        _REQ_WARN.unverified = False
    return None


def _gate0_error(entry: dict) -> str | None:
    """门 0（地址探针，有网络）：调用方保证不持清单锁（9.6）。"""
    from scripts import proxy_upstream   # 调用期导入：面板导入期不应加载网络模块
    # 序 0：地址问题 —— 仅当用户**两侧都没**显式声明地址时才探针（显式声明优先，§5.2）。
    if not (entry.get("anthropic_base_url") or entry.get("openai_base_url")):
        from scripts.derive_endpoints import effective_endpoints
        base = entry.get("base_url") if isinstance(entry.get("base_url"), str) else ""
        _, openai_url = effective_endpoints(base, "", "")
        if not proxy_upstream.probe_base_openai(openai_url, entry.get("api_key") or ""):
            return _ADM_ADDR

    return None


def _gates234_error(entry: dict, data: dict) -> str | None:
    """「序 0/1（本地）+ 序 2/3（含网络探针）」的**锁外**语义（E-12：upsert 主路径已不再用它，
    仅保留给外部/测试；持清单锁时一律改用 ``_gates_local_error``）。"""
    return _admission_local_error(entry) or _api_admission_remote_error(entry, data)


def _gates_local_error(entry: dict, data: dict) -> str | None:
    """锁内版准入门（E-12）：**只做本地判定**——序 0/1（未选协议/无模型）+ 序 3（重名），
    绝不发网络探针；地址门（序 2）已由 ``_gate0_outside_lock`` 在锁外判过。
    持清单锁期间发慢探针会拖住整把清单锁（其它 bot 的读写全被卡）。"""
    return _admission_local_error(entry) or _name_dup_error(entry, data)


def _binding_guard(pid: str, prev: dict, entry: dict, data: dict) -> None:
    """统一绑定守卫（9.6，R8/D1=A）：前后任一 proxy=true，且有已绑定 bot 的应写三键将变（含翻转 proxy）
    或其 model 将不在 E 中 → 409。前后都 proxy=false 不触发。

    本轮输入差异（INTERFACE-provider-protocol-level §4）：usable 由「协议已判 + 不在 unavailable」
    简化为「**provider 的 ``protocol`` 已选**」，即已选协议时 usable = 该 provider 的全部模型。
    """
    if prev.get("proxy") is not True and entry.get("proxy") is not True:
        return
    usable = None
    if entry.get("proxy") is True:
        usable = set(_model_ids(entry)) if entry_protocol(entry) in _PROTOCOLS else set()
    hit = []
    for bot in bound_bots(pid, data):
        model = ((data.get("bindings") or {}).get(bot) or {}).get("model")
        a, b = resolve_env(bot, prev, model), resolve_env(bot, entry, model)
        if any(a[k] != b[k] for k in _TRIPLE) or (usable is not None and model not in usable):
            hit.append(bot)
    if hit:
        hit.sort()
        raise ProviderConflict(_ERR_BOUND % ",".join(hit), hit)


def _norm_override_base_url(raw) -> tuple[bool, str | None]:
    """``anthropic_base_url`` / ``openai_base_url`` 的**唯一**判据（INTERFACE §1.2）。

    返回 ``(ok, value)``：ok=True 时 value 是归一化后的值（``""`` = 显式清空）；
    ok=False 表示非法。合法判据与 ``base_url`` **同一个函数**（http(s) + hostname +
    无 userinfo + 长度 ≤500），只是 ``""`` 额外合法——它表示「显式清空、回到自动推导」。
    """
    if not isinstance(raw, str):
        return False, None
    if raw == "":
        return True, ""
    if not _valid_base_url(raw):
        return False, None
    return True, raw


# 三个地址字段（base_url 与两个覆盖地址）共用这一句：入口只认这一个形态（400 + 本文案）。
_ERR_ADDR_NEEDS_KEY = "base_url 变更时必须同时提交 api_key"


def _no_usable_key(body: dict) -> bool:
    """本次请求有没有提交**非空**密钥（改地址判定专用）：省略 / None / ``""`` / 全空白一律算没有。

    全空白单列出来：它不是 ``""``，照 ``has_key_field`` 的口径算"提交了"，于是一个空格就能
    过"改地址必须重填密钥"这道门，还把存量密钥覆盖成那串空白。非字符串不归这里管，
    留给后面的格式校验报它自己那一句。
    """
    key = body.get("api_key")
    return key is None or (isinstance(key, str) and not key.strip())


def _override_changed(old: dict, anthropic_opt, openai_opt) -> bool:
    """两个覆盖地址里有没有哪个的**值**变了。只比值不比字段：没提交、原样回传都不算；
    清单里缺键与 ``""`` 同为「空」，所以空→非空、非空→空都算变。"""
    return any(opt is not _UNSET and opt != (old.get(key) or "")
               for key, opt in (("anthropic_base_url", anthropic_opt), ("openai_base_url", openai_opt)))


def _gate0_outside_lock(pid, body, proxy_opt, anthropic_opt, openai_opt, protocol_opt, models):
    """锁外预检（9.6 顺序：…→ 开启中改地址 400 → 门 0）。返回错误文案；门 0 跑过时返回探针前现值
    （供提交锁内比对并发改动）；否则 None。404 / 缺 api_key 留给锁内既有判定。

    门顺序（INTERFACE-provider-protocol-level §2）**序 0/1 先于序 2**：未选协议 / 无模型是本地
    判定，必须在地址探针（网络）**之前**给出——故在跑 ``_gate0_error`` 前先 ``_admission_local_error``。
    """
    if pid is None:   # 新建：false→true 且两覆盖地址均空才跑门 0（锁外）；缺 api_key 留给锁内既有 400
        key = body.get("api_key")
        if proxy_opt is not True or anthropic_opt not in (_UNSET, "") or openai_opt not in (_UNSET, "") \
                or not (isinstance(key, str) and key):
            return None
        cand = {"base_url": body.get("base_url"), "api_key": key, "anthropic_base_url": "",
                "openai_base_url": "", "models": models if models is not None else [],
                "protocol": protocol_opt if protocol_opt is not _UNSET else ""}
        return _admission_with_probe(cand)
    try:
        old = _find(load_providers(), pid)
    except ProvidersCorrupt:
        return None
    new_key = body.get("api_key") if isinstance(body.get("api_key"), str) else None
    if old is None or ("base_url" in body and body.get("base_url") != old.get("base_url") and new_key is None):
        return None
    # 覆盖地址的值变了却没带非空密钥：锁内必拒（判据与锁内同一个 _no_usable_key）。这里直接让路，
    # 不为一个注定被拒的请求拿存量密钥去跑网络探针。
    if _no_usable_key(body) and _override_changed(old, anthropic_opt, openai_opt):
        return None
    base_changed = "base_url" in body and body.get("base_url") != old.get("base_url")
    cand = dict(old)
    if "base_url" in body:
        cand["base_url"] = body["base_url"]
    if models is not None:
        cand["models"] = models
    if new_key is not None:
        cand["api_key"] = new_key
    cand["protocol"] = _next_protocol(old, protocol_opt, base_changed)
    for k, v in (("proxy", proxy_opt), ("anthropic_base_url", anthropic_opt), ("openai_base_url", openai_opt)):
        if v is not _UNSET:
            cand[k] = v
    before, after = old.get("proxy") is True, cand.get("proxy") is True
    if before and after and cand.get("base_url") != old.get("base_url"):
        return _ERR_PROXY_BASE_URL
    # 何时必须跑门 0（网络探针，只能在此处锁外跑）：开启中、用户两侧都没显式声明地址，
    # 且本次提交**真的会改变**该 provider 的可用性——首开 / 换 key / 改覆盖地址 / 改 models。
    # 触发条件与锁内本地判定的触发条件一致：锁内不再补跑探针（E-12）。
    models_changed = models is not None and cand.get("models") != old.get("models")
    if after and not (cand.get("anthropic_base_url") or cand.get("openai_base_url")) and (
            not before or models_changed
            or any(cand.get(k) != old.get(k) for k in ("api_key", "anthropic_base_url", "openai_base_url"))):
        err = _admission_with_probe(cand)
        return err if err else copy.deepcopy(old)
    return None


def _next_protocol(entry: dict, protocol_opt, base_changed: bool) -> str:
    """写入后的**有效协议**：显式提交优先；否则「地址一换、结论作废」清空；否则保留（含派生值）。"""
    if protocol_opt is not _UNSET:
        return protocol_opt
    if base_changed:
        return ""
    return entry_protocol(entry)


def upsert_provider(body: dict, provider_id: str | None = None) -> tuple[dict | None, str | None]:
    """新建 / 更新 provider。返回 (脱敏条目, None) 或 (None, 错误文案)。"""
    if not isinstance(body, dict):
        return None, "请求体必须是 JSON 对象"

    updating = provider_id is not None

    # name：新建必填；更新时省略/空串 = 保留旧值（R4「全字段可选」）
    new_name = None
    raw_name = body.get("name")
    if raw_name not in (None, ""):
        if not isinstance(raw_name, str) or len(raw_name) > 200:
            return None, "name 必须是长度 ≤200 的字符串"
        if not _valid_name(raw_name):
            return None, "name 必须是 1–32 个字符、没有空白或控制字符、不以 - 或 _ 开头，且不能是保留字"
        new_name = raw_name
    elif not updating:
        return None, "name required"

    if not updating and not _valid_base_url(body.get("base_url")):
        return None, "base_url 必须是 http/https 开头的地址"
    if updating and "base_url" in body and not _valid_base_url(body.get("base_url")):
        return None, "base_url 必须是 http/https 开头的地址"

    if "models" in body:
        models = _norm_models(body.get("models"))
        if models is None:
            return None, "models 必须是字符串数组（≤500 项）"
    else:
        models = None

    # default_model（可选）：缺省 = 保留旧值；"" = 显式清空；其余见 _norm_default_model。
    # 校验插在锁**之前**：与 §1.7 的优先级一致（"不存在的 id + 非法 default_model" 必须报
    # default_model 那条，不能先撞上锁内的 provider not found），且保证零写入。
    default_model: str | object = _UNSET
    if "default_model" in body:
        default_model = _norm_default_model(body.get("default_model"))
        if default_model is None:
            return None, "default_model 必须是长度 ≤200 的字符串"

    # proxy（可选，INTERFACE §1.2）：缺省/不传 = 保留旧值（新建落 false）；非 bool
    # （含 null / "true" / 1）→ 400。判据是「键存在且非 bool」，故 ``"proxy": null`` 也拒。
    proxy_opt: bool | object = _UNSET
    if "proxy" in body:
        if not isinstance(body.get("proxy"), bool):
            return None, "proxy 必须是布尔值"
        proxy_opt = body.get("proxy")

    # protocol（可选，INTERFACE-provider-protocol-level §1）：取值必须是 "anthropic" / "openai"，
    # 否则（含 ""、null、数字、其它字符串）→ 400。**新建时键必须存在**（"" 不是合法新建值）。
    # 校验先于锁内 provider not found（§2.2：「不存在 id + 非法 protocol」报 protocol 那条）。
    protocol_opt: str | object = _UNSET
    if "protocol" in body:
        value = body.get("protocol")
        if not isinstance(value, str) or value not in _PROTOCOLS:
            return None, "protocol 必须是 anthropic 或 openai"
        protocol_opt = value
    elif not updating:
        return None, "protocol 必须是 anthropic 或 openai"

    # 两个 base_url 覆盖（可选，INTERFACE §1.2）：缺省/不传 = 保留旧值（新建落 ""）；
    # "" = 显式清空（回到自动推导）；非 str / 非法地址 → 400。两条文案同构、逐字。
    anthropic_opt: str | object = _UNSET
    if "anthropic_base_url" in body:
        ok, value = _norm_override_base_url(body.get("anthropic_base_url"))
        if not ok:
            return None, "anthropic_base_url 必须是 http/https 开头的地址"
        anthropic_opt = value
    openai_opt: str | object = _UNSET
    if "openai_base_url" in body:
        ok, value = _norm_override_base_url(body.get("openai_base_url"))
        if not ok:
            return None, "openai_base_url 必须是 http/https 开头的地址"
        openai_opt = value

    pre_old = _gate0_outside_lock(provider_id, body, proxy_opt, anthropic_opt, openai_opt, protocol_opt, models)
    if isinstance(pre_old, str):
        return None, pre_old
    with _manifest_lock():
        data = load_providers()

        if updating:
            entry = _find(data, provider_id)
            if entry is None:
                return None, "provider not found"
        else:
            entry = None

        # api_key：更新时省略/空串 = 保留旧值；新建时与 has_key_field 同一判据
        # （None/空串 = 未提供）。若放空串过去，会造出一个永远绑不上的 provider
        # （set_bot_provider 要求 api_key 非空）却返回 201。
        has_key_field = "api_key" in body and body.get("api_key") not in (None, "")
        # 凭据与地址必须同源（rev10）：更新时若 base_url 的**值**真的变了，必须在同一次请求里
        # 提交非空 api_key，否则存储中的真 key 会被改道发往新地址（POST /api/providers/models
        # 的 ① 模式把清单里的明文 key 发向清单里的 base_url；改地址不必重填 key = 外泄出口）。
        # 只比“值”不比“字段”：同值回传不带 key 是真实流程，不得 400。零写入：任何 entry[...] = 之前返回。
        # “没提交非空 key”的判据是 _no_usable_key：api_key 省略/None/""/全空白一律视为未提交
        # （比 has_key_field 多认一个全空白，否则一个空格就能过这道门）。
        # 两个覆盖地址一视同仁：存量 key 同样会与它们配对写进代理配置、拉模型回落时发往
        # openai 覆盖地址，所以三个地址字段任一值变化都走这同一条拒绝。
        if updating and _no_usable_key(body) and (
                ("base_url" in body and body.get("base_url") != entry.get("base_url"))
                or _override_changed(entry, anthropic_opt, openai_opt)):
            return None, _ERR_ADDR_NEEDS_KEY
        if has_key_field:
            api_key = body.get("api_key")
            if not isinstance(api_key, str) or len(api_key) > 500:
                return None, "api_key 必须是长度 ≤500 的字符串"
        elif updating:
            api_key = entry.get("api_key")
        else:
            return None, "api_key required"

        now = int(time.time())
        if entry is None:
            new_entry = {
                "id": new_provider_id(data),
                "name": new_name,
                "base_url": body["base_url"],
                "api_key": api_key,
                "models": models if models is not None else [],
                "default_model": "" if default_model is _UNSET else default_model,
                "created_at": now,
                "updated_at": now,
                "proxy": False,
                "protocol": protocol_opt,   # 新建必填且已校验为 anthropic/openai
                "anthropic_base_url": "",
                "openai_base_url": "",
            }
            data["providers"].append(new_entry)
            entry = new_entry
        else:
            prev = copy.deepcopy(entry)
            if pre_old is not None and any(prev.get(k) != pre_old.get(k) for k in _CONC_KEYS):
                raise ProviderConflict(_ERR_CONCURRENT)   # 门 0 探针期间被别的请求改动（锁内最新清单）
            old_base_url = entry.get("base_url")
            base_changed = "base_url" in body and body.get("base_url") != old_base_url
            next_protocol = _next_protocol(entry, protocol_opt, base_changed)
            if new_name is not None:
                entry["name"] = new_name
            if "base_url" in body:
                entry["base_url"] = body["base_url"]
            if api_key is not None:
                entry["api_key"] = api_key
            if models is not None:
                entry["models"] = models
            if default_model is not _UNSET:
                entry["default_model"] = default_model
            entry["updated_at"] = now
            # 协议固化 + 旧键删除（INTERFACE-provider-protocol-level §1/§2.5）：显式提交优先；
            # 否则 base_url 真的换了就清空（地址一换、结论作废），否则保留有效协议（含派生值）。
            # 任意一次 PUT 都把旧 protocols / unavailable_models 从条目删除，不留空壳。
            entry["protocol"] = next_protocol
            entry.pop("protocols", None)
            entry.pop("unavailable_models", None)

        # 本轮可写字段（§1.2）。protocol 已在上面按语义写入。
        if proxy_opt is not _UNSET:
            entry["proxy"] = proxy_opt
        if anthropic_opt is not _UNSET:
            entry["anthropic_base_url"] = anthropic_opt
        if openai_opt is not _UNSET:
            entry["openai_base_url"] = openai_opt

        # E-15（修法二）：锁外探针的结论落到条目「未验证」标记（绑定响应据此继续提示）。
        # 内部键，不进 _public_entry 的 14 键；本请求没跑探针（mark=None）时保留旧值。
        _mark = _take_unverified_mark()
        if _mark is True:
            entry["openai_addr_unverified"] = True
        elif _mark is False:
            entry.pop("openai_addr_unverified", None)
        elif entry.get("anthropic_base_url") or entry.get("openai_base_url"):
            # 显式覆盖地址（任一）→ 锁外探针本就不跑（上面条件排除了这种情形），
            # 用户已自己声明地址，旧「未验证」标记不再适用。
            entry.pop("openai_addr_unverified", None)

        # 开启 proxy=true 的准入判定（INTERFACE-provider-protocol-level §2）。门顺序固定为
        # **未选协议 → 无模型 → 地址问题 → 重名**：更具体的诊断优先；地址门已在锁外判过。
        if provider_id is None:
            err = _gates_local_error(entry, data) if entry.get("proxy") is True else None   # 门 0 已在锁外
        else:   # 9.6：开启中改地址 → 统一守卫 → 准入门（仅 false→true 或开启中 models 变化才跑）
            before, after = prev.get("proxy") is True, entry.get("proxy") is True
            if before and after and entry.get("base_url") != prev.get("base_url"):
                return None, _ERR_PROXY_BASE_URL
            _binding_guard(provider_id, prev, entry, data)
            err = (_gates_local_error(entry, data)
                   if after and (not before or entry.get("models") != prev.get("models")) else None)
        if err:
            return None, err

        # 迁移前备份（SECURITY-REPORT-provider-protocol-level 重要-1）：**仅当本次写确实会从
        # 该条目删掉旧键**时才落备份（更新分支必 pop 旧键，见上）。不含旧键的普通 PUT 不产生
        # 备份，避免每次改名都堆备份。备份机会性：失败不阻断本次写（helper 内部自吞并记 stderr）。
        if provider_id is not None and any(k in prev for k in _LEGACY_KEYS):
            _backup_providers_before_migration()
        _commit_locked(data)
        public = _public_entry(entry)
        snapshot = copy.deepcopy(data)     # 快照在**锁内**取（I3）
    _sync_after_write(snapshot)
    return public, None


def delete_provider(provider_id: str) -> tuple[bool, str | None]:
    with _manifest_lock():
        data = load_providers()
        entry = _find(data, provider_id)
        if entry is None:
            return False, "provider not found"
        bound = bound_bots(provider_id, data)
        if bound:
            return False, "provider 仍被绑定: " + ",".join(bound)
        if any(k in entry for k in _LEGACY_KEYS):
            # 建议-3：删掉仍带旧键的条目会让旧的逐模型判定整条消失，同样在落盘前留回退点。
            _backup_providers_before_migration()
        data["providers"] = [p for p in data["providers"] if p.get("id") != provider_id]
        _commit_locked(data)
        snapshot = copy.deepcopy(data)     # 快照在**锁内**取（I3）
    _sync_after_write(snapshot)
    return True, None


def _sync_after_write(snapshot: dict) -> dict | None:
    """写后重建代理配置（R4/R5/R6/R7/R8 的共同副作用）；返回 ``provider_proxy.sync`` 的结果。

    **no-op 由 ``provider_proxy.sync`` 自己保证**（无 proxy provider 时不写盘、不探测、
    不 kickstart、不创建运行目录）——故这里无条件调用是安全的，锁定套件不受影响。
    重建**异常不得整体吞掉**（E-13）：写 stderr 一行并把代理状态 ``last_error`` 置位，
    也不得影响调用者自身的状态码/文案（§2.1 R6 副作用），故不重抛，返回失败体供
    E-17 的绑定路径据实报失败；其它调用方忽略返回值即可。
    """
    if getattr(_LOCK_STATE, "held", False):   # 在 try 之外：锁序违例必须抛出，不能被吞
        raise RuntimeError("锁序违例：持有清单锁时不得进入配置锁")
    try:
        import provider_proxy
        return provider_proxy.sync(snapshot)
    except Exception as e:
        msg = "写后重建代理配置失败: %s" % type(e).__name__
        print("[provider_config] %s" % msg, file=sys.stderr)
        try:
            import provider_proxy
            provider_proxy._LAST_ERROR = msg
        except Exception:
            pass
        return {"ok": False, "error": msg}


# ── 模型列表：只增不减写回 + meta（批1-C；INTERFACE §3/§5）────────────────────

# 写回列表上限（与拉取截断、面板 PUT 同口径：500 封顶）
_META_MAX_MODELS = 500


def _norm_model_items(models) -> list:
    """拉到的列表归一成 ``[{id,label}]`` 并去重保序（非法元素跳过）；元素恒两键（§3 C2）。"""
    out, seen = [], set()
    for m in models or []:
        mid = m.get("id") if isinstance(m, dict) else (m if isinstance(m, str) else None)
        if not isinstance(mid, str) or not mid or mid in seen:
            continue
        seen.add(mid)
        label = (m.get("label") if isinstance(m, dict) else None) or mid
        out.append({"id": mid, "label": label if isinstance(label, str) and label else mid})
    return out


def _entry_models_by_id(entry: dict) -> dict:
    """条目现存的模型（id → {id,label}），对本地可手改的 JSON 容错。"""
    out = {}
    for m in entry.get("models") or []:
        mid = m.get("id") if isinstance(m, dict) else (m if isinstance(m, str) else None)
        if isinstance(mid, str) and mid and mid not in out:
            label = (m.get("label") if isinstance(m, dict) else None) or mid
            out[mid] = {"id": mid, "label": label if isinstance(label, str) and label else mid}
    return out


def _meta_manual_models(entry: dict) -> list:
    meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
    out, seen = [], set()
    for m in meta.get("manual_models") or []:
        if isinstance(m, str) and m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def merge_models(entry: dict, models) -> list:
    """**只增不减**（INTERFACE §3/§5 增强三）：新列表 = 拉到的（去重保序）∪ ``meta.manual_models``
    中缺项——缺项保留原条目里的原值（label 与相对顺序都照旧，追加在拉到的之后）。500 封顶。

    R6 写回前的绑定守卫/重名判定也用本函数算「写回后的最终列表」，与 ``store_models`` 同一口径
    （§4⑥：消除"守卫按拉到的列表、写回按并集"的误 409）。"""
    merged = _norm_model_items(models)
    have = {m["id"] for m in merged}
    old = _entry_models_by_id(entry)
    for mid in _meta_manual_models(entry):
        if mid in have:
            continue
        have.add(mid)
        merged.append(old.get(mid) or {"id": mid, "label": mid})
    return merged[:_META_MAX_MODELS]


def merge_meta(entry: dict, contexts: dict | None) -> dict:
    """meta 写法（§3 数据契约）：``manual_models`` 记账保留；``owner_context``（主人数值）
    不被覆盖；``guessed_context`` 按**当次抽取**重算（抽不到就不记）。"""
    meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
    owner = {}
    raw_owner = meta.get("owner_context")
    if isinstance(raw_owner, dict):
        for k, v in raw_owner.items():
            if isinstance(k, str) and k and isinstance(v, int) and not isinstance(v, bool):
                owner[k] = v
    guessed = {}
    if isinstance(contexts, dict):
        for k, v in contexts.items():
            if isinstance(k, str) and k and isinstance(v, int) and not isinstance(v, bool):
                guessed[k] = v
    return {"manual_models": _meta_manual_models(entry), "owner_context": owner,
            "guessed_context": guessed}


def _store_into_entry(entry: dict, models: list, contexts: dict | None) -> None:
    """只增不减写回一个条目（两个映射都先算完再赋值——merge_* 都要读改动前的 entry）。"""
    merged, meta = merge_models(entry, models), merge_meta(entry, contexts)
    entry["models"] = merged
    entry["meta"] = meta


# ── 管理模型（批2；INTERFACE-tg-provider-v2 §1 §1.1 §3）────────────────────
# 三个函数是 /model add|del|ctx 的权威实现（文本版在 tg_provider，按钮版复用同一处）。
# 返回口径：(结果, None) 或 (None, 错误码)；错误码由调用层映射成各自文案。
# 错误码：bad_id / bad_ctx / missing / dup / full / "bound:<bot,..>" / corrupt。

def _digits(s) -> bool:
    """纯 ASCII 数字串（不用 str.isdigit：上标/全角字符会让 int() 抛错）。"""
    return bool(s) and all("0" <= c <= "9" for c in s)


def parse_context(text):
    """上下文长度写法的**唯一判据**（§1.1；tg_provider 与按钮版共用）：

    纯整数，或 ``数字[k/m]``（k×1000、m×1000000，四舍五入）；结果须落在
    1024–100000000（含端点）内才有效，否则 None。
    """
    if not isinstance(text, str):
        return None
    t = text.strip()
    if not t:
        return None
    mult = None
    if t[-1] in "kKmM":
        mult = 1000 if t[-1] in "kK" else 1000000
        t = t[:-1]
        if not t:
            return None
    head, dot, tail = t.partition(".")
    if not _digits(head) or (dot and not _digits(tail)):
        return None
    try:
        if dot:
            if mult is None:            # 无后缀时必须是整数："1.5" 非法
                return None
            n = int(float("%s.%s" % (head, tail)) * mult + 0.5)   # 四舍五入（值为正）
        else:
            n = int(head) * (mult or 1)
    except (ValueError, OverflowError):
        # 一长串数字：float() 溢出成 inf 后 int() 抛 OverflowError；纯数字超过解释器的
        # 整数字符串位数上限时 int() 抛 ValueError。都只是"写法非法"，不该冒成异常。
        return None
    return n if 1024 <= n <= 100000000 else None


def _storable_text(v: str) -> bool:
    """字符串能否原样写进清单的**唯一判据**（手动加模型的模型 id、按勾选写回的模型名与
    显示名共用）：不含控制字符（C0、DEL、C1），且能编码成 UTF-8。

    孤立代理项（JSON 里写 ``"\\ud800"`` 就能送进来）过得了长度与类型检查，却要到落盘时
    才抛 ``UnicodeEncodeError``——必须在校验阶段就拒掉。"""
    try:
        v.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return not any(ord(ch) < 32 or 0x7F <= ord(ch) <= 0x9F for ch in v)


def _valid_model_id(v) -> bool:
    """模型 id 规则（§1.1；与 ``_valid_name`` 同族）：1–200 字符、无空白/控制字符、写得成 UTF-8。"""
    if not isinstance(v, str) or not (1 <= len(v) <= 200):
        return False
    return _storable_text(v) and not any(ch.isspace() for ch in v)


def _norm_context_arg(context):
    """``add_model``/``set_model_ctx`` 的 context 入参归一：None / 合法 int / 可解析的字符串
    → 合法 int 或 None；其余 → ``_UNSET`` 哨兵（调用层转 bad_ctx）。"""
    if context is None:
        return None
    if isinstance(context, str):
        context = parse_context(context)
    if isinstance(context, int) and not isinstance(context, bool) and 1024 <= context <= 100000000:
        return context
    return _UNSET


def _meta_mut(entry: dict) -> dict:
    """取条目的 ``meta`` 子字典（可写）；缺失/形态不对 → 就地补齐为空结构（不动既有合法内容）。"""
    meta = entry.get("meta")
    if not isinstance(meta, dict):
        meta = entry["meta"] = {}
    if not isinstance(meta.get("manual_models"), list):
        meta["manual_models"] = []
    for k in ("owner_context", "guessed_context"):
        if not isinstance(meta.get(k), dict):
            meta[k] = {}
    return meta


def _model_users(data: dict, provider_id, model_id) -> list:
    """绑定到「这条 provider + 这个模型」的 bot（升序）。

    删模型的拒绝判定、按勾选写回的拒绝判定、面板明细三处共用同一口径：只看清单
    ``bindings``，不看该 bot 是否停用、是否待重启、是否在发现名单里。"""
    return sorted(b for b, bd in (data.get("bindings") or {}).items()
                  if isinstance(bd, dict) and bd.get("provider_id") == provider_id
                  and bd.get("model") == model_id)


def add_model(provider_id: str, model_id: str, context=None):
    """手动加模型（§1 表/§3）：加进 ``models[]`` ∪ ``meta.manual_models``；``context`` 可选
    （省略=不落该模型的 ctx 记录），提供时写 ``meta.owner_context[id]``。

    返回 ``({"context": int|None}, None)`` 或 ``(None, 错误码)``。500 上限与全网同口径。
    """
    if not _valid_model_id(model_id):
        return None, "bad_id"
    ctx = _norm_context_arg(context)
    if ctx is _UNSET:
        return None, "bad_ctx"
    try:
        with _manifest_lock():
            data = load_providers()
            entry = _find(data, provider_id)
            if entry is None:
                return None, "missing"
            if model_id in _entry_models_by_id(entry):
                return None, "dup"
            if len(entry.get("models") or []) >= _META_MAX_MODELS:
                return None, "full"
            models = entry.get("models")
            if not isinstance(models, list):   # 本地可手改的 JSON 容错
                models = entry["models"] = []
            models.append({"id": model_id, "label": model_id})
            meta = _meta_mut(entry)
            if model_id not in meta["manual_models"]:
                meta["manual_models"].append(model_id)
            if ctx is not None:
                meta["owner_context"][model_id] = ctx
            _commit_locked(data)
            out = copy.deepcopy(data)
    except ProvidersCorrupt:
        return None, "corrupt"
    _sync_after_write(out)
    return {"context": ctx}, None


def del_model(provider_id: str, model_id: str):
    """删模型（§1 表/§3）：从 ``models[]`` 移除 + 清 meta 三处
    （``manual_models`` / ``owner_context`` / ``guessed_context``）。

    **被任何 bot 绑定使用**（该 provider 下 binding.model == id）→ 拒绝，返回使用名单。
    返回 ``({"was_manual": bool, "count": int}, None)`` 或 ``(None, 错误码)``；
    使用中错误码形如 ``"bound:bot2,bot3"``。
    """
    if not isinstance(model_id, str) or not model_id:
        return None, "missing"
    try:
        with _manifest_lock():
            data = load_providers()
            entry = _find(data, provider_id)
            if entry is None or model_id not in _model_ids(entry):
                return None, "missing"
            users = _model_users(data, provider_id, model_id)
            if users:
                return None, "bound:" + ",".join(users)
            was_manual = model_id in _meta_manual_models(entry)
            kept = []
            for m in entry.get("models") or []:
                mid = m.get("id") if isinstance(m, dict) else (m if isinstance(m, str) else None)
                if mid != model_id:
                    kept.append(m)
            entry["models"] = kept
            meta = _meta_mut(entry)
            meta["manual_models"] = [m for m in meta["manual_models"] if m != model_id]
            meta["owner_context"].pop(model_id, None)
            meta["guessed_context"].pop(model_id, None)
            _commit_locked(data)
            count = len(entry["models"])
            out = copy.deepcopy(data)
    except ProvidersCorrupt:
        return None, "corrupt"
    _sync_after_write(out)
    return {"was_manual": was_manual, "count": count}, None


def set_model_ctx(provider_id: str, model_id: str, context):
    """改模型上下文长度（§1 表/§1.1）：写 ``meta.owner_context[id]``，同 id 从
    ``guessed_context`` 移出（主人值覆盖推测值）。

    返回 ``(True, None)`` 或 ``(None, 错误码)``；id 不在列表 → ``missing``。
    """
    ctx = _norm_context_arg(context)
    if ctx is _UNSET:
        return None, "bad_ctx"
    try:
        with _manifest_lock():
            data = load_providers()
            entry = _find(data, provider_id)
            if entry is None or model_id not in _model_ids(entry):
                return None, "missing"
            meta = _meta_mut(entry)
            meta["owner_context"][model_id] = ctx
            meta["guessed_context"].pop(model_id, None)
            _commit_locked(data)
            out = copy.deepcopy(data)
    except ProvidersCorrupt:
        return None, "corrupt"
    _sync_after_write(out)
    return True, None


def _valid_context_value(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 1024 <= v <= 100000000


def set_models(provider_id: str, items, base):
    """把模型列表**整体设成**提交的集合（面板多选对话框的"确定"；INTERFACE-provider-ux §5）。

    这是「只增不减」口径的例外：用户明确取消勾选的模型会被移除（手动添加的也一样）。
    ``items``：模型名字符串，或 ``{"id", "label"?, "context"?}``；``base``：调用方打开对话框时
    看到的模型名列表——清单锁内与现状逐项比对，不同即拒绝，免得把别处刚加的模型当成
    "未勾选"顺手删掉（丢更新）。

    返回 ``({"models": [...], "added": [...], "removed": [...]}, None)`` 或 ``(None, 错误码)``；
    错误码：bad_models / bad_ctx / bad_base / corrupt / missing / changed / name_dup。
    将被移除的模型正被 bot 使用时返回 ``([{"model", "bots"}, ...], "in_use")``（第一项带名单）。
    任何错误都零写入。
    """
    # —— 入参校验（锁外、不读清单）：形状 → 上下文 → base ——
    if _norm_models(items) is None:      # 列表、≤500 项、元素是字符串或带字符串 id 的对象、名字 ≤500
        return None, "bad_models"
    if any(not (item if isinstance(item, str) else item["id"]) for item in items):
        return None, "bad_models"        # 空模型名

    def adopted_label(item):             # 会被采用的显示名：非空且不超过 500 的字符串；否则沿用原值
        label = item.get("label") if isinstance(item, dict) else None
        return label if isinstance(label, str) and 0 < len(label) <= 500 else None

    if any(not _storable_text(text) for item in items
           for text in (item if isinstance(item, str) else item["id"], adopted_label(item) or "")):
        return None, "bad_models"        # 模型名或显示名含控制字符 / 写不成 UTF-8（与手动加模型同一判据）
    if any(isinstance(item, dict) and item.get("context") is not None
           and not _valid_context_value(item["context"]) for item in items):
        return None, "bad_ctx"
    if not isinstance(base, list) or not all(isinstance(b, str) for b in base):
        return None, "bad_base"
    wanted, contexts = {}, {}            # id → 提交的显示名（None=沿用原值）；id → 提交的上下文
    for item in items:
        model_id = item if isinstance(item, str) else item["id"]
        if model_id in wanted:           # 同名只认第一次（显示名与上下文都以第一次为准）
            continue
        wanted[model_id] = adopted_label(item)
        if isinstance(item, dict) and item.get("context") is not None:
            contexts[model_id] = item["context"]

    try:
        with _manifest_lock():
            data = load_providers()
            entry = _find(data, provider_id)
            if entry is None:
                return None, "missing"
            current = _model_ids(entry)
            if current != base:
                return None, "changed"
            removed = [m for m in dict.fromkeys(current) if m not in wanted]   # 原清单顺序，去重
            in_use = []
            for m in removed:
                users = _model_users(data, provider_id, m)
                if users:
                    in_use.append({"model": m, "bots": users})
            if in_use:
                return in_use, "in_use"
            old = _entry_models_by_id(entry)
            entry["models"] = [{"id": m, "label": label or (old.get(m) or {}).get("label") or m}
                               for m, label in wanted.items()]
            # data 是锁内刚读出的局部副本：下面任何一步 return 都不落盘，等于零写入
            if entry.get("proxy") is True and _name_dup_error(entry, data):
                return None, "name_dup"
            meta = _meta_mut(entry)
            # 手动标记只留"原来就在列表里、现在仍在"的；本次新加入的不算手动添加
            # （先判类型：手改坏的清单里这里可能混着不可哈希的值）
            meta["manual_models"] = [m for m in meta["manual_models"]
                                     if isinstance(m, str) and m in wanted and m in old]
            for m in removed:
                meta["owner_context"].pop(m, None)
                meta["guessed_context"].pop(m, None)
            meta["guessed_context"].update(contexts)   # 提交带来的是"上游提供"的值，不动手动值
            _commit_locked(data)
            result = {"models": copy.deepcopy(entry["models"]),
                      "added": [m for m in wanted if m not in old],
                      "removed": removed}
            out = copy.deepcopy(data)
    except ProvidersCorrupt:
        return None, "corrupt"
    _sync_after_write(out)
    return result, None


def _snapshot_of(entry: dict) -> dict:
    """刷新快照（锁外拉取期间别的进程改动可比对出来）：key_fp / updated_at / models 前值。"""
    return {"key_fp": _key_fp(entry.get("api_key") or ""),
            "updated_at": entry.get("updated_at"),
            "models": copy.deepcopy(entry.get("models") or [])}


def refresh_snapshot(provider_id: str) -> dict | None:
    """刷新前取快照（读侧，不加锁）。条目不存在或清单读坏 → None。"""
    entry = _find(_load_providers_safe(), provider_id)
    return _snapshot_of(entry) if entry is not None else None


def refresh_store_models(provider_id: str, models: list, contexts: dict | None, snapshot: dict) -> dict:
    """刷新写回：**锁内**先比对快照、未被改过才只增不减写回（§4②：拉取在锁外）。

    返回 ``{"ok": True, "added": [...], "removed": [...], "manual_kept": [...]}``（供成功文案
    算"新增 a / 去掉 r / 手动保留 k"）；未写回时 ``{"ok": False, "reason": ...}``，reason ∈
    ``changed``（拉取期间被改过→放弃写入）/ ``missing``（条目已被删）/ ``empty``（空列表不写回）/
    ``corrupt``（清单读坏）。
    """
    if not models:
        return {"ok": False, "reason": "empty"}
    try:
        with _manifest_lock():
            data = load_providers()
            entry = _find(data, provider_id)
            if entry is None:
                return {"ok": False, "reason": "missing"}
            if snapshot is not None and _snapshot_of(entry) != snapshot:
                return {"ok": False, "reason": "changed"}
            before = [m.get("id") if isinstance(m, dict) else m for m in (entry.get("models") or [])]
            manual = _meta_manual_models(entry)
            _store_into_entry(entry, models, contexts)
            final = [m["id"] for m in entry["models"]]
            info = {"ok": True,
                    "added": [i for i in final if i not in before],
                    "removed": [i for i in before if i not in final],
                    "manual_kept": [i for i in final if i in set(manual)]}
            _commit_locked(data)
            out = copy.deepcopy(data)
    except ProvidersCorrupt:
        return {"ok": False, "reason": "corrupt"}
    _sync_after_write(out)
    return info


def store_models(provider_id: str, models: list, contexts: dict | None = None) -> None:
    """把拉取到的模型快照**只增不减**写回 provider（读改写**在清单锁内一次完成**）。

    - 新列表 = 拉到的（去重保序）∪ ``meta.manual_models`` 缺项；**空列表不写回**。
    - ``meta.guessed_context`` 按当次抽取重算，``owner_context``/``manual_models`` 保留。
    - provider 已不存在 / 文件损坏 → 静默跳过（既有语义）。

    原实现（调用方 load_providers → 改 → save_providers）在锁外跨两次加锁读改写，中间
    空档里别人写的 binding 会被手里那份陈旧快照整份覆盖（丢更新）；下沉到这里与其它
    providers.json 读改写共用同一把锁。
    """
    if not models:
        return
    try:
        with _manifest_lock():
            data = load_providers()
            entry = _find(data, provider_id)
            if entry is None:
                return
            _store_into_entry(entry, models, contexts)
            _commit_locked(data)
    except ProvidersCorrupt:
        return


def bound_bots(provider_id: str, data: dict | None = None) -> list[str]:
    if data is None:
        data = _load_providers_safe()
    return sorted(b for b, bd in (data.get("bindings") or {}).items()
                  if isinstance(bd, dict) and bd.get("provider_id") == provider_id)


# ── bot 目录 / local 层 ─────────────────────────────────────────────────────

def _bot_cfg(bot_id: str) -> dict | None:
    """已知 bot 的配置；yml 缺失/非法 → None（归一化，不外抛）。

    归一化的异常集（docstring 承诺"非法→None"，逃逸会让面板 500）：
    yml 不存在/不可读（OSError）、bot_id 非法（ValueError）、yml 语法错误（yaml.YAMLError）、
    yml 根不是映射（标量 → AttributeError；列表 → ``cfg.pop(key, default)`` 走 list.pop
    的签名而抛 TypeError）。后两类原先都会穿出函数。
    """
    try:
        return config_loader.load_bot(bot_id)
    except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
        return None


def _within_channels_root(real_dir: str) -> bool:
    """bot 目录（调用方已 ``os.path.realpath``）是否落在 ``CHANNELS_ROOT`` 内。

    settings.json 写口与侧车哨兵写口共用同一判据：``bot_channel_path`` 来自（可被测试
    接缝重定向的）yml，只要它指向 ``CHANNELS_ROOT`` 之外，两处都必须拒写——否则
    yml 一改就能把带明文 key 的文件写到任意目录。
    """
    real_root = os.path.realpath(CHANNELS_ROOT)
    return real_dir == real_root or real_dir.startswith(real_root + os.sep)


def _discovered_bot_ids() -> set:
    """发现的 bot 名单 id 集合（yml ∪ channels 目录）；面板写口的合法集合。

    yml 段来自 T04``bots_registry``（含停用 bot）；目录段兼容 dsh 侧只建工作区、
    不写本地 yml 的 bot（发现口径与 CBL ``list_discovered_bots`` 一致）。
    """
    ids = {d["id"] for d in bot_registry.bots(include_disabled=True)}
    root = _channels_dir()
    try:
        names = os.listdir(root)
    except OSError:
        names = []
    for name in names:
        if not bot_registry.safe_bot_id(name) or name.startswith(("_", ".")):
            continue        # 模板/隐藏目录不是 bot（与 configs/*.yml 跳过 `_` 同口径）
        if name in ("media", "group_transcripts"):
            continue
        path = os.path.join(root, name)
        if os.path.isdir(path) and not os.path.islink(path):
            ids.add(name)
    return ids


def _bot_dir(bot_id: str) -> str | None:
    """bot 的 channels 工作目录（写盘基准），两分支：

    - yml 存在且有 ``bot_channel_path`` 字段 → 合法绝对路径返回之；字段非法
      （非 str/空/非绝对）→ None，保持既有报错——**即使同名目录存在**，配置损坏
      不被"同名目录"静默掩盖（I-6）。
    - 仅"无 yml 或 yml 无该字段"时兜底：bot ∈ 发现集合且 ``<CHANNELS_ROOT>/<id>``
      是目录 → 返回之；否则 None。yml 存在却读不出来（损坏）同样不兜底。
    """
    cfg = _bot_cfg(bot_id)
    if cfg is None:
        yml_path = os.path.join(str(bot_registry.configs_dir()), "%s.yml" % bot_id)
        if os.path.exists(yml_path):
            return None
    elif "bot_channel_path" in cfg:
        p = cfg.get("bot_channel_path")
        if not isinstance(p, str) or not p or not os.path.isabs(p):
            return None
        return p
    if bot_id not in _discovered_bot_ids():
        return None
    candidate = os.path.join(_channels_dir(), bot_id)
    return candidate if os.path.isdir(candidate) else None


def bot_settings_path(bot_id: str) -> str | None:
    d = _bot_dir(bot_id)
    return os.path.join(d, ".claude", "settings.json") if d else None


def bot_local_path(bot_id: str) -> str | None:
    d = _bot_dir(bot_id)
    return os.path.join(d, ".claude", "settings.local.json") if d else None


def _read_settings_doc(path: str | None) -> dict | None:
    """读一份 settings.json 的顶层对象；缺失/不可读/非 JSON 对象 → None（只读，绝不写）。"""
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _env_of(doc: dict | None) -> dict:
    env = doc.get("env") if isinstance(doc, dict) else None
    return env if isinstance(env, dict) else {}


def _read_env(path: str | None) -> dict:
    """读一份 settings 的 env；文件缺失/坏 JSON → {}（读侧容错）。"""
    return _env_of(_read_settings_doc(path))


def local_provider_keys(bot_id: str) -> list[str]:
    """<bot_dir>/.claude/settings.local.json 里出现的 provider 键名（按 ALLOWED 顺序）。"""
    env = _read_env(bot_local_path(bot_id))
    return [k for k in PROVIDER_KEYS if k in env]


# ── 唯一落盘点 ─────────────────────────────────────────────────────────────

def _proxy_meta_readonly() -> dict:
    """只读地取顶层 ``proxy`` 段（GET /api/proxy 用；**绝不生成 key**，读侧无副作用）。"""
    try:
        data = load_providers()
    except ProvidersCorrupt:
        return {}
    meta = data.get("proxy")
    return meta if isinstance(meta, dict) else {}


def _context_with_source(provider: dict, model: str):
    """模型在该 provider 里的上下文长度与来源：``meta.owner_context`` 优先、其次
    ``guessed_context``。返回 ``(值, "owner" | "guessed")``；都没有（或不是整数）→ ``(None, None)``。

    写给 bot 的值（``_bound_context``）与面板显示的值（``model_details``）共用这一处判定顺序。"""
    meta = provider.get("meta") if isinstance(provider.get("meta"), dict) else {}
    for key, source in (("owner_context", "owner"), ("guessed_context", "guessed")):
        m = meta.get(key)
        if isinstance(m, dict):
            v = m.get(model)
            if isinstance(v, int) and not isinstance(v, bool):
                return v, source
    return None, None


def _bound_context(provider: dict, model: str):
    """绑定模型在该 provider 里的上下文长度（§4）：``meta.owner_context`` 优先、其次
    ``guessed_context``；都没有（或不是整数）→ None（条件落键：无值不写键）。"""
    return _context_with_source(provider, model)[0]


def model_details(entry: dict, data: dict) -> list:
    """条目下每个模型的明细（面板展开模型列表用；INTERFACE-provider-ux §2.2）。

    与 ``_public_entry`` 的 ``models`` 逐项对应：上下文与来源、是否手动添加、被哪些 bot 绑定。
    只含模型名、整数与 bot 名，不带任何密钥。"""
    manual = set(_meta_manual_models(entry))
    provider_id = entry.get("id")
    out = []
    for model_id in _model_ids(entry):
        context, source = _context_with_source(entry, model_id)
        out.append({"id": model_id, "context": context, "context_source": source,
                    "manual": model_id in manual,
                    "used_by": _model_users(data, provider_id, model_id)})
    return out


def resolve_env(bot_id: str, provider: dict, model: str) -> dict:
    """把「某 bot 应该拿到的 env 键」算成纯数据（微信接入位：直接把返回值当子进程 env）。

    **键集 = 7 键恒在（保持原序）+ 白名单扩展键条件追加**（INTERFACE-tg-provider-v2 §4）：
    绑定模型有上下文（owner 优先、其次 guessed）→ 追加 ``CLAUDE_CODE_AUTO_COMPACT_WINDOW``；
    没有则不落键。``proxy=true`` 时分叉（§3）：base_url 指向本机代理
    ``http://127.0.0.1:<port>``（<port> = 当前配置端口）、token 换成**实例 key**、
    model 换成**客户端名** ``<pid>/<model>``（**字面拼接**，模型名自带 ``/`` 时不折叠，N2）。
    ``proxy=false``（含全部既有 provider）走原分支，**字节级等价**（R2，至多追加扩展键）。
    """
    if provider.get("proxy") is True:
        import provider_proxy
        client_model = "%s/%s" % (provider.get("id"), model)
        env = {
            "ANTHROPIC_BASE_URL": provider_proxy.base_url(),
            "ANTHROPIC_AUTH_TOKEN": _proxy_meta_readonly().get("instance_key") or "",  # 只读，不生成 key
            "ANTHROPIC_MODEL": client_model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": client_model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": client_model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": client_model,
            "ANTHROPIC_DEFAULT_FABLE_MODEL": client_model,
        }
    else:
        # 命令行自己拼 /v1/messages：地址以 /v1 结尾时先剥掉，否则变成 /v1/v1/messages。
        # 不以 /v1 结尾的地址原样返回，所以既有 provider 仍是字节级等价。
        from scripts.derive_endpoints import direct_anthropic_url
        env = {
            "ANTHROPIC_BASE_URL": direct_anthropic_url(provider["base_url"]),
            "ANTHROPIC_AUTH_TOKEN": provider["api_key"],
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
            "ANTHROPIC_DEFAULT_FABLE_MODEL": model,
        }
    ctx = _bound_context(provider, model)
    if ctx is not None:
        env[EXT_CONTEXT_ENV_KEY] = str(ctx)
    return env


def _prune_backups(settings_path: str) -> None:
    """裁掉 ``<base>.bak-<纯数字>`` 备份，只留最近 ``_BACKUPS_KEEP`` 份。

    只认**纯数字后缀**：一次性迁移快照 ``<base>.pre-protocol-migration.bak`` 与 desync 安装器
    的 ``.bak-desync-<ts>`` 因此都不参与裁剪（前者是必须永存的全量回退点，后者归各自机制管）。
    """
    d = os.path.dirname(settings_path)
    base = os.path.basename(settings_path) + ".bak-"
    try:
        baks = sorted(n for n in os.listdir(d)
                      if n.startswith(base) and n[len(base):].isdigit())
    except OSError:
        return
    for name in baks[:-_BACKUPS_KEEP] if len(baks) > _BACKUPS_KEEP else []:
        try:
            os.remove(os.path.join(d, name))
        except OSError:
            pass


def _write_settings(bot_id: str, mutate, top: dict | None = None) -> tuple[dict | None, str | None]:
    """settings.json 写入公共骨架：路径校验 → local 冲突 → 跨进程锁 → 备份 → 写 → 校验 → 回滚。

    锁等满 ``SETTINGS_LOCK_WAIT_SEC`` 拿不到 → ``(None, SETTINGS_BUSY_MESSAGE)``，什么都不写。
    mutate(existing_doc_env) 就地改 env，返回被写入的 7 键 dict（None 表示只需删除）。
    ``top``（批2，可省）：要写进 settings **顶层**的键值（思考强度 ``effortLevel``——不进 env）；
    缺省 None = 顶层不写；未列入 top 的既有顶层键一律不动（deepcopy 保留）。
    """
    _require_writable()
    real_dir, err = _settings_dir(bot_id)
    if err:
        return None, err

    settings_path = os.path.join(real_dir, ".claude", "settings.json")
    local_path = os.path.join(real_dir, ".claude", "settings.local.json")
    conflict = [k for k in PROVIDER_KEYS if k in _read_env(local_path)]
    if conflict:
        # 类型化抛出：路由按类型映射 409，不匹配文案（fn 层由 set/clear 转回字符串）
        raise LocalConflict(bot_id, conflict)

    return _under_settings_lock(
        settings_path, lambda: _write_settings_locked(bot_id, real_dir, settings_path, mutate, top))


def _settings_dir(bot_id: str | None, settings_path: str | None = None) -> tuple[str | None, str | None]:
    """settings.json 写口共用的路径校验：返回 ``(频道目录的 realpath, None)`` 或 ``(None, 文案)``。

    ``settings_path`` 不给 → 按 bot 定位（``_bot_dir``）；给了 → 取它的上两级（``<目录>/.claude/settings.json``，
    形状由调用方核对）。两种都要求 realpath 落在 ``CHANNELS_ROOT`` 内。
    """
    if settings_path is None:
        d = _bot_dir(bot_id)
        if d is None:
            # 既有两条错误文案保持：彻底未知的 bot / yml 存在但路径不可用
            if _bot_cfg(bot_id) is None:
                return None, "unknown bot %s" % bot_id
            return None, "bot 配置缺少可用的 bot_channel_path"
    else:
        d = os.path.dirname(os.path.dirname(settings_path))
    real_dir = os.path.realpath(d)
    if not _within_channels_root(real_dir):
        return None, "bot 目录越界，拒绝写入"
    return real_dir, None


def _under_settings_lock(settings_path: str, locked):
    """在 ``<频道目录>/.claude/settings.json.lock`` 的跨进程 flock 里调 ``locked()``，返回它的结果（INTERFACE 3.8.3）。

    读改写整段在锁里，命令行与壳同时写同一只也不丢改动；进程内的 bot_lock 挡不住别的进程。
    .claude 目录不在就先建（原来写 settings.json 前也会建）。等满 ``SETTINGS_LOCK_WAIT_SEC`` 拿不到 →
    ``(None, SETTINGS_BUSY_MESSAGE)``；锁文件建不出来（目录只读）或 flock 出错 → 按写失败处理。
    """
    try:
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        lock_fd = os.open(settings_path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        return None, "写入校验失败，已回滚"
    try:
        # T03 跨平台锁：POSIX 用 flock、Windows 用 msvcrt；锁文件 0600 且不删。
        with file_lock(lock_fd, timeout=SETTINGS_LOCK_WAIT_SEC, poll=SETTINGS_LOCK_RETRY_SEC):
            return locked()
    except LockBusy:
        return None, SETTINGS_BUSY_MESSAGE
    except (LockError, OSError):
        return None, "写入校验失败，已回滚"
    finally:
        os.close(lock_fd)   # 关掉即放锁；锁文件不删（删了会让正在等的进程与后来者锁到两个不同的文件上）


def _load_settings_for_write(settings_path: str) -> tuple[dict | None, str | None]:
    """锁内现读 settings.json：不在 → ``({}, None)``；不是 JSON 对象 → ``(None, 解析失败文案)``。别的 OSError 照旧抛出。"""
    try:
        with open(settings_path, encoding="utf-8") as f:
            existing = json.load(f)
        if not isinstance(existing, dict):
            raise ValueError
    except FileNotFoundError:
        return {}, None
    except ValueError:
        return None, "bot settings.json 解析失败，已中止"
    return existing, None


_BACKUP_NAME_TRIES = 1000


def _new_backup(src_path: str) -> str:
    """把 ``src_path`` 逐字节复制成 ``<src>.bak-<纳秒>`` 并返回路径：创建即 0600、``O_EXCL`` 新建，撞名就把数字加一再试，
    **绝不删除任何已有文件**（``_open_private`` 撞名先删再建，用在备份上会让同一秒的第二次写删掉第一次写前的原状）。
    名字仍是纯数字（``_prune_backups`` 照认）；纳秒的前 10 位就是秒，与原先按秒命名的旧备份按名字排序时新的仍排在后面。
    复制失败只删本次新建的半截文件，异常照抛。"""
    with open(src_path, "rb") as src:
        stamp = time.time_ns()
        for _ in range(_BACKUP_NAME_TRIES):
            path = "%s.bak-%d" % (src_path, stamp)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                break
            except FileExistsError:
                stamp += 1
        else:
            raise OSError("备份名一直撞名: %s" % src_path)
        try:
            with os.fdopen(fd, "wb") as dst:
                shutil.copyfileobj(src, dst)
                dst.flush()
                os.fsync(dst.fileno())
        except BaseException:
            _cleanup(path)
            raise
    return path


def _backup_settings(settings_path: str) -> tuple[str | None, str | None]:
    """写前备份 ``settings.json.bak-<纳秒>``（``_new_backup``），只留 ``_BACKUPS_KEEP`` 份。原文件不在 → ``(None, None)``；备份失败 → ``(None, 文案)``。"""
    if not os.path.exists(settings_path):
        return None, None
    try:
        backup_path = _new_backup(settings_path)   # 创建即 0600，不留 0644 明文 key 窗口；同一秒写两次也各留一份
        _prune_backups(settings_path)
    except OSError:
        return None, "写入校验失败，已回滚"
    return backup_path, None


def _replace_settings(settings_path: str, backup_path: str | None, existing: dict, out: dict,
                      written: dict | None, snapshot: dict) -> str | None:
    """把 ``out`` 写成新的 settings.json：同目录 0600 临时文件 → 改名 → chmod → 校验。成功回 None；失败回滚到写前、回文案。"""
    tmp = settings_path + ".tmp-%d" % os.getpid()
    try:
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
        fd = _open_private(tmp)   # 先建成 0600 再写：明文 key 不得先落在 0644 inode 上
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, settings_path)
    except OSError:
        _cleanup(tmp)
        return "写入校验失败，已回滚"

    if not _chmod_600(settings_path):
        _restore(settings_path, backup_path, existing)
        return "写入校验失败，已回滚"

    if not _verify(settings_path, written, snapshot):
        _restore(settings_path, backup_path, existing)
        return "写入校验失败，已回滚"
    return None


def _write_settings_locked(bot_id: str, real_dir: str, settings_path: str, mutate,
                           top: dict | None) -> tuple[dict | None, str | None]:
    """``_write_settings`` 的后半段，只在 settings.json.lock 里调：锁内现读 → 备份 → 写 → 校验 → 回滚。"""
    existing, err = _load_settings_for_write(settings_path)
    if err:
        return None, err

    snapshot = {"enabledPlugins": existing.get("enabledPlugins"),
                "hooks": existing.get("hooks"),
                "top": dict(top or {})}
    original_env = dict(existing.get("env") or {})

    backup_path, err = _backup_settings(settings_path)
    if err:
        return None, err

    out = copy.deepcopy(existing)
    env = dict(existing.get("env") or {})
    written = mutate(env)
    if env:
        out["env"] = env
    else:
        out.pop("env", None)
    if top:
        out.update(top)   # 顶层键（effortLevel）：不参与扩展键清理，缺省不动

    err = _replace_settings(settings_path, backup_path, existing, out, written, snapshot)
    if err:
        return None, err

    env_removed = sorted(k for k in original_env if k not in env)
    _LAST_SNAPSHOT[bot_id] = snapshot
    return {
        "bot_id": bot_id,
        "bot_dir": real_dir,
        "settings_path": settings_path,
        "env_written": {k: _mask(v) for k, v in (written or {}).items()},
        "backup_path": backup_path,
        "env_removed": env_removed,
    }, None


# ── 技能显示设置：settings.json 顶层的 skillOverrides（PLAN-bot-features D3；INTERFACE 3.4.2）──────────────

SKILL_OVERRIDES_KEY = "skillOverrides"
SKILL_OFF = "off"


def update_skill_overrides(bot_id: str | None, changes: dict,
                           settings_path: str | None = None) -> tuple[dict | None, str | None]:
    """改 settings.json 顶层 ``skillOverrides`` 里给定的几个键，别的一概不动。

    ``changes``：``{键: "off" 或 None}``。``"off"`` → 这个键写成 ``"off"``（技能停用）；``None`` → 删掉这个键（回到缺省可见）。
    开关只会写 ``"off"`` 或删键（D3），别的值、非字符串键、空键一律 ValueError，什么都不写。
    ``skillOverrides`` 里没列的键、settings.json 其余顶层键的解析值都不变；改完 ``skillOverrides`` 变空就整键删掉。
    结果与现状相同时不备份、不写，文件逐字节不变（幂等，可反复对齐）。

    定位：``settings_path`` 不给时按 ``bot_id`` 找（与换供应商同一取法）；给了就写它，用于还没登记的 bot（新建时的暂存目录），
    这时 ``bot_id`` 可为 None。``settings_path`` 必须是 ``<目录>/.claude/settings.json`` 的绝对路径（否则 ValueError），
    目录的 realpath 须在频道根内。

    与换供应商共用写入骨架：``settings.json.lock`` 跨进程锁里现读、现改、现写，写前备份（只留 2 份）、写后校验、失败回滚。
    不查 settings.local.json 的供应商键冲突：那条只管供应商写入。

    返回 ``(结果, None)``，结果 ``{"changed": [真改了的键，顺序同 changes], "settings_path", "backup_path"}``（没写盘时
    ``changed`` 为空、``backup_path`` 为 None）；失败 ``(None, 文案)``：3 秒拿不到锁时文案恰为 ``SETTINGS_BUSY_MESSAGE``，
    写不进、解析失败、``skillOverrides`` 不是对象、越界、没有这只 bot 都是别的文案。调用方按"是不是 SETTINGS_BUSY_MESSAGE"
    区分锁被占与写失败。接缝只读时与别的写口一样抛 ``SeamsNotConfigured``。
    """
    if not isinstance(changes, dict):
        raise ValueError("changes 必须是对象")
    for key, value in changes.items():
        if not isinstance(key, str) or not key:
            raise ValueError("技能键必须是非空字符串")
        if value is not None and value != SKILL_OFF:
            raise ValueError("技能键 %s 的值只能是 %r 或 None" % (key, SKILL_OFF))
    if settings_path is None:
        if not isinstance(bot_id, str) or not bot_id:
            raise ValueError("要给 bot_id 或 settings_path")
    elif not (isinstance(settings_path, str) and os.path.isabs(settings_path)
              and os.path.basename(settings_path) == "settings.json"
              and os.path.basename(os.path.dirname(settings_path)) == ".claude"):
        raise ValueError("settings_path 必须是 <目录>/.claude/settings.json 的绝对路径")
    _require_writable()
    real_dir, err = _settings_dir(bot_id, settings_path)
    if err:
        return None, err
    path = os.path.join(real_dir, ".claude", "settings.json")
    return _under_settings_lock(path, lambda: _update_skill_overrides_locked(path, changes))


def _update_skill_overrides_locked(settings_path: str, changes: dict) -> tuple[dict | None, str | None]:
    """``update_skill_overrides`` 的锁内部分：现读 → 算 → 与现状相同就返回 → 备份 → 写 → 校验 → 回滚。"""
    try:
        existing, err = _load_settings_for_write(settings_path)
    except OSError:   # 读不了（没权限、路径是目录等）：按写不进处理，不把异常抛给调用方
        return None, "写入校验失败，已回滚"
    if err:
        return None, err
    current = existing.get(SKILL_OVERRIDES_KEY)
    if current is not None and not isinstance(current, dict):
        return None, "bot settings.json 的 skillOverrides 不是对象，已中止"   # 不覆盖别人写的怪形状

    overrides = dict(current or {})
    changed = []
    for key, value in changes.items():
        if value is None and key in overrides:
            del overrides[key]
        elif value is not None and overrides.get(key) != value:
            overrides[key] = value
        else:
            continue
        changed.append(key)
    result = {"changed": changed, "settings_path": settings_path, "backup_path": None}
    if not changed:
        return result, None

    backup_path, err = _backup_settings(settings_path)
    if err:
        return None, err
    out = copy.deepcopy(existing)
    if overrides:
        out[SKILL_OVERRIDES_KEY] = overrides
    else:
        out.pop(SKILL_OVERRIDES_KEY, None)
    # 写后校验：skillOverrides 恰为算出来的值（删掉时读回 None），enabledPlugins、hooks 与写前相同
    snapshot = {"enabledPlugins": existing.get("enabledPlugins"), "hooks": existing.get("hooks"),
                "top": {SKILL_OVERRIDES_KEY: overrides or None}}
    err = _replace_settings(settings_path, backup_path, existing, out, None, snapshot)
    if err:
        return None, err
    result["backup_path"] = backup_path
    return result, None


def _open_private(path: str) -> int:
    """以 0600 独占创建临时文件并返回 fd（两份落盘骨架共用）。

    **必须在写入任何字节之前就把模式钉成 0600**：默认 umask 下 ``open()`` 会先建成
    0644，明文 api_key／settings 内容会在 ``chmod`` 之前就落在一个组/其他用户可读的
    inode 上——那道窗口每次保存都开着。``O_EXCL`` 顺带挡住残留 tmp（同 pid 的崩溃现场）：
    先清掉旧文件再重建，保证新 inode 的权限来自本参数而非继承。
    """
    for _ in range(2):
        try:
            return os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            _cleanup(path)
    raise OSError("无法创建私有临时文件: %s" % path)


def _cleanup(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _chmod_600(path: str) -> bool:
    for _ in range(2):  # chmod 失败重试一次
        try:
            os.chmod(path, 0o600)
            return True
        except OSError:
            continue
    return False


def _verify(settings_path: str, written: dict, snapshot: dict) -> bool:
    try:
        with open(settings_path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return False
    env = doc.get("env") or {}
    for k, v in (written or {}).items():
        if env.get(k) != v:
            return False
    for k, v in (snapshot.get("top") or {}).items():   # 顶层键（effortLevel）同轮校验
        if doc.get(k) != v:
            return False
    return (doc.get("enabledPlugins") == snapshot["enabledPlugins"]
            and doc.get("hooks") == snapshot["hooks"])


def _restore(settings_path: str, backup_path: str | None, fallback: dict) -> None:
    if backup_path and os.path.exists(backup_path):
        try:
            os.replace(backup_path, settings_path)
            return
        except OSError:
            pass
    # 兜底内容 = 写入前的文档，**可能含 ANTHROPIC_AUTH_TOKEN**（bot 早已绑过 provider 时）。
    # 与主写入路径同权：先建成 0600 的 tmp 再原子替换，绝不让明文 key 以默认 umask(0644)
    # 落在一个新建 inode 上（与 I1 同类）。裸 open(...,"w") 在文件不存在时会以 0644 建出含
    # key 的文件；改走 _open_private 后无论目标在否都只可能是 0600。
    tmp = settings_path + ".restore-%d" % os.getpid()
    try:
        fd = _open_private(tmp)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(fallback, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, settings_path)
    except OSError:
        _cleanup(tmp)


def _undo_settings_write(result: dict | None) -> None:
    """绑定登记失败时把 settings.json 退回写入前状态（E-11 第三条路径）。

    ``_write_settings`` 成功时返回的 result 里带本次写入前的备份路径：
    有备份 → 用 ``os.replace`` 还原；无备份（写入前文件本不存在）→ 删掉本次新建的文件。
    两条路都失败只留 stderr 线索——**绝不**在还原失败时再假装成功。
    """
    path = (result or {}).get("settings_path")
    if not path:
        return
    backup = (result or {}).get("backup_path")
    if backup:
        if not os.path.exists(backup):
            print("[provider_config] 绑定登记失败但 settings 备份缺失，未回滚: %s" % path, file=sys.stderr)
            return
        try:
            os.replace(backup, path)
        except OSError as e:
            print("[provider_config] 绑定登记失败后 settings 回滚失败: %s" % e, file=sys.stderr)
        return
    # 写入前文件不存在（无备份）：本次写入是新建 → 删除即回到原状。
    try:
        os.remove(path)
    except OSError as e:
        print("[provider_config] 绑定登记失败后 settings 删除失败: %s" % e, file=sys.stderr)


def set_bot_provider(bot_id: str, provider: dict, model: str) -> tuple[dict | None, str | None]:
    """把 provider 的 7 键写进该 bot 的项目级 settings.json，并登记 binding。"""
    _require_writable()
    if bot_id not in _discovered_bot_ids():
        return None, "unknown bot %s" % bot_id
    if not isinstance(model, str) or not (1 <= len(model) <= 200):
        return None, "model 必须是长度 1..200 的字符串"
    if (not isinstance(provider, dict) or not isinstance(provider.get("base_url"), str)
            or not provider.get("base_url")
            or not isinstance(provider.get("api_key"), str) or not provider.get("api_key")):
        return None, "provider 数据不完整"

    resolved = resolve_env(bot_id, provider, model)
    if provider.get("proxy") is True:
        # 汇口第 0 步：在清单锁内确保 key 并取回**最终落盘值**（key 落盘后不可变，I2），
        # 再用它写 env——并发首绑时各 bot 的 token 都等于清单里那一个 key（勘误 5）。
        resolved["ANTHROPIC_AUTH_TOKEN"] = ensure_instance_key()

    def _mutate(env):
        # 清理语义（§4）：先 pop 全部扩展键，再按新推导写——换绑到无 ctx 模型时旧扩展键不残留
        for k in _EXT_ENV_KEYS:
            env.pop(k, None)
        env.update(resolved)
        return resolved

    with bot_lock(bot_id):
        # 思考强度（§3/§4）：写条件 = 绑定记录有合法 effort；无 effort 时不动顶层该键
        effort = (get_binding_raw(bot_id) or {}).get("effort")
        top = {EFFORT_KEY: effort} if effort in EFFORT_LEVELS else None
        try:
            result, err = _write_settings(bot_id, _mutate, top)
        except LocalConflict as e:
            return None, str(e)   # 契约：函数层仍返回文案（HTTP 层按类型映射 409）
        if err:
            return None, err
        try:
            _set_binding(bot_id, provider, model, pending=True)
        except Exception:
            # 登记失败 → settings.json 恢复成备份（E-11）：否则用户看到“保存失败”，
            # 但那只 bot 的 settings.json 已被写入新 env，重启后半生效。
            _undo_settings_write(result)
            raise
        return result, None


def clear_bot_provider(bot_id: str) -> tuple[bool, str | None]:
    """从 env 删 7 键 + 扩展键（其余键不动；顶层 effortLevel 不参与清理）；删后 env 为空 → 整个删掉 env 键（J7）。"""
    _require_writable()
    if bot_id not in _discovered_bot_ids():
        return False, "unknown bot %s" % bot_id

    def _mutate(env):
        for k in ALLOWED_ENV_KEYS + _EXT_ENV_KEYS:
            env.pop(k, None)
        return None

    with bot_lock(bot_id):
        try:
            result, err = _write_settings(bot_id, _mutate)
        except LocalConflict as e:
            return False, str(e)   # 契约：函数层仍返回文案（HTTP 层按类型映射 409）
        if err:
            return False, err
        _clear_binding(bot_id)
        return True, None


def _set_binding(bot_id: str, provider: dict, model: str, pending: bool, *, stale: bool = True) -> None:
    """全量重写这只 bot 的绑定记录。``stale=False``：不写 thinking 哨兵（从归档恢复时供应商没变，见 rebind_from_manifest）。"""
    with _manifest_lock():
        data = load_providers()
        old = (data.get("bindings") or {}).get(bot_id) or {}
        changed = (old.get("base_url") != provider.get("base_url")
                   or old.get("model") != model
                   or old.get("key_fp") != _key_fp(provider.get("api_key", "")) )
        new_bd = {
            "provider_id": provider.get("id"),
            "model": model,
            "base_url": provider.get("base_url"),
            "key_fp": _key_fp(provider.get("api_key", "")),
            "applied_at": old.get("applied_at"),
            "pending_restart": bool(pending),
            "pending_since": int(time.time()) if pending else None,
            "pending_desired": {"provider_id": provider.get("id"), "model": model},
            "forced_at": None,
            "last_error": old.get("last_error"),
        }
        if "effort" in old:
            # effort 保活（§3）：全量重写时保留既有值——换供应商/换模型不得把思考强度丢掉。
            # 沿用字段级写入的新值（若本轮有 set_bot_effort 先落过）。
            new_bd["effort"] = old["effort"]
        data.setdefault("bindings", {})[bot_id] = new_bd
        _commit_locked(data)
    if changed and stale:
        mark_thinking_stale(bot_id)


def _clear_binding(bot_id: str) -> None:
    with _manifest_lock():
        data = load_providers()
        bd = (data.get("bindings") or {}).get(bot_id)
        if bd:
            mark_thinking_stale(bot_id)
        data.setdefault("bindings", {}).pop(bot_id, None)
        _commit_locked(data)


# ── 归档、删除时的解绑与重绑（INTERFACE-bot-lifecycle 3.5.2、3.5.3、3.7 第 8 条；PLAN 2.1）──────────────
# 只动 providers.json 的 bindings.<bot> 与 provider-notices.json 的 <bot> 两处记录：settings.json 随频道目录走，
# 不改、不重启、不排待重启。与 _clear_binding / _set_binding 同一把清单锁、同一个落盘汇口；解绑不调 _clear_binding，
# 因为它会往这只 bot 正要归档的会话目录里写 thinking 哨兵，落盘时还可能补生成代理 key，提交点之前撤回就复原不了字节。

def _insert_at(d: dict, index: int, key, value) -> dict:
    items = list(d.items())
    items.insert(max(0, min(index, len(items))), (key, value))
    return dict(items)


def unbind_for_archive(bot_id: str) -> dict:
    """去掉这只 bot 的绑定记录与提示关闭记录（没有就不写盘）。返回原样快照，只在调用方内存里用于提交点之前的
    逐字节撤回（``rebind_from_manifest(bot_id, ..., snapshot=)``）；快照里只有绑定记录（指纹，不含密钥）与提示标识。
    清单读不出（ProvidersCorrupt）、锁等不到、写不进照常抛出，什么都没改。"""
    _require_writable()
    snap = {"binding": None, "notice": None}
    with _manifest_lock():
        data = load_providers()
        bindings = data["bindings"]
        if bot_id in bindings:
            snap["binding"] = (list(bindings).index(bot_id), copy.deepcopy(bindings[bot_id]))
            del bindings[bot_id]
            _commit_locked(data, ensure_key=False)
    with _NOTICES_LOCK:
        dismissed = load_dismissed()
        if bot_id in dismissed:
            snap["notice"] = (list(dismissed).index(bot_id), dismissed.pop(bot_id))
            _write_notices_locked(dismissed)
    return snap


def rebind_from_manifest(bot_id: str, provider_binding, *, snapshot=None):
    """重新绑定，不重启、不排待重启、不写 thinking 哨兵。这只 bot 已有绑定记录就不动它。

    - 给了 ``snapshot``（同一进程里提交点之前撤回）：原条目放回原位置，提示关闭记录同样放回；两份文件在此期间
      没被别人改过时逐字节复原。
    - 没给（从归档恢复）：按清单的 ``provider_id``、``model``、``effort`` 经 ``_set_binding`` 重新登记。

    返回 None（已复原或本来就不用绑），或一句给人看的原因（供应商已经删掉、清单里的值不对：没有绑，用默认设置）。"""
    _require_writable()
    if snapshot is not None:
        binding, notice = snapshot.get("binding"), snapshot.get("notice")
        if binding is not None:
            with _manifest_lock():
                data = load_providers()
                if bot_id not in data["bindings"]:
                    data["bindings"] = _insert_at(data["bindings"], binding[0], bot_id, copy.deepcopy(binding[1]))
                    _commit_locked(data, ensure_key=False)
        if notice is not None:
            with _NOTICES_LOCK:
                dismissed = load_dismissed()
                if bot_id not in dismissed:
                    _write_notices_locked(_insert_at(dismissed, notice[0], bot_id, notice[1]))
        return None
    if not isinstance(provider_binding, dict) or not provider_binding.get("provider_id"):
        return None
    if get_binding_raw(bot_id):
        return None
    provider_id, model = provider_binding.get("provider_id"), provider_binding.get("model")
    provider = _find(load_providers(), provider_id)
    if provider is None:
        return "原来的供应商（%s）已经删掉了，没有重新绑定，用默认设置" % provider_id
    if not isinstance(model, str) or not (1 <= len(model) <= 200):
        return "清单里记的模型不对，没有重新绑定，用默认设置"
    _set_binding(bot_id, provider, model, pending=False, stale=False)
    effort = provider_binding.get("effort")
    if effort in EFFORT_LEVELS:
        _set_effort_field(bot_id, effort)
    return None


def _set_effort_field(bot_id: str, effort: str) -> bool:
    """字段级写 ``bindings[bot].effort``（§3）：不动 ``_set_binding`` 的全量路径——
    不重置 applied_at/pending，也不误触 thinking 哨兵。binding 不存在 → False（不动清单）。"""
    with _manifest_lock():
        data = load_providers()
        bd = (data.get("bindings") or {}).get(bot_id)
        if not isinstance(bd, dict):
            return False
        bd["effort"] = effort
        _commit_locked(data)
    return True


def set_bot_effort(bot_id: str, effort: str) -> tuple[bool | None, str | None]:
    """设置某 bot 的思考强度（批2 调用面；INTERFACE §3/§4）：

    白名单校验（``low|medium|high|xhigh|max``，非法档位零写入）→ 写该 bot settings **顶层**
    ``effortLevel``（不进 env；该键不参与扩展键清理）→ 字段级记 ``bindings[bot].effort``
    （不动 ``_set_binding`` 全量路径）→ 走既有排队重启链（``mark_pending``）。

    返回 ``(True, None)`` 或 ``(None, 错误文案)``。绑定路径上的"无 effort 值时不动该键"
    语义见 ``set_bot_provider`` / ``_sync_one_bot_settings``；本函数要求显式档位。
    未绑定供应商时也允许写 settings 顶层（INTERFACE §2.7.4 的独立入口）；
    此时没有可记账的 binding，只落 settings、不排重启。
    """
    _require_writable()
    if effort not in EFFORT_LEVELS:
        return None, "思考强度只支持 low、medium、high、xhigh、max 五档"
    bd = get_binding_raw(bot_id)
    bound = isinstance(bd, dict) and bool(bd.get("provider_id"))
    with bot_lock(bot_id):
        try:
            result, err = _write_settings(bot_id, lambda env: None, {EFFORT_KEY: effort})
        except LocalConflict as e:
            return None, str(e)
        if err:
            return None, err
        if not bound:
            return True, None
        try:
            _set_effort_field(bot_id, effort)
        except Exception:
            _undo_settings_write(result)   # 同 set_bot_provider：记账失败不留半生效的 settings
            raise
        mark_pending(bot_id, {"provider_id": bd.get("provider_id"), "model": bd.get("model")})
        return True, None


def _sync_one_bot_settings(bot_id: str, entry: dict, model: str, effort) -> bool:
    """级联单个 bot（§4）：resolve_env（含扩展键）与 effortLevel 与现值**全等 → 跳过**
    （不建备份、不写 mtime）；有变化才写。返回是否写了盘。"""
    desired = resolve_env(bot_id, entry, model)
    if entry.get("proxy") is True:
        desired["ANTHROPIC_AUTH_TOKEN"] = ensure_instance_key()
    want_effort = effort if effort in EFFORT_LEVELS else None
    with bot_lock(bot_id):
        doc = _read_settings_doc(bot_settings_path(bot_id)) or {}
        cur_env = _env_of(doc)
        if all(cur_env.get(k) == desired.get(k) for k in ALLOWED_ENV_KEYS + _EXT_ENV_KEYS) \
                and (want_effort is None or doc.get("effortLevel") == want_effort):
            return False

        def _mutate(env):
            for k in _EXT_ENV_KEYS:
                env.pop(k, None)   # 先 pop 扩展键，再按新推导写（§4 清理语义）
            env.update(desired)
            return desired

        top = {EFFORT_KEY: want_effort} if want_effort else None
        _result, err = _write_settings(bot_id, _mutate, top)
        if err:
            raise RuntimeError(err)
        return True


def _cascade_provider_change(provider_id: str) -> list[str]:
    """provider 落盘变化后的级联（批2；§4 生效链）：对**所有绑定该 provider 的 bot**
    重算 env（含扩展键）与 effortLevel——有变化才重写 settings 并走**既有排队重启链**
    （``mark_pending``，与"切了之后自动重启 worker"同一条链）。

    单个 bot 写失败**不得阻断其它 bot**（逐个 try）；清单读坏/条目已不存在 → 不动作。
    返回被处理（写盘完成或确认无需写盘）的 bot 列表。
    """
    try:
        data = load_providers()
    except ProvidersCorrupt:
        return []
    entry = _find(data, provider_id)
    if entry is None:
        return []
    done = []
    for bot in bound_bots(provider_id, data):
        bd = (data.get("bindings") or {}).get(bot) or {}
        model = bd.get("model")
        if not isinstance(model, str) or not model:
            continue
        try:
            changed = _sync_one_bot_settings(bot, entry, model, bd.get("effort"))
        except Exception:
            continue
        if changed:
            try:
                mark_pending(bot, {"provider_id": provider_id, "model": model})
            except Exception:
                continue
        done.append(bot)
    return done


# ── 读取 / 比对 ────────────────────────────────────────────────────────────

def effective_provider(bot_id: str) -> dict:
    """按 local → project → global 解析生效层（不抛异常）。"""
    data = _load_providers_safe()
    binding = (data.get("bindings") or {}).get(bot_id) or None
    local_env = _read_env(bot_local_path(bot_id))
    proj_env = _read_env(bot_settings_path(bot_id))
    glob_doc = _read_settings_doc(GLOBAL_SETTINGS_PATH)
    glob_env = _env_of(glob_doc)

    source = "unknown"
    chosen = {}
    for name, env in (("local", local_env), ("bot", proj_env), ("global", glob_env)):
        if env.get("ANTHROPIC_BASE_URL"):
            source, chosen = name, env
            break
    # rev6 P11：三层都没有 ANTHROPIC_BASE_URL 且全局 settings.json 可读 ⇒ 官方订阅（钥匙串登录）。
    # 模型只取全局的 ANTHROPIC_MODEL；全局文件缺失/坏 JSON 仍走原有 unknown/unmanaged，不新增分支。
    # 三层任一有地址的判定一字不变（chosen 为空 ⇒ base_url / key_masked 自然为 None）。
    subscription = source == "unknown" and glob_doc is not None
    if subscription:
        source = "subscription"

    extra = sorted({k for k in list(local_env) + list(proj_env)
                    if k.startswith("ANTHROPIC_") and k not in ALLOWED_ENV_KEYS})
    conflict = local_provider_keys(bot_id)

    token = chosen.get("ANTHROPIC_AUTH_TOKEN") or ""
    # in_sync 与 _set_binding 的「什么算变化」同口径：同一组 (base_url, model, key_fp) 三元组。
    # 只比 key 指纹时，PUT 改过 provider 的 base_url/key 也不标 drifted —— 面板仍报
    # applied/in_sync，而 bot 的 settings.json 与运行中的 worker 还是旧地址（假成功）。
    # provider 被删（entry 为 None）同样判不同步 → state 落 drifted。
    entry = _find(data, binding.get("provider_id")) if binding else None
    # 9.9：应写三键与写 env 同一来源（resolve_env，含 proxy=true 分叉）；只算不写盘。
    # proxy=true：端口 ≤0 恒 false；实例 key 缺失（want token 为空）为 false。
    in_sync = False
    if binding and entry and token:
        import provider_proxy
        want = resolve_env(bot_id, entry, binding.get("model"))
        in_sync = bool(
            want["ANTHROPIC_AUTH_TOKEN"]
            and _key_fp(token) == _key_fp(want["ANTHROPIC_AUTH_TOKEN"])
            and chosen.get("ANTHROPIC_BASE_URL") == want["ANTHROPIC_BASE_URL"]
            and chosen.get("ANTHROPIC_MODEL") == want["ANTHROPIC_MODEL"]
            and not (entry.get("proxy") is True and provider_proxy.proxy_port() <= 0))

    try:
        stopped = _bot_stopped(bot_id)
    except Exception:
        stopped = False
    alive = worker_alive(bot_id)
    pending = bool(binding and binding.get("pending_restart"))

    if stopped:
        state = "stopped"
    elif pending and binding.get("halted") is True:
        state = "restart_halted"  # 自动重启已停止（试满 5 次或根本无法重启），原因看 last_error
    elif pending:
        state = "pending"
    elif conflict:
        state = "local_override"
    elif binding and not in_sync:
        state = "drifted"
    elif binding and not alive:
        # E-25.2：worker 会话不在 ≠ 故障 —— 绑定在、配置一致（in_sync）、只是没有活会话，
        # 是"空闲"健康态（面板按中性提示渲染）。真拉起失败的走 pending / restart_halted。
        state = "worker_idle"
    elif not binding:
        state = "subscription" if subscription else "unmanaged"
    else:
        state = "applied"

    return {
        "bot_id": bot_id,
        "desired": {
            "source": source,
            "base_url": chosen.get("ANTHROPIC_BASE_URL"),
            "model": ((glob_env.get("ANTHROPIC_MODEL") or None) if subscription
                      else chosen.get("ANTHROPIC_MODEL")),
            "key_masked": _mask(chosen.get("ANTHROPIC_AUTH_TOKEN") or "") or None,
            "extra_keys": extra,
        },
        "local_conflict": conflict,
        "pending_restart": pending,
        "pending_since": binding.get("pending_since") if binding else None,
        "applied_at": binding.get("applied_at") if binding else None,
        "in_sync": in_sync,
        "worker_alive": alive,
        "state": state,
    }


def binding_status(bot_id: str) -> dict:
    data = _load_providers_safe()
    binding = (data.get("bindings") or {}).get(bot_id) or None
    known = bot_id in _discovered_bot_ids()
    if not known and not binding:
        return {"bot_id": bot_id, "bound": False, "binding": None,
                "provider_exists": False, "effective": None}
    if not binding:
        return {"bot_id": bot_id, "bound": False, "binding": None,
                "provider_exists": False, "effective": effective_provider(bot_id)}
    entry = _find(data, binding.get("provider_id"))
    return {
        "bot_id": bot_id,
        "bound": True,
        "binding": {
            "provider_id": binding.get("provider_id"),
            "provider_name": (entry or {}).get("name"),
            "model": binding.get("model"),
            "base_url": binding.get("base_url"),
            "key_fp": binding.get("key_fp"),
            "applied_at": binding.get("applied_at"),
            "pending_restart": bool(binding.get("pending_restart")),
            "pending_since": binding.get("pending_since"),
            "forced_at": binding.get("forced_at"),
            **_backoff_view(binding),
        },
        "provider_exists": entry is not None,
        "effective": effective_provider(bot_id),
    }


def _bot_stopped(bot_id: str) -> bool:
    """停用判定唯一来源：``bots_registry.is_enabled``（yml enabled + 旧标记兼容）。"""
    cfg = _bot_cfg(bot_id)
    return cfg is not None and not bot_registry.is_enabled(cfg, bot_id)


def list_bots_status() -> list[dict]:
    out = []
    by_id = {b["id"]: b for b in bot_registry.bots(include_disabled=True)}
    for bot_id in sorted(_discovered_bot_ids()):
        item = by_id.get(bot_id) or {}
        cfg = _bot_cfg(bot_id)
        out.append({
            "bot_id": bot_id,
            "display_name": item.get("display_name") or bot_id,
            "has_config": cfg is not None,
            "bot_dir": _bot_dir(bot_id),
            "worker_session": "tg-%s-worker" % bot_id,
            "worker_alive": worker_alive(bot_id),
            "stopped": _bot_stopped(bot_id),
            "effective": effective_provider(bot_id),
        })
    return out


def worker_alive(bot_id: str) -> bool:
    """worker 是否在跑：只问 dispatcher 本机 HTTP（T13 去 tmux），不可达按未跑处理。"""
    try:
        from adapters import claude_proc
        return bool(claude_proc.polling(bot_id))
    except Exception:
        return False


# ── 待重启标记（Z1，持久在 providers.json）─────────────────────────────────

FORCE_AFTER_SEC = 1800
# 拉起失败的退避：60 秒起翻倍、封顶 480 秒（5 次上限下能达到的最大延迟），满 5 次停止自动重试
RESTART_BACKOFF_BASE_SEC = 60
RESTART_BACKOFF_CAP_SEC = 480
RESTART_MAX_FAILURES = 5
_LAST_ERROR_MAX = 200  # last_error 会进清单、经 API 返回并显示在横幅上
_HALT_DEFAULT_REASON = "自动重启已停止"


def _backoff_view(bd: dict) -> dict:
    """读入口对退避五键的统一归一（脏值一律取缺省，不抛）。"""
    fc = bd.get("fail_count")
    nra = bd.get("queued_since"), bd.get("next_retry_at")
    le = bd.get("last_error")
    return {
        "fail_count": fc if isinstance(fc, int) and not isinstance(fc, bool) and fc >= 0 else 0,
        "next_retry_at": nra[1] if isinstance(nra[1], (int, float)) and not isinstance(nra[1], bool) else None,
        "halted": bd.get("halted") is True,
        "last_error": le if isinstance(le, str) else None,
        "queued_since": nra[0] if isinstance(nra[0], (int, float)) and not isinstance(nra[0], bool) else None,
    }


def _reset_backoff(bd: dict, *, fail_count: bool = True) -> None:
    if fail_count:
        bd["fail_count"] = 0
    bd["halted"] = False
    bd["next_retry_at"] = None
    bd["queued_since"] = None


def _pending_binding(data: dict, bot_id) -> dict | None:
    """待重启记账入口共用：bot_id 合法、清单里有 binding 且 pending_restart 为真才返回它。"""
    if not isinstance(bot_id, str) or not bot_id:
        return None
    bd = (data.get("bindings") or {}).get(bot_id)
    if not isinstance(bd, dict) or not bd.get("pending_restart"):
        return None
    return bd


def _check_now_arg(now):
    """now 非 None 时的合法性同 retry_backoff（TypeError / ValueError），None → 当前时间。"""
    if now is None:
        return time.time()
    retry_backoff.is_due(None, now)  # 只借它校验 now
    return now


def mark_pending(bot_id: str, desired: dict) -> None:
    _require_writable()
    with _manifest_lock():
        data = load_providers()
        bd = data.setdefault("bindings", {}).setdefault(bot_id, {})
        bd["pending_restart"] = True
        bd["pending_since"] = int(time.time())  # 已在排队的项也刷新（现有行为，写死）
        bd["pending_desired"] = desired
        _reset_backoff(bd)
        _commit_locked(data, ensure_key=False)


def clear_pending(bot_id: str) -> None:
    # 六个记账入口统一顺序：先校验 bot_id（非法 → 无操作、不抛），再过只读闸，再进锁
    if not isinstance(bot_id, str) or not bot_id:
        return
    _require_writable()
    with _manifest_lock():
        data = load_providers()
        bd = (data.get("bindings") or {}).get(bot_id)
        if not isinstance(bd, dict):
            return
        bd["pending_restart"] = False
        bd["pending_since"] = None
        _reset_backoff(bd, fail_count=False)
        _commit_locked(data, ensure_key=False)


def list_pending() -> list[dict]:
    data = _load_providers_safe()
    out = []
    for bot_id, bd in (data.get("bindings") or {}).items():
        if not isinstance(bd, dict) or not bd.get("pending_restart"):
            continue
        desired = bd.get("pending_desired") or {
            "provider_id": bd.get("provider_id"), "model": bd.get("model")}
        out.append({
            "bot_id": bot_id,
            "pending_since": bd.get("pending_since"),
            "desired": desired,
            "force_after_sec": FORCE_AFTER_SEC,
            "forced_at": bd.get("forced_at"),
            **_backoff_view(bd),
        })
    out.sort(key=lambda e: (e["pending_since"] or 0))
    return out


# ── 提示条的「已关闭」记录（INTERFACE-provider-ux §3）──────────────────────────
# 记在清单同目录的独立小文件里，不写清单：关一条提示不该改动清单的字节与修改时间
# （核对脚本的指纹、收敛器的判定都看清单）。每只 bot 只记「最近一次被关闭的提示标识」，
# 文件大小以 bot 数为上限。

_NOTICES_BASENAME = "provider-notices.json"
_NOTICES_MAX_BYTES = 1024 * 1024   # 正常只有「bot 数 × 一行」；超过这个数就是被写坏了
_NOTICES_LOCK = threading.Lock()


def notices_path() -> str:
    """关闭记录文件：与 providers.json **同目录**（测试接缝指到 tmp 时自动跟着走）。"""
    return os.path.join(os.path.dirname(PROVIDERS_PATH) or ".", _NOTICES_BASENAME)


def notice_id(entry: dict) -> str:
    """一条待重启提示的标识（入参是 ``list_pending()`` 的一项）。

    只取「是否已停止自动重启、pending_since、fail_count」三项：新的一次待重启会刷新
    pending_since、新的一次失败让 fail_count 加一、停止与恢复翻转 halted —— 任一变化标识
    必变，旧的关闭记录随之失效。``last_error`` 的文字与 ``next_retry_at`` 在同一次状态里
    也会变，纳入会导致提示关不掉，所以不参与。摘要是确定性的：进程重启后标识不变。
    """
    basis = json.dumps([entry.get("halted") is True, entry.get("pending_since"),
                        entry.get("fail_count")], default=str)
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def load_dismissed() -> dict:
    """读关闭记录 ``{bot_id: 最近一次被关闭的提示标识}``。

    文件不存在、为空、超过 1 MiB、不是合法 JSON、顶层不是对象 → 一律当「没有关闭过任何提示」
    （最坏结果只是提示重新出现）。只读：绝不创建或修改文件。"""
    try:
        with open(notices_path(), "rb") as f:
            blob = f.read(_NOTICES_MAX_BYTES + 1)   # 多读一个字节用来判超限，不把大文件整个读进来
    except OSError:
        return {}
    if len(blob) > _NOTICES_MAX_BYTES:
        return {}
    try:
        raw = json.loads(blob.decode("utf-8"))
    except Exception:
        # 故意接得这么宽：解析会抛的不只 ValueError——极深嵌套抛 RecursionError。概览每次
        # 都读这份文件，它读坏了的代价只是提示重新出现，绝不能换成整页 500。
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def dismiss_notice(bot_id: str, nid: str) -> None:
    """记下「这只 bot 的这条提示已被关闭」。只写关闭记录文件：不碰清单、不碰任何
    settings、不执行外部命令。已是这条 → 不重写（幂等，文件字节与修改时间不变）。"""
    _require_writable()
    with _NOTICES_LOCK:
        dismissed = load_dismissed()
        if dismissed.get(bot_id) == nid:
            return
        dismissed[bot_id] = nid
        _write_notices_locked(dismissed)


def _write_notices_locked(dismissed: dict) -> None:
    """关闭记录整份落盘（调用方持 ``_NOTICES_LOCK``）：同目录临时文件 + fsync + 改名，0600。"""
    path = notices_path()
    tmp = os.path.join(os.path.dirname(path), ".%s.tmp-%d-%d" % (
        _NOTICES_BASENAME, os.getpid(), int(time.time() * 1000)))
    fd = _open_private(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(dismissed, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:   # 与清单落盘同理：任何异常都不留临时文件
        _cleanup(tmp)
        raise


def note_forced(bot_id: str) -> None:
    _require_writable()
    with _manifest_lock():
        data = load_providers()
        bd = (data.get("bindings") or {}).get(bot_id)
        if bd is None:
            return
        bd["forced_at"] = int(time.time())
        _commit_locked(data, ensure_key=False)


def set_applied(bot_id: str) -> None:
    """重启成功后：清 pending + 记 applied_at。"""
    if not isinstance(bot_id, str) or not bot_id:
        return
    _require_writable()
    with _manifest_lock():
        data = load_providers()
        bd = (data.get("bindings") or {}).get(bot_id)
        if not isinstance(bd, dict):
            return
        bd["pending_restart"] = False
        bd["pending_since"] = None
        bd["forced_at"] = None
        bd["last_error"] = None
        _reset_backoff(bd)
        bd["applied_at"] = int(time.time())
        _commit_locked(data, ensure_key=False)


def note_error(bot_id: str, text: str) -> None:
    with _manifest_lock():
        data = load_providers()
        bd = (data.get("bindings") or {}).get(bot_id)
        if bd is None:
            return
        bd["last_error"] = text
        _commit_locked(data, ensure_key=False)


def record_failure(bot_id: str, text: str, *, now: float | None = None) -> None:
    """拉起失败：计数 + 退避（60 秒起翻倍、封顶 480 秒），满 5 次停止自动重试；≥3 次留红字文案。"""
    now = _check_now_arg(now)
    text = text if isinstance(text, str) else str(text)
    if not isinstance(bot_id, str) or not bot_id:
        return
    _require_writable()
    with _manifest_lock():
        data = load_providers()
        bd = _pending_binding(data, bot_id)
        if bd is None:
            return
        piece = retry_backoff.on_failure(_backoff_view(bd), int(now),
                                         base=RESTART_BACKOFF_BASE_SEC, cap=RESTART_BACKOFF_CAP_SEC,
                                         max_failures=RESTART_MAX_FAILURES)
        n = piece["fail_count"]
        bd["fail_count"] = n
        bd["next_retry_at"] = None if piece["next_retry_at"] is None else int(piece["next_retry_at"])
        bd["halted"] = piece["halted"]
        bd["queued_since"] = None
        if n >= 3:
            bd["last_error"] = ("连续 %d 次拉起失败：%s" % (n, text))[:_LAST_ERROR_MAX]
        _commit_locked(data, ensure_key=False)


def halt_restart(bot_id: str, reason: str) -> None:
    """无法重启：停止自动重试，标记保留等人工。reason 非 str 或空白 → 固定文案（不做 str()）。"""
    if not isinstance(reason, str) or not reason.strip():
        reason = _HALT_DEFAULT_REASON
    if not isinstance(bot_id, str) or not bot_id:
        return
    _require_writable()
    with _manifest_lock():
        data = load_providers()
        bd = _pending_binding(data, bot_id)
        if bd is None:
            return
        bd["halted"] = True
        bd["next_retry_at"] = None
        bd["queued_since"] = None
        bd["last_error"] = reason[:_LAST_ERROR_MAX]
        _commit_locked(data, ensure_key=False)


def note_queued(bot_id: str, *, now: float | None = None) -> None:
    """首次因忙排队：记 queued_since（强杀从这一刻起算）；已有值不动。"""
    now = _check_now_arg(now)
    if not isinstance(bot_id, str) or not bot_id:
        return
    _require_writable()
    with _manifest_lock():
        data = load_providers()
        bd = _pending_binding(data, bot_id)
        if bd is None or bd.get("queued_since") is not None:
            return
        bd["queued_since"] = int(now)
        _commit_locked(data, ensure_key=False)


def resume_restart(bot_id: str) -> None:
    """解除停止（手动重启前调用）：清零计数与退避；last_error、pending_* 不动。"""
    if not isinstance(bot_id, str) or not bot_id:
        return
    _require_writable()
    with _manifest_lock():
        data = load_providers()
        bd = _pending_binding(data, bot_id)
        if bd is None:
            return
        _reset_backoff(bd)
        _commit_locked(data, ensure_key=False)


def rollback_binding(bot_id: str, previous: dict | None) -> None:
    """重建失败的保存：把 binding 还原成 previous（None = 删掉）。"""
    with _manifest_lock():
        data = load_providers()
        if previous is None:
            data.setdefault("bindings", {}).pop(bot_id, None)
        else:
            data.setdefault("bindings", {})[bot_id] = previous
        _commit_locked(data, ensure_key=False)


def get_binding_raw(bot_id: str) -> dict | None:
    data = _load_providers_safe()
    bd = (data.get("bindings") or {}).get(bot_id)
    return copy.deepcopy(bd) if isinstance(bd, dict) else None


# ── thinking 剥离哨兵 ──────────────────────────────────────────────────────

def unified_jsonl_path(bot_id: str) -> str | None:
    d = _bot_dir(bot_id)
    if not d:
        return None
    import chat_history
    uuid = chat_history.unified_session_uuid(bot_id)
    if not uuid:
        return None
    return os.path.join(chat_history._project_dir(d), uuid + ".jsonl")


def mark_thinking_stale(bot_id: str) -> bool:
    """写侧车哨兵 forced-by-panel，触发 spawn-worker.sh 一次性全量剥离 thinking。

    jsonl 不存在 → False（走 --session-id 全新会话，本就不需要剥离）。
    """
    _require_writable()
    d = _bot_dir(bot_id)
    # 与 settings.json 写口同款越界校验：侧车写面同样是"每只 bot 只写自己那份"的边界，
    # 不能因为 bot_channel_path 被指向别处就跟着写到 CHANNELS_ROOT 之外。
    if not d or not _within_channels_root(os.path.realpath(d)):
        return False
    path = unified_jsonl_path(bot_id)
    if not path or not os.path.exists(path):
        return False
    try:
        with open(path + ".providerfp", "w", encoding="utf-8") as f:
            f.write("forced-by-panel")
        return True
    except OSError:
        return False


# ── aux_llm（全局辅助模型，落 _global.yml）─────────────────────────────────

def global_cfg_path() -> str:
    """全局配置路径；``CLAUDEBOT_GLOBAL_CFG`` 接缝优先，默认 ``configs/_global.yml``。"""
    return os.environ.get("CLAUDEBOT_GLOBAL_CFG") or os.path.join(
        str(bot_registry.configs_dir()), "_global.yml")


def read_aux_llm() -> tuple[str, str]:
    """现读 _global.yml 的 aux_llm，返回 (provider_id, model)；缺块/类型非法归一为空串。"""
    try:
        path = global_cfg_path()
        with open(path, encoding="utf-8") as f:
            g = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return "", ""
    blk = g.get("aux_llm") if isinstance(g, dict) else None
    if not isinstance(blk, dict):
        return "", ""
    pid = blk.get("provider_id")
    model = blk.get("model")
    return (pid if isinstance(pid, str) else ""), (model if isinstance(model, str) else "")


def _yaml_scalar(value: str) -> str:
    """字符串 → 安全单行 YAML 标量（避免 safe_dump 的 ``...`` 文档结束标记）。"""
    dumped = yaml.safe_dump(value, allow_unicode=True).strip()
    return dumped.splitlines()[0] if dumped else "''"


def set_global_aux_llm(provider_id: str, model: str) -> None:
    """文本级 upsert ``aux_llm`` 块，保留 _global.yml 其余键与注释；原子落盘。"""
    _require_writable()
    path = global_cfg_path()
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = ""
    block = "aux_llm:\n  provider_id: %s\n  model: %s\n" % (
        _yaml_scalar(provider_id or ""), _yaml_scalar(model or ""))
    lines = text.splitlines(keepends=True)
    start = next((i for i, ln in enumerate(lines) if ln.startswith("aux_llm:")), None)
    if start is None:
        new_text = text
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        new_text += block
    else:
        end = len(lines)
        for j in range(start + 1, len(lines)):
            ln = lines[j]
            if ln.strip() and not ln[0].isspace():
                end = j
                break
        new_text = "".join(lines[:start]) + block + "".join(lines[end:])
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = "%s.tmp-%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(new_text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
