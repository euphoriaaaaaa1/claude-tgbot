"""每只 bot 的八项功能开关（公开版 T08；INTERFACE §2.6、§4.4、§8.3）。

开关存在频道目录下的 ``features.json``（``{"version": 1, "default": 布尔, "features": {键: 布尔}}``）。
本模块是它的唯一写者；调度器、worker、定时任务只读。

读法：
- 文件不存在（只认 ENOENT）：八项都开（没初始化的现有 bot）；
- 文件在、顶层是对象：值是布尔的键按值；缺的键取 ``default``，``default`` 缺或不是布尔按关；
  值不是布尔的键按关并警告；``features`` 不是对象时等于一个键都没写；
- 读不出（权限、非普通文件、非严格 UTF-8、非法 JSON/NaN）或顶层不是对象：八项都关、``readable`` 假。

写法：``<频道目录>/features.json.lock`` 上的跨平台锁（``platform_lock``），读改写整段在锁里；
写前备份 ``features.json.bak-<时间>``（只留 2 份），同目录临时文件 + ``os.replace`` + fsync。

公开版适配（与 CBL 的差异）：
- 频道目录与名单走 ``bots_registry`` / ``config_loader``（T04 唯一注册表），不再有第二套发现逻辑；
- 锁走 ``platform_lock``（T03），不直接 import fcntl；
- ``skillOverrides`` 的写入暂由本模块完成（T09 的 ``provider_config`` 尚未移植）；T09 到位后应改为
  调用 ``provider_config.update_skill_overrides``，本模块只保留开关读写；
- 前提检查收敛到公开版实际存在的资源：语音编号、NovelAI 技能目录/令牌、朋友圈库、私聊编号；
  不再检查 CBL 专有的语音桥、表情包库、主动消息 launchd 任务；
- ``--init`` 不带任何按编号的个人例外（PLAN §4.7）。
"""
import contextlib
import datetime
import json
import logging
import os
import stat
import sys
import tempfile
import time

_log = logging.getLogger(__name__)

FEATURES_NAME = "features.json"
LOCK_SUFFIX = ".lock"
BACKUP_INFIX = ".bak-"
BACKUP_KEEP = 2
NEW_FILE_MODE = 0o600
LOCK_WAIT_SEC = 3.0

BUSY_MESSAGE = "别的操作正在改这只 bot 的设置，请稍后再试"
NOT_SAVED_MESSAGE = "开关没有保存"
UNREADABLE_MESSAGE = "读不出这只 bot 的功能开关"
REQUIREMENT_MISSING_MESSAGE = "打不开：缺少必需的设置"
UNKNOWN_BOT_MESSAGE = "这个底层没有这只 bot"
NOT_BOOL_WARNING = "%s的开关值不是真假，已按关着处理"
SYNC_PENDING_WARNING = "技能显示设置没同步，下次重启前会自动重试"
SYNC_BUSY_MESSAGE = "技能显示设置没同步：别的操作正在改这只 bot 的设置"
SYNC_WRITE_MESSAGE = "技能显示设置没同步：写不进 settings.json"
SKILL_OFF = "off"
SKILL_OVERRIDES_KEY = "skillOverrides"
SKILLS_MANUAL_NAME = ".skills-manual"
SKILLS_MANUAL_TEXT = "这只 bot 的技能由人手管理：程序不写它的技能文件，打开功能时也不自动放技能；本文件只由人手删除。\n"
SKILL_PLACED_LINE = "已放上技能：%s"
SKILL_MANUAL_WARNING = "这只 bot 的技能由人手管理，没有自动放上%s的技能"
SKILL_PLACE_FAIL_WARNING = "%s的技能没放上，下次重启前会自动重试"

UNCHANGED_MESSAGE = "没有改动"
SAVED_MESSAGE = "已保存"
NEED_RESTART_MESSAGE = "要重启才生效"
NO_SUCH_BOT_MESSAGE = "没有这只 bot：%s"

# 功能键的唯一定义（顺序即页面块顺序）。skill：技能通用名；没有技能为 None。
FEATURES = {
    "voice": {"label": "发语音", "skill": "voice"},
    "image": {"label": "生图", "skill": "image"},
    "stickers": {"label": "表情包", "skill": "gif"},
    "moments": {"label": "朋友圈", "skill": "moments"},
    "proactive": {"label": "主动找你聊", "skill": "proactive"},
    "life": {"label": "生活作息", "skill": None},
    "relationship": {"label": "关系进展", "skill": None},
    "groups": {"label": "群聊", "skill": "group"},
}
KEYS = tuple(FEATURES)

REQUIREMENT_TEXT = {
    "voice_id_missing": "缺语音编号。去哪填：本页语音块的“语音编号”",
    "novelai_key_missing": "没设 NovelAI 令牌。去哪填：画风页的 NovelAI 令牌",
    "novelai_skill_missing": "生图工具目录不在。去哪处理：重新安装 novelai-skill",
    "moments_db_missing": "朋友圈库不在。去哪处理：先打开一次朋友圈页",
    "chat_id_missing": "不知道该主动找谁（配置里没有私聊编号）。去哪填：新建或重新配置这只 bot",
}
NOTE_MOMENTS_NO_IMAGE = "生图关着，朋友圈只发文字"
NOTE_MOMENTS_NO_LIFE = "生活作息关着，很少会自己发朋友圈；回评论、看你的朋友圈照常"
NOTE_PROACTIVE_NO_LIFE = "生活作息关着，主动找你时没有生活素材，可能很久才开口"
NOTE_NO_SLEEP = "还没填睡觉时段"
NOTE_JIWEN_OFF = "全局积温开关关着"
NOTE_NO_USERNAME = "进群前要先有它的 Telegram 用户名"

TELEGRAM_ME_NAME = ".telegram-me.json"


class FeaturesError(Exception):
    """code ∈ busy / internal / requirement_missing / unknown_bot；message 给人看，不含路径与文件内容。
    fields 只在 requirement_missing 时有：{键: 缺的各项 message 以"；"连接}。"""

    def __init__(self, code: str, message: str, fields: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.fields = code, message, fields


def features_path(bot_dir: str) -> str:
    return os.path.join(bot_dir, FEATURES_NAME)


def public_keys() -> list:
    return [{"key": k, "label": v["label"]} for k, v in FEATURES.items()]


def _require_key(key: str) -> None:
    if key not in FEATURES:
        raise ValueError("没有这个功能：%r" % (key,))


# ── 路径与名单（T04：bots_registry / config_loader 是唯一来源） ───────────────

def channels_root() -> str:
    raw = (os.environ.get("HUB_CHANNELS_DIR")
           or os.environ.get("CLAUDEBOT_CHANNELS_ROOT")
           or os.path.join("~", ".claude", "channels"))
    return os.path.realpath(os.path.expanduser(raw))


def _load_cfg(bot_id: str):
    import config_loader
    try:
        cfg = config_loader.load_bot(bot_id)
    except Exception:   # yml 不在或读不出：当没有
        return None
    return cfg if isinstance(cfg, dict) else None


def _registered(bot_id: str) -> bool:
    import bots_registry
    return any(b["id"] == bot_id for b in bots_registry.bots(include_disabled=True))


def _dir_from_cfg(bot_id: str, cfg: dict) -> str:
    raw = cfg.get("bot_channel_path")
    if isinstance(raw, str) and raw:
        return os.path.expanduser(raw)
    return os.path.join(channels_root(), bot_id)


def _require_inside_channels_root(bot_id: str, bot_dir: str) -> None:
    root = channels_root()
    real = os.path.realpath(bot_dir)
    if real != root and not real.startswith(root + os.sep):
        _log.warning("频道目录不在频道根内，拒绝写入 bot=%s", bot_id)
        raise FeaturesError("internal", NOT_SAVED_MESSAGE)


def channel_dir(bot_id: str, cfg: dict | None = None, *, for_write: bool = False):
    """这只 bot 的频道目录；不在注册表或配置损坏 → None。``for_write`` 先核对真实路径在频道根内。"""
    if cfg is None:
        if not _registered(bot_id):
            return None
        cfg = _load_cfg(bot_id)
        if cfg is None:
            return None
    bot_dir = _dir_from_cfg(bot_id, cfg)
    if for_write:
        _require_inside_channels_root(bot_id, bot_dir)
    return bot_dir


def _listed_bots() -> list:
    """配置目录里有 yml、频道目录存在的全部 bot（含停用），按编号升序。"""
    import bots_registry
    out = []
    for item in bots_registry.bots(include_disabled=True):
        bot_id = item["id"]
        cfg = _load_cfg(bot_id)
        if cfg is None:
            continue
        bot_dir = _dir_from_cfg(bot_id, cfg)
        if os.path.isdir(bot_dir):
            out.append((bot_id, bot_dir))
    return sorted(out)


def features_of(bot_id: str, cfg: dict | None = None) -> dict:
    bot_dir = channel_dir(bot_id, cfg)
    return dict.fromkeys(KEYS, False) if bot_dir is None else read(bot_dir)["features"]


def is_on(bot_id: str, key: str) -> bool:
    _require_key(key)
    return features_of(bot_id)[key]


def bots_with(key: str) -> list:
    _require_key(key)
    import config_loader
    return [cfg for cfg in config_loader.list_enabled_bots() if features_of(cfg["_bot_id"], cfg)[key]]


# ── 读 ───────────────────────────────────────────────────────────────────────

_MISSING = object()


def _reject_constant(name):
    raise ValueError("JSON 里不许有 %s" % name)


def _load_doc(path: str):
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return _MISSING
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with open(fd, encoding="utf-8") as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                return None
            text = f.read()
        doc = json.loads(text, parse_constant=_reject_constant)
    except (OSError, ValueError, RecursionError):
        return None
    return doc if isinstance(doc, dict) else None


def _interpret(doc: dict) -> dict:
    default = doc.get("default") is True
    given = doc.get("features")
    if not isinstance(given, dict):
        given = {}
    features, warnings = {}, []
    for k in KEYS:
        if k not in given:
            features[k] = default
        elif isinstance(given[k], bool):
            features[k] = given[k]
        else:
            features[k] = False
            warnings.append(NOT_BOOL_WARNING % FEATURES[k]["label"])
    return {"features": features, "warnings": warnings, "readable": True}


def _state_of(doc) -> dict:
    if doc is _MISSING:
        return {"features": dict.fromkeys(KEYS, True), "warnings": [], "readable": True}
    if doc is None:
        return {"features": dict.fromkeys(KEYS, False), "warnings": [], "readable": False}
    return _interpret(doc)


def read(bot_dir: str) -> dict:
    return _state_of(_load_doc(features_path(bot_dir)))


# ── 前提（公开版实际存在的资源） ─────────────────────────────────────────────

def _bot_cfg(bot_id: str) -> dict:
    cfg = _load_cfg(bot_id)
    return cfg if isinstance(cfg, dict) else {}


def _global_cfg() -> dict:
    import config_loader
    try:
        cfg = config_loader.load_global()
    except Exception:
        _log.warning("全局配置读不出，功能提示按缺省处理")
        return {}
    return cfg if isinstance(cfg, dict) else {}


def _read_json_dict(path: str):
    doc = _load_doc(path)
    return doc if isinstance(doc, dict) else None


def _voice_id(bot_dir: str):
    data = _read_json_dict(os.path.join(bot_dir, "access.json")) or {}
    value = data.get("voiceId")
    return value if isinstance(value, str) and value.strip() else None


def _novelai_skill_root() -> str:
    from moments import styles_routes
    return str(styles_routes.SKILL_ROOT)


def _novelai_key_set(skill_root: str) -> bool:
    if (os.environ.get("NOVELAI_BEARER_TOKEN") or "").strip():
        return True
    try:
        with open(os.path.join(skill_root, ".env.local"), encoding="utf-8") as f:
            for line in f:
                s = line.strip()
                if s.startswith("export "):
                    s = s[len("export "):].lstrip()
                key, sep, value = s.partition("=")
                if sep and key.strip() == "NOVELAI_BEARER_TOKEN" and value.strip().strip("'\"") != "":
                    return True
    except (OSError, UnicodeDecodeError):
        return False
    return False


def _has_chat_id(cfg: dict) -> bool:
    v = cfg.get("chat_id")
    return (isinstance(v, int) and not isinstance(v, bool)) or (isinstance(v, str) and v.strip() != "")


def _has_username(cfg: dict, bot_dir: str) -> bool:
    value = cfg.get("telegram_username")
    if isinstance(value, str) and value.strip():
        return True
    me = _read_json_dict(os.path.join(bot_dir, TELEGRAM_ME_NAME)) or {}
    name = me.get("username")
    return isinstance(name, str) and name.strip() != ""


def _check(bot_id: str, bot_dir: str, keys, features: dict, bridge=None) -> dict:
    cfg, gcfg = _bot_cfg(bot_id), _global_cfg()
    out = {}
    for key in keys:
        missing, notes = [], []
        if key == "voice":
            if not _voice_id(bot_dir):
                missing.append("voice_id_missing")
        elif key == "image":
            root = _novelai_skill_root()
            if not os.path.isdir(root):
                missing.append("novelai_skill_missing")
            elif not _novelai_key_set(root):
                missing.append("novelai_key_missing")
        elif key == "stickers":
            pass
        elif key == "moments":
            import db
            if not os.path.isfile(db.DB_PATH):
                missing.append("moments_db_missing")
            if not features.get("image"):
                notes.append(NOTE_MOMENTS_NO_IMAGE)
            if not features.get("life"):
                notes.append(NOTE_MOMENTS_NO_LIFE)
        elif key == "proactive":
            if not _has_chat_id(cfg):
                missing.append("chat_id_missing")
            if not features.get("life"):
                notes.append(NOTE_PROACTIVE_NO_LIFE)
        elif key == "life":
            if not cfg.get("sleep_hours"):
                notes.append(NOTE_NO_SLEEP)
        elif key == "relationship":
            jiwen = gcfg.get("jiwen")
            if not (isinstance(jiwen, dict) and jiwen.get("enabled")):
                notes.append(NOTE_JIWEN_OFF)
        elif key == "groups":
            if not _has_username(cfg, bot_dir):
                notes.append(NOTE_NO_USERNAME)
        else:
            raise ValueError("没有这个功能：%r" % (key,))
        out[key] = {"ready": not missing,
                    "missing": [{"code": c, "message": REQUIREMENT_TEXT[c]} for c in missing],
                    "notes": notes}
    return out


def requirements(bot_id: str, *, features: dict | None = None, bridge: dict | None = None) -> dict:
    bot_dir = channel_dir(bot_id)
    if bot_dir is None:
        raise FeaturesError("unknown_bot", UNKNOWN_BOT_MESSAGE)
    if features is None:
        features = read(bot_dir)["features"]
    return _check(bot_id, bot_dir, KEYS, features, bridge)


# ── 写 ───────────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def _locked(bot_dir: str):
    import platform_lock
    try:
        with platform_lock.file_lock(features_path(bot_dir) + LOCK_SUFFIX, timeout=LOCK_WAIT_SEC):
            yield
    except platform_lock.LockBusy:
        raise FeaturesError("busy", BUSY_MESSAGE) from None
    except platform_lock.LockError:
        raise FeaturesError("internal", NOT_SAVED_MESSAGE) from None


def _backup(path: str, raw: bytes, mode: int) -> None:
    base = path + BACKUP_INFIX + datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    bak, n = base, 0
    while os.path.lexists(bak):
        n += 1
        bak = "%s.%d" % (base, n)
    with open(os.open(bak, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "wb") as f:
        f.write(raw)
    os.chmod(bak, mode)
    d, prefix = os.path.dirname(path), os.path.basename(path) + BACKUP_INFIX
    for old in sorted(x for x in os.listdir(d) if x.startswith(prefix))[:-BACKUP_KEEP]:
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(d, old))


def _write_atomic(path: str, data: bytes, mode: int) -> None:
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".%s.tmp-" % os.path.basename(path))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
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


def _save(path: str, data: bytes) -> None:
    try:
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except FileNotFoundError:
            mode = None
        if mode is not None:
            with open(path, "rb") as f:
                _backup(path, f.read(), mode)
        _write_atomic(path, data, NEW_FILE_MODE if mode is None else mode)
    except OSError as e:
        _log.warning("功能开关写回失败：%s", type(e).__name__)
        raise FeaturesError("internal", NOT_SAVED_MESSAGE)


def write(bot_id: str, bot_dir: str, changes: dict) -> dict:
    """改开关：锁内现读，只改给了的键；从关变开先查前提；文件不存在时按本次提交新建。
    返回 ``{"features", "changed", "warnings"}``。"""
    if not isinstance(changes, dict):
        raise ValueError("changes 必须是对象")
    for k, v in changes.items():
        _require_key(k)
        if not isinstance(v, bool):
            raise ValueError("%s 的值只能是 true 或 false" % k)
    _require_inside_channels_root(bot_id, bot_dir)
    path = features_path(bot_dir)
    with _locked(bot_dir):
        doc = _load_doc(path)
        if doc is None:
            raise FeaturesError("internal", UNREADABLE_MESSAGE)
        created = doc is _MISSING
        if created:
            fresh = dict.fromkeys(KEYS, False)
            fresh.update({k: True for k, v in changes.items() if v})
            doc = {"version": 1, "default": False, "features": fresh}
            before = dict.fromkeys(KEYS, False)
        else:
            before = _interpret(doc)["features"]
        changed = [k for k in KEYS if k in changes and changes[k] != before[k]]
        turning_on = [k for k in changed if changes[k]]
        if turning_on:
            checked = _check(bot_id, bot_dir, turning_on, before, None)
            lacking = {k: "；".join(m["message"] for m in checked[k]["missing"])
                       for k in turning_on if checked[k]["missing"]}
            if lacking:
                raise FeaturesError("requirement_missing", REQUIREMENT_MISSING_MESSAGE, lacking)
        if changed or created:
            given = doc.get("features")
            given = dict(given) if isinstance(given, dict) else {}
            given.update({k: changes[k] for k in changed})
            doc = dict(doc, features=given)
            _save(path, (json.dumps(doc, ensure_ascii=True, indent=2) + "\n").encode("ascii"))
    result = _interpret(doc)
    return {"features": result["features"], "changed": changed, "warnings": result["warnings"]}


def init_features(bot_dir: str, features: dict, *, force: bool = False) -> tuple:
    """整份写 ``{"version":1,"default":true,"features":{八项}}``；文件在且没给 force → 不写。
    返回 ``(写没写, 写前八项)``。"""
    for k in KEYS:
        if not isinstance(features.get(k), bool):
            raise ValueError("%s 的值只能是 true 或 false" % k)
    path = features_path(bot_dir)
    with _locked(bot_dir):
        doc = _load_doc(path)
        before = _state_of(doc)["features"]
        if doc is not _MISSING and not force:
            return False, before
        new = {"version": 1, "default": True, "features": {k: features[k] for k in KEYS}}
        _save(path, (json.dumps(new, ensure_ascii=True, indent=2) + "\n").encode("ascii"))
    return True, before


def mark_skills_manual(bot_dir: str) -> bool:
    path = os.path.join(bot_dir, SKILLS_MANUAL_NAME)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, NEW_FILE_MODE)
    except FileExistsError:
        return False
    try:
        with open(fd, "wb") as f:
            f.write(SKILLS_MANUAL_TEXT.encode("utf-8"))
            f.flush()
            os.fsync(f.fileno())
        with contextlib.suppress(OSError):   # Windows 的 chmod 语义有限，写成功就算成
            os.chmod(path, NEW_FILE_MODE)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    return True


# ── 技能显示设置与补放 ──────────────────────────────────────────────────────

def skill_overrides_for(bot_dir: str, features: dict) -> dict:
    prefix = os.path.basename(os.path.normpath(bot_dir))
    return {"%s-%s" % (prefix, spec["skill"]): (None if features[key] else SKILL_OFF)
            for key, spec in FEATURES.items() if spec["skill"]}


def skill_file_path(bot_dir: str, skill: str) -> str:
    prefix = os.path.basename(os.path.normpath(bot_dir))
    return os.path.join(bot_dir, ".claude", "skills", "%s-%s" % (prefix, skill), "SKILL.md")


def _load_settings(settings_path: str):
    doc = _load_doc(settings_path)
    if doc is None:
        raise FeaturesError("internal", SYNC_WRITE_MESSAGE)
    return {} if doc is _MISSING else doc


def _update_skill_overrides(settings_path: str, wanted: dict) -> list:
    """按 ``wanted``（键 → "off" 或 None）对齐 settings.json 的 skillOverrides。
    与现状相同不写盘；写前备份；失败抛 FeaturesError。"""
    import platform_lock
    try:
        os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    except OSError:
        raise FeaturesError("internal", SYNC_WRITE_MESSAGE) from None
    try:
        with platform_lock.file_lock(settings_path + ".lock", timeout=LOCK_WAIT_SEC):
            doc = _load_settings(settings_path)
            current = doc.get(SKILL_OVERRIDES_KEY)
            if current is None:
                current = {}
            if not isinstance(current, dict):
                raise FeaturesError("internal", SYNC_WRITE_MESSAGE)
            before = dict(current)
            for key, want in wanted.items():
                if want is None:
                    current.pop(key, None)
                else:
                    current[key] = want
            changed = sorted(k for k in set(before) | set(current) if before.get(k) != current.get(k))
            if not changed:
                return []
            out = dict(doc)
            if current:
                out[SKILL_OVERRIDES_KEY] = current
            else:
                out.pop(SKILL_OVERRIDES_KEY, None)
            try:
                mode = stat.S_IMODE(os.stat(settings_path).st_mode)
            except FileNotFoundError:
                mode = NEW_FILE_MODE
            if os.path.exists(settings_path):
                with open(settings_path, "rb") as f:
                    _backup(settings_path, f.read(), mode)
            _write_atomic(settings_path,
                          (json.dumps(out, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), mode)
            return changed
    except platform_lock.LockBusy:
        raise FeaturesError("busy", SYNC_BUSY_MESSAGE) from None
    except platform_lock.LockError:
        raise FeaturesError("internal", SYNC_WRITE_MESSAGE) from None


def _sync_overrides_locked(bot_id: str, bot_dir: str, doc: dict, extra: dict | None = None) -> list:
    wanted = skill_overrides_for(bot_dir, _interpret(doc)["features"])
    wanted.update(extra or {})
    settings_path = os.path.join(bot_dir, ".claude", "settings.json")
    return _update_skill_overrides(settings_path, wanted)


def _sync_error(e: FeaturesError) -> FeaturesError:
    if e.message == BUSY_MESSAGE:
        return FeaturesError("busy", SYNC_BUSY_MESSAGE)
    if e.message == NOT_SAVED_MESSAGE:
        return FeaturesError("internal", SYNC_WRITE_MESSAGE)
    return e


def sync_skill_visibility(bot_id: str, *, extra: dict | None = None) -> list:
    """按当前开关幂等对齐 settings.json 的六个技能键；没初始化（features.json 不在）返回 []。"""
    bot_dir = channel_dir(bot_id)
    if bot_dir is None:
        raise FeaturesError("unknown_bot", UNKNOWN_BOT_MESSAGE)
    path = features_path(bot_dir)
    if _load_doc(path) is _MISSING:
        return []
    try:
        _require_inside_channels_root(bot_id, bot_dir)
        with _locked(bot_dir):
            doc = _load_doc(path)
            if doc is _MISSING:
                return []
            if doc is None:
                raise FeaturesError("internal", UNREADABLE_MESSAGE)
            return _sync_overrides_locked(bot_id, bot_dir, doc, extra)
    except FeaturesError as e:
        raise _sync_error(e)


def _precheck_settings(bot_dir: str) -> None:
    claude_dir = os.path.join(bot_dir, ".claude")
    try:
        os.makedirs(claude_dir, exist_ok=True)
    except OSError:
        raise FeaturesError("internal", SYNC_WRITE_MESSAGE) from None
    if not os.access(claude_dir, os.W_OK):
        raise FeaturesError("internal", SYNC_WRITE_MESSAGE)
    _load_settings(os.path.join(claude_dir, "settings.json"))


def _probe_settings_lock(bot_dir: str) -> None:
    import platform_lock
    path = os.path.join(bot_dir, ".claude", "settings.json.lock")
    try:
        with platform_lock.file_lock(path, timeout=LOCK_WAIT_SEC):
            return
    except platform_lock.LockBusy:
        raise FeaturesError("busy", SYNC_BUSY_MESSAGE) from None
    except platform_lock.LockError:
        raise FeaturesError("internal", SYNC_WRITE_MESSAGE) from None


def sync_skills(bot_id: str) -> dict:
    """先给开着的功能补放缺的技能，再按当前开关对齐六个 skillOverrides 键。
    返回 ``{"features", "placed", "skipped_manual", "changed", "warnings"}``。"""
    bot_dir = channel_dir(bot_id)
    if bot_dir is None:
        raise FeaturesError("unknown_bot", UNKNOWN_BOT_MESSAGE)
    empty = {"features": dict.fromkeys(KEYS, True), "placed": [], "skipped_manual": [],
             "changed": [], "warnings": []}
    path = features_path(bot_dir)
    if _load_doc(path) is _MISSING:
        return empty
    try:
        _require_inside_channels_root(bot_id, bot_dir)
        with _locked(bot_dir):
            doc = _load_doc(path)
            if doc is _MISSING:
                return empty
            if doc is None:
                raise FeaturesError("internal", UNREADABLE_MESSAGE)
            features = _interpret(doc)["features"]
            manual = os.path.lexists(os.path.join(bot_dir, SKILLS_MANUAL_NAME))
            to_place = []
            for key, spec in FEATURES.items():
                skill = spec["skill"]
                if skill and features[key] and not os.path.isfile(skill_file_path(bot_dir, skill)):
                    to_place.append((spec, skill))
            placed, skipped_manual, warnings = [], [], []
            if manual:
                skipped_manual = [skill for _spec, skill in to_place]
            elif to_place:
                _precheck_settings(bot_dir)
                _probe_settings_lock(bot_dir)
                for spec, skill in to_place:
                    try:
                        import skill_gen
                        result = skill_gen.write_all(bot_id, "fill", names=[skill], apply=True)
                    except Exception as e:  # noqa: BLE001 补放失败不算对齐失败
                        _log.warning("补放技能失败 bot=%s skill=%s：%s", bot_id, skill, type(e).__name__)
                        warnings.append(SKILL_PLACE_FAIL_WARNING % spec["label"])
                        continue
                    if skill in result["placed"]:
                        placed.append(skill)
                    else:
                        _log.warning("补放技能没写成 bot=%s skill=%s", bot_id, skill)
                        warnings.append(SKILL_PLACE_FAIL_WARNING % spec["label"])
            changed = _sync_overrides_locked(bot_id, bot_dir, doc)
            return {"features": features, "placed": placed, "skipped_manual": skipped_manual,
                    "changed": changed, "warnings": warnings}
    except FeaturesError as e:
        raise _sync_error(e)


def initial_features(features, voice_id=None, *, bot_id: str | None = None, bot_dir: str | None = None,
                     bridge: dict | None = None) -> tuple:
    """新建时的 features.json 内容与 skipped。

    ``features`` 是勾了的功能键数组（省略/空 = 只开文字聊天）。返回
    ``({"version":1,"default":False,"features":{八项}}, [{"key","missing"}, …])``。
    给了 ``voice_id`` 时自动打开 voice；勾了 voice 却没有 voice_id 记进 skipped。
    公开版新建不因其它全局资源缺失而关掉用户勾的项（CBL 的完整前提检查留给生命周期后续收紧）。
    """
    if features is None:
        features = []
    if not isinstance(features, (list, tuple)):
        raise ValueError("features 必须是数组")
    picked = []
    for key in features:
        if not isinstance(key, str) or key not in FEATURES:
            raise ValueError("没有这个功能：%r" % (key,))
        if key in picked:
            raise ValueError("功能给重复了：%s" % key)
        picked.append(key)
    if isinstance(voice_id, str) and voice_id.strip() and "voice" not in picked:
        picked.append("voice")

    state = dict.fromkeys(KEYS, False)
    skipped = []
    for key in FEATURES:
        if key not in picked:
            continue
        if key == "voice" and not (isinstance(voice_id, str) and voice_id.strip()):
            skipped.append({"key": key,
                            "missing": [{"code": "voice_id_missing",
                                         "message": REQUIREMENT_TEXT["voice_id_missing"]}]})
        else:
            state[key] = True
    return {"version": 1, "default": False, "features": state}, skipped


# ── 命令行 python3 -m bot_features ───────────────────────────────────────────

USAGE = ("写法：--show <编号> | --show --all | --init [--apply] [--force] [--no-restart] | "
         "--set <编号> <键>=on 或 off … | --sync-skills <编号> | --dump-keys")


class _ArgError(Exception):
    pass


def _out(text: str) -> None:
    sys.stdout.write(text + "\n")


def _err(text: str) -> None:
    sys.stderr.write(text + "\n")


def _json_out(obj) -> None:
    _out(json.dumps(obj, ensure_ascii=False, indent=2))


def _cli_dump_keys(args) -> int:
    if args:
        raise _ArgError("--dump-keys 不带别的参数")
    _json_out(public_keys())
    return 0


def _show_entry(bot_id: str, state: dict) -> dict:
    return {"bot_id": bot_id, "features": state["features"],
            "requirements": requirements(bot_id, features=state["features"]),
            "warnings": state["warnings"]}


def _cli_show(args) -> int:
    if len(args) != 1 or (args[0].startswith("-") and args[0] != "--all"):
        raise _ArgError("写法是 --show <编号> 或 --show --all")
    if args[0] != "--all":
        bot_id = args[0]
        bot_dir = channel_dir(bot_id)
        if bot_dir is None:
            _err(NO_SUCH_BOT_MESSAGE % bot_id)
            return 2
        state = read(bot_dir)
        if not state["readable"]:
            _err(UNREADABLE_MESSAGE)
            return 1
        try:
            entry = _show_entry(bot_id, state)
        except FeaturesError:
            _err(NO_SUCH_BOT_MESSAGE % bot_id)
            return 2
        _json_out(entry)
        return 0
    entries, rc = [], 0
    for bot_id, bot_dir in _listed_bots():
        state = read(bot_dir)
        if not state["readable"]:
            entries.append({"bot_id": bot_id, "error": UNREADABLE_MESSAGE})
            rc = 1
            continue
        try:
            entries.append(_show_entry(bot_id, state))
        except FeaturesError:
            continue
    _json_out(entries)
    return rc


def _parse_set(args) -> tuple:
    if not args or args[0].startswith("-"):
        raise _ArgError("写法是 --set <编号> <键>=on 或 off …")
    bot_id, pairs = args[0], args[1:]
    if not pairs:
        raise _ArgError("没给要改的开关，写法是 <键>=on 或 off")
    changes = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise _ArgError("%s 缺等号，写法是 <键>=on 或 off" % pair)
        if key == "text":
            raise _ArgError("文字聊天常开，没有开关")
        if key not in FEATURES:
            raise _ArgError("没有这个功能：%s（能改的键：%s）" % (key, "、".join(KEYS)))
        if value not in ("on", "off"):
            raise _ArgError("%s 的值只能是 on 或 off" % key)
        if key in changes:
            raise _ArgError("%s 给了两次" % key)
        changes[key] = value == "on"
    return bot_id, changes


def _cli_set(args) -> int:
    bot_id, changes = _parse_set(args)
    from adapters import ops
    if ops.person_busy(bot_id):
        _err(ops.PERSON_BUSY_MESSAGE)
        return 1
    try:
        bot_dir = channel_dir(bot_id, for_write=True)
        if bot_dir is None:
            _err(NO_SUCH_BOT_MESSAGE % bot_id)
            return 2
        result = write(bot_id, bot_dir, changes)
    except FeaturesError as e:
        _err(e.message)
        for key, text in (e.fields or {}).items():
            _err("%s：%s" % (FEATURES[key]["label"], text))
        return 1
    warnings = list(result["warnings"])
    try:
        sync_skill_visibility(bot_id)
    except Exception as e:  # noqa: BLE001 没对齐上不算失败
        _log.warning("技能显示设置没同步 %s：%s", bot_id, getattr(e, "code", type(e).__name__))
        warnings.append(SYNC_PENDING_WARNING)
    _out(SAVED_MESSAGE if result["changed"] else UNCHANGED_MESSAGE)
    for text in warnings:
        _out("警告：" + text)
    return 0


def _cli_sync_skills(args) -> int:
    if len(args) != 1 or args[0].startswith("-"):
        raise _ArgError("写法是 --sync-skills <编号>")
    bot_id = args[0]
    try:
        result = sync_skills(bot_id)
    except FeaturesError as e:
        if e.code == "unknown_bot":
            _err(NO_SUCH_BOT_MESSAGE % bot_id)
            return 2
        _err(e.message)
        return 1
    if result["placed"]:
        _out(SKILL_PLACED_LINE % "、".join(result["placed"]))
    for skill in result["skipped_manual"]:
        label = next(spec["label"] for spec in FEATURES.values() if spec["skill"] == skill)
        _err("技能手管，跳过：%s" % label)
    for text in result["warnings"]:
        _out("警告：" + text)
    changed = result["changed"]
    _out("已同步：" + "、".join(changed) if changed else UNCHANGED_MESSAGE)
    return 0


def _cli_init(args) -> int:
    flags = set(args)
    if len(flags) != len(args) or not flags <= {"--apply", "--force", "--no-restart"}:
        raise _ArgError("--init 只认 --apply、--force、--no-restart，各至多一次")
    apply_, force, no_restart = "--apply" in flags, "--force" in flags, "--no-restart" in flags
    planned = []
    unverifiable = []
    for bot_id, bot_dir in _listed_bots():
        entry = {"bot_id": bot_id, "features_json": "skip_exists", "features": None,
                 "skills_manual": "skip", "effective_changed": False,
                 "restart": "not_needed", "result": None, "message": None}
        doc = _load_doc(features_path(bot_dir))
        if doc is not _MISSING and not force:
            planned.append((entry, bot_dir))
            continue
        try:
            _load_settings(os.path.join(bot_dir, ".claude", "settings.json"))
        except FeaturesError:
            unverifiable.append(bot_id)
        wanted = dict.fromkeys(KEYS, True)
        before = _state_of(doc)["features"]
        entry.update(features_json="write" if doc is _MISSING else "overwrite",
                     features=wanted, skills_manual="exists"
                     if os.path.lexists(os.path.join(bot_dir, SKILLS_MANUAL_NAME)) else "write",
                     effective_changed=before != wanted,
                     restart="needed" if before != wanted else "not_needed")
        planned.append((entry, bot_dir))
    doc = {"apply": apply_, "ok": not unverifiable, "bots": [e for e, _ in planned]}
    if not doc["ok"]:
        _json_out(doc)
        for bot_id in unverifiable:
            _err("%s：settings.json 读不出，没法核对技能显示设置，整次什么都没写" % bot_id)
        return 1
    if not apply_:
        _json_out(doc)
        return 0
    failed = []
    for entry, bot_dir in planned:
        if entry["features_json"] == "skip_exists":
            entry["result"] = "skipped"
            continue
        try:
            _require_inside_channels_root(entry["bot_id"], bot_dir)
            wrote, before = init_features(bot_dir, entry["features"], force=force)
            if not wrote:
                entry.update(result="skipped", skills_manual="skip",
                             effective_changed=False, restart="not_needed")
                continue
            entry["effective_changed"] = before != entry["features"]
            entry["restart"] = "needed" if entry["effective_changed"] else "not_needed"
            try:
                entry["skills_manual"] = "write" if mark_skills_manual(bot_dir) else "exists"
            except OSError:
                raise FeaturesError("internal", "开关已保存，但技能手管标记没写成") from None
            sync_skill_visibility(entry["bot_id"])
            entry["result"] = "written"
            if entry["restart"] == "needed" and no_restart:
                entry["message"] = NEED_RESTART_MESSAGE
        except Exception as e:  # noqa: BLE001 一只出错不挡别的只
            entry["result"] = "failed"
            failed.append((entry["bot_id"], e.message if isinstance(e, FeaturesError)
                           else "出错了：%s" % type(e).__name__))
    doc["ok"] = not failed
    _json_out(doc)
    for bot_id, reason in failed:
        _err("%s：%s" % (bot_id, reason))
    return 1 if failed else 0


_COMMANDS = {"--dump-keys": _cli_dump_keys, "--show": _cli_show, "--init": _cli_init, "--set": _cli_set,
             "--sync-skills": _cli_sync_skills}


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        handler = _COMMANDS.get(args[0]) if args else None
        if handler is None:
            raise _ArgError("要给一个命令。" + USAGE)
        return handler(args[1:])
    except _ArgError as e:
        _err("参数不对：%s" % e)
        return 2


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            _stream.reconfigure(encoding="utf-8")
    logging.getLogger().addHandler(logging.NullHandler())
    import bot_features
    sys.exit(bot_features.main())
