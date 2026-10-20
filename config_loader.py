"""加载 bot 配置和全局配置。

以 ``_`` 开头的键是运行时注入的计算字段（年龄/学制/身份），不在 yml 文件里；
``load_bot`` 每次从磁盘重读后在内存态加工，绝不写回。

公开版：配置目录与停用判定都走 ``bots_registry`` 的同一接缝；DeepSeek 密钥优先读
``_secrets.yml``（不进 git），旧写法 ``_global.yml.jiwen.delta_llm.api_key`` 仍兼容。
"""
import datetime
import os
import re
import stat
import sys
from typing import Optional

import yaml

import persona_clock
from bots_registry import is_enabled as _enabled   # 停用判定只有一处，别在这里再写一遍
from bots_registry import configs_dir as _registry_configs_dir

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIGS_DIR = os.path.join(PROJECT_ROOT, "configs")
_DEFAULT_CONFIGS_DIR = CONFIGS_DIR
GLOBAL_CFG_PATH = os.path.join(CONFIGS_DIR, "_global.yml")
_DEFAULT_GLOBAL_CFG_PATH = GLOBAL_CFG_PATH

# INTERFACE §2.1：bot 标识只允许字母/数字开头，后续可含字母/数字/_/-，
# 从源头排除 _global.yml 与 ../ 等路径穿越。
BOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _is_safe_bot_id(bot_id: str) -> bool:
    """判断 bot_id 是否符合安全 bot 标识契约。"""
    return isinstance(bot_id, str) and BOT_ID_RE.fullmatch(bot_id) is not None


def _validate_bot_id(bot_id: str) -> None:
    """bot_id 不合法时抛 ValueError，错误信息不泄露路径/配置内容。"""
    if not _is_safe_bot_id(bot_id):
        raise ValueError(f"{bot_id} 非法 bot 标识")


def _configs_dir() -> str:
    """bot yml 目录，与 ``bots_registry.configs_dir()`` 同源：每次现读 ``HUB_CONFIGS_DIR``。

    导入期快照会让"注册表认停用、这里读另一份目录"两处不一致；
    ``CONFIGS_DIR`` 被显式改过（既有测试的 monkeypatch 写法）则以它为准。
    """
    if CONFIGS_DIR != _DEFAULT_CONFIGS_DIR:
        return CONFIGS_DIR
    return str(_registry_configs_dir())


def config_dir() -> str:
    """返回 bot 配置目录（公开接缝 ``CLAUDEBOT_CONFIG_DIR`` / ``HUB_CONFIGS_DIR``）。"""
    return _configs_dir()


def global_cfg_path() -> str:
    """返回全局配置路径。

    优先级：``CLAUDEBOT_GLOBAL_CFG`` → 被显式 monkeypatch 的 ``GLOBAL_CFG_PATH``
    → 配置目录下的 ``_global.yml``（跟随 ``CLAUDEBOT_CONFIG_DIR``）。
    """
    override = os.environ.get("CLAUDEBOT_GLOBAL_CFG")
    if override:
        return override
    if GLOBAL_CFG_PATH != _DEFAULT_GLOBAL_CFG_PATH:
        return GLOBAL_CFG_PATH
    return os.path.join(_configs_dir(), "_global.yml")


def load_global() -> dict:
    path = global_cfg_path()
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ── 本机密钥（key-out-of-git）───────────────────────────────────────────────
# DeepSeek 密钥不进 git：放在全局配置同目录的 _secrets.yml（.gitignore 已排除，权限 600），
# 键 deepseek.api_key；base_url、model 等仍在 _global.yml 的 jiwen.delta_llm。
# 文件名必须以下划线开头，否则 list_enabled_bots() 会把它当成一个 bot。
SECRETS_FILE_NAME = "_secrets.yml"

# [secrets] 警告去重：键是 (码, 路径)，同一进程里同一个键只写一次 stderr。
_WARNED: set = set()


def secrets_path() -> str:
    """本机密钥文件路径。每次调用都现读环境变量；不检查文件存不存在；不抛异常。

    CLAUDEBOT_SECRETS_FILE 非空时用它（展开 ~；测试接缝，生产不设）；否则与全局配置同目录。
    """
    override = os.environ.get("CLAUDEBOT_SECRETS_FILE")
    if override:
        return os.path.expanduser(override)
    return os.path.join(os.path.dirname(global_cfg_path()) or ".", SECRETS_FILE_NAME)


def _warn_once(code: str, path: str, detail: str) -> None:
    """往 stderr 写一行 "[secrets] <码>: <说明>"，同一 (码, 路径) 每个进程只写一次。

    说明里只许有路径、权限位、异常类型名和修复指引；密钥的任何部分、长度、文件内容、
    str(异常) 一律不许出现。
    """
    key = (code, path)
    if key in _WARNED:
        return
    _WARNED.add(key)
    try:
        sys.stderr.write("[secrets] %s: %s\n" % (code, detail))
    except Exception:  # stderr 已关闭也不能让取密钥失败（约定永不抛异常）
        pass


def _read_secrets_file(path: str) -> tuple:
    """判断新位置的状态，返回 (状态, 密钥, 说明)。

    状态按序判断，命中即停：absent / too_open / unreadable / no_key / ok。
    密钥只在 ok 时非空；说明只是权限位或异常类型名，不含文件内容。
    """
    try:
        st = os.stat(path)  # 跟随符号链接
    except (FileNotFoundError, NotADirectoryError):
        return "absent", "", ""
    except (OSError, ValueError) as e:
        return "unreadable", "", type(e).__name__
    # 只在 posix 上查权限：Windows 的权限位不可靠，查了反而永远读不到。
    # 组用户或其他用户有任何权限位就不打开文件。
    if os.name == "posix" and st.st_mode & 0o077:
        return "too_open", "", "%04o" % stat.S_IMODE(st.st_mode)
    if not stat.S_ISREG(st.st_mode):
        return "unreadable", "", "不是普通文件"
    try:
        with open(path, "rb") as f:
            data = yaml.safe_load(f.read().decode("utf-8"))
    except Exception as e:
        # 只留类型名：PyYAML 的异常文字会带出错那一行的原文（可能就是密钥那一行）
        return "unreadable", "", type(e).__name__
    section = data.get("deepseek") if isinstance(data, dict) else None
    value = section.get("api_key") if isinstance(section, dict) else None
    if not isinstance(value, str) or not value.strip():
        return "no_key", "", ""
    return "ok", value.strip(), ""


def _legacy_api_key(delta_cfg) -> str:
    """旧写法 _global.yml 的 jiwen.delta_llm.api_key（去掉首尾空白），没有就是空串。

    delta_cfg 为 None 时现读全局配置；读出错算空，不输出任何异常信息。
    既不是 dict 也不是 None 时当作没有旧值。
    """
    if delta_cfg is None:
        try:
            g = load_global()
        except Exception:
            return ""
        jiwen = g.get("jiwen") if isinstance(g, dict) else None
        delta_cfg = jiwen.get("delta_llm") if isinstance(jiwen, dict) else None
    value = delta_cfg.get("api_key") if isinstance(delta_cfg, dict) else None
    return value.strip() if isinstance(value, str) else ""


def get_deepseek_api_key(delta_cfg: Optional[dict] = None) -> str:
    """返回 DeepSeek 密钥（去掉首尾空白）；没有可用密钥时返回 ""。

    新位置 secrets_path() 的 deepseek.api_key 优先；新位置不可用时回落旧写法
    （_global.yml 的 jiwen.delta_llm.api_key，照用并警告，提示挪走）。
    delta_cfg：调用方手里的 jiwen.delta_llm 段，用作旧写法的来源；传 None 则现读 load_global()。
    永不抛异常；永不写 stdout；stderr 只写 [secrets] 警告行。
    """
    path = secrets_path()
    status, new_key, detail = _read_secrets_file(path)
    legacy_key = _legacy_api_key(delta_cfg)
    if status == "ok":
        if legacy_key:
            _warn_once("legacy_ignored", path,
                       "已使用 %s 的 deepseek.api_key，_global.yml 里 jiwen.delta_llm.api_key "
                       "的旧写法被忽略，请删掉那一行" % path)
        return new_key
    if status == "too_open":
        _warn_once("too_open", path,
                   "%s 权限 %s 过宽，未读取；请执行 chmod 600 %s" % (path, detail, path))
    elif status == "unreadable":
        _warn_once("unreadable", path, "%s 读不出（%s），按未配置处理" % (path, detail))
    elif status == "no_key":
        _warn_once("no_key", path,
                   "%s 里没有可用的 deepseek.api_key（应为非空字符串），按未配置处理" % path)
    if legacy_key:
        _warn_once("legacy_in_use", path,
                   "正在使用 _global.yml 里 jiwen.delta_llm.api_key 的旧写法；请移到 %s 的 "
                   "deepseek.api_key，再执行 chmod 600 %s" % (path, path))
    return legacy_key


def load_bot(bot_id: str) -> dict:
    _validate_bot_id(bot_id)
    p = os.path.join(_configs_dir(), f"{bot_id}.yml")
    if not os.path.exists(p):
        raise FileNotFoundError(f"配置不存在：{p}")
    with open(p, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    # 运行时会注入的计算字段不应来自 YAML；先清掉残留，避免旧值绕过年龄闸门。
    cfg.pop("_age", None)
    cfg.pop("_schooling", None)
    cfg.pop("_identity", None)

    today = datetime.date.today()
    age = persona_clock.age_at(cfg, today)
    schooling = persona_clock.schooling_at(cfg, today)
    identity = persona_clock.identity_line(cfg, today)

    # 加工是内存态行为：计算字段只用下划线前缀标记，绝不写回 yml。
    if age is not None:
        cfg["_age"] = age
    if schooling is not None:
        cfg["_schooling"] = schooling
    if identity:
        cfg["_identity"] = identity

    # persona_summary：原字段非空且身份串非空才前置【当前身份】行。
    persona_summary = cfg.get("persona_summary")
    if isinstance(persona_summary, str) and persona_summary.strip() and identity:
        cfg["persona_summary"] = "【当前身份】" + identity + "\n" + persona_summary

    # face_traits：成年才前置无空格年龄；未成年拦截并 warn 一次（生图提示词永不带未成年年龄）。
    face_traits = cfg.get("face_traits")
    if isinstance(face_traits, str) and face_traits.strip():
        if age is not None and age >= persona_clock.MIN_AGE_IN_PROMPT:
            cfg["face_traits"] = f"{age}岁" + face_traits
        elif age is not None and age < persona_clock.MIN_AGE_IN_PROMPT:
            persona_clock._warn(
                persona_clock._bot_id(cfg), "face_traits", "age_below_min",
                "年龄未满 18 岁，已拒绝注入生图提示词", age)

    return cfg


def list_enabled_bots(include_disabled: bool = False) -> list[dict]:
    """枚举所有 configs/<bot>.yml（除 _ 开头的）。

    顶层 ``enabled: false`` 或旧 ``.disabled/<bot>`` 标记的 bot 默认跳过 —— 判定与
    ``bots_registry.is_enabled(cfg, bot_id)`` 同一口径（没写字段 = 启用，只有显式假值才停），
    两处必须一致，否则"停了"在这条路径上不生效。
    ``include_disabled=True`` 只给管理台列表用：停用的 bot 得留在页面上才点得回来。
    """
    bots = []
    d = _configs_dir()
    if not os.path.isdir(d):
        return bots
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".yml"):
            continue
        bot_id = fn[:-4]
        # 只枚举满足安全 bot 标识规则的文件名，_global.yml 等一律跳过。
        if not _is_safe_bot_id(bot_id):
            continue
        try:
            cfg = load_bot(bot_id)
            if not include_disabled and not _enabled(cfg, bot_id):
                continue
            cfg["_bot_id"] = bot_id
            bots.append(cfg)
        except Exception:
            continue
    return bots


# ── bot 自动发现（channels 目录扫描）────────────────────────────────────────
# 目录 bot 判据：名称过安全正则、不在 NON_BOT_DIRS、非符号链接、含 CLAUDE.md 标志物。
# 选 CLAUDE.md 而非 .claude/：前者是每个 bot 工作区的固定锚（新建目录即可被发现），
# 后者是"面板写过 provider 之后"的产物，拿它当判据会把刚建好的新 bot 挡在门外。
NON_BOT_DIRS = frozenset({"media", "group_transcripts"})
BOT_MARKER_FILE = "CLAUDE.md"
# 目录名 ≠ bot_id 的历史失配映射；公开版不预设任何个人目录，保持空表。
DIR_ALIASES: dict = {}


def channels_dir() -> str:
    """channels 根目录；支持公开测试/运维接缝 CLAUDEBOT_CHANNELS_ROOT（与 provider_config 同名接缝）。"""
    return os.environ.get("CLAUDEBOT_CHANNELS_ROOT") or os.path.expanduser("~/.claude/channels")


def _is_bot_dir(name: str, path: str) -> bool:
    """目录是否像一个 bot 工作区：安全名 + 非黑名单 + 非符号链接 + 含标志物（只做存在性检查）。"""
    if not _is_safe_bot_id(name) or name in NON_BOT_DIRS:
        return False
    if os.path.islink(path):
        return False
    return os.path.isfile(os.path.join(path, BOT_MARKER_FILE))


def _yml_bot_ids() -> list[str]:
    """配置目录下所有符合安全命名的 ``<bot>.yml`` 的 bot_id（按名升序，含 load 失败的）。

    与 ``list_enabled_bots`` 同一命名过滤（安全 bot 标识），只是**不尝试加载**——
    供发现列表把「yml 在但加载失败」的 bot 补回名单。
    """
    configs = _configs_dir()
    try:
        names = sorted(os.listdir(configs))
    except OSError:
        return []
    return [fn[:-4] for fn in names if fn.endswith(".yml") and _is_safe_bot_id(fn[:-4])]


def list_discovered_bots() -> list[dict]:
    """发现的 bot 名单 = yml 段 ∪ 目录段（各按 bot_id 升序后拼接）。

    返回轻量记录 ``{"bot_id","display_name","has_config","bot_dir"}``（不做人设加工）。
    与 ``list_enabled_bots()`` 分工：本函数供面板名单类消费方；
    需要完整 yml 人设的消费方继续用前者。
    不抛异常：channels 根不存在/不可读 → 仅 yml 段。
    """
    yml_bots = []
    claimed_names = set()
    claimed_realpaths = set()
    for cfg in list_enabled_bots(include_disabled=True):
        bot_id = cfg["_bot_id"]
        claimed_names.add(bot_id)
        raw_dir = cfg.get("bot_channel_path")
        if isinstance(raw_dir, str) and raw_dir:
            bot_dir = raw_dir
            expanded = os.path.expanduser(raw_dir)
            if os.path.isabs(expanded):
                claimed_realpaths.add(os.path.realpath(expanded))
        else:
            bot_dir = os.path.join(channels_dir(), bot_id)
        yml_bots.append({"bot_id": bot_id,
                         "display_name": cfg.get("display_name") or bot_id,
                         "has_config": True,
                         "bot_dir": bot_dir})
    # 配置损坏的 bot：yml 文件在、但加载失败（list_enabled_bots 会静默跳过）→ 用 broken 标记
    # 补回名单并写 stderr 一行点名。绝不静默消失：面板上再也看不到它，用户无从排查。
    for bot_id in _yml_bot_ids():
        if bot_id in claimed_names:
            continue
        sys.stderr.write("[config_loader] bot 配置损坏，无法加载: %s.yml\n" % bot_id)
        yml_bots.append({"bot_id": bot_id,
                         "display_name": bot_id,
                         "has_config": False,
                         "bot_dir": os.path.join(channels_dir(), bot_id),
                         "broken": True})
    yml_bots.sort(key=lambda item: item["bot_id"])

    dir_bots = []
    root = channels_dir()
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    for name in names:
        if name in DIR_ALIASES or name in claimed_names:
            continue
        path = os.path.join(root, name)
        if not _is_bot_dir(name, path):
            continue
        try:
            if os.path.realpath(path) in claimed_realpaths:
                continue
        except OSError:
            continue
        dir_bots.append({"bot_id": name,
                         "display_name": name,
                         "has_config": False,
                         "bot_dir": path})
    dir_bots.sort(key=lambda item: item["bot_id"])

    return yml_bots + dir_bots


def in_sleep_hours(cfg: dict, now) -> bool:
    """now 在 sleep_hours 任一区间内 → True"""
    from generators.situation import _time_in_range
    for spec in cfg.get("sleep_hours", []):
        if _time_in_range(now.time(), spec):
            return True
    return False
