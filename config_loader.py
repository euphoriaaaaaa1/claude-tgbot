"""加载 bot 配置和全局配置。

以 ``_`` 开头的键是运行时注入的计算字段（年龄/学制/身份），不在 yml 文件里；
``load_bot`` 每次从磁盘重读后在内存态加工，绝不写回。
"""
import datetime
import os
import yaml

import persona_clock
from bots_registry import is_enabled as _enabled   # 停用判定只有一处，别在这里再写一遍

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIGS_DIR = os.path.join(PROJECT_ROOT, "configs")
GLOBAL_CFG_PATH = os.path.join(CONFIGS_DIR, "_global.yml")


def load_global() -> dict:
    if not os.path.exists(GLOBAL_CFG_PATH):
        return {}
    with open(GLOBAL_CFG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_bot(bot_id: str) -> dict:
    p = os.path.join(CONFIGS_DIR, f"{bot_id}.yml")
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

    顶层 ``enabled: false`` 的 bot 默认跳过 —— 判定与 ``bots_registry._enabled`` 同一口径
    （没写字段 = 启用，只有显式假值才停），两处必须一致，否则"停了"在这条路径上不生效。
    ``include_disabled=True`` 只给管理台列表用：停用的 bot 得留在页面上才点得回来。
    """
    bots = []
    if not os.path.isdir(CONFIGS_DIR):
        return bots
    for fn in sorted(os.listdir(CONFIGS_DIR)):
        if fn.startswith("_") or not fn.endswith(".yml"):
            continue
        bot_id = fn[:-4]
        try:
            cfg = load_bot(bot_id)
            if not include_disabled and not _enabled(cfg):
                continue
            cfg["_bot_id"] = bot_id
            bots.append(cfg)
        except Exception:
            continue
    return bots


def in_sleep_hours(cfg: dict, now) -> bool:
    """now 在 sleep_hours 任一区间内 → True"""
    from generators.situation import _time_in_range
    for spec in cfg.get("sleep_hours", []):
        if _time_in_range(now.time(), spec):
            return True
    return False
