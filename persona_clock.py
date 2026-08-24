# -*- coding: utf-8 -*-
"""角色年龄 / 学制状态：存起点（生日、入学年份），运行时算当前值。

纯计算、无 IO、无第三方依赖。给定相同 (cfg, d) 恒返回相同结果，不修改 cfg。

要点：
- age_at 是计算原语，不受 MIN_AGE_IN_PROMPT 用途级闸门影响；闸门由
  identity_line / config_loader 的注入位执行。
- schooling_at 只回答「到今天为止活到哪一步」，不驱动任何作息/门控逻辑。
- 警告只输出 bot 标识、字段名和截断后的非法值，绝不 dump 整份 cfg（其中含
  token/chat_id）。
"""
from __future__ import annotations

import datetime
import sys

KNOWN_STAGES: dict[str, tuple[str, ...]] = {
    "高中": ("高一", "高二", "高三"),
    "大学": ("大一", "大二", "大三", "大四"),
}

# 学年翻篇日：7 月 1 日起算新学年。与 school_calendar 的 summer_start 是同一
# 件事的两种精度；若日后要逐 bot 精确化，可让本常量退位给 school_calendar 字段。
ACADEMIC_ROLLOVER: tuple[int, int] = (7, 1)
# 毕业日（月, 日）：该日当天起即视为已毕业。
GRADUATION: tuple[int, int] = (6, 30)
# 年龄进入任何对外文本的下限。低于它，年龄一律不注入——生图提示词不进，
# 人设提示词也不进。
MIN_AGE_IN_PROMPT: int = 18

_MAX_AGE = 150
WARN_VALUE_MAX_CHARS = 16
_MISSING = object()

# 进程内警告去重：键为 (bot 标识, 字段名, 错误类型)。
# 不同 bot 的同类错误各自至少一条；同一 bot 的同一错误最多输出一次。
_WARNED: set[tuple[str, str, str]] = set()


def _bot_id(cfg: dict) -> str:
    raw = cfg.get("id")
    return str(raw) if raw else "?"


def _warn(bot_id: str, field: str, kind: str, detail: str, value=_MISSING) -> None:
    key = (bot_id, field, kind)
    if key in _WARNED:
        return
    _WARNED.add(key)
    tail = ""
    if value is not _MISSING:
        tail = "（%s）" % str(value)[:WARN_VALUE_MAX_CHARS]
    sys.stderr.write("[persona_clock] %s: %s %s%s\n" % (bot_id, field, detail, tail))


def _normalize_day(d):
    if not isinstance(d, datetime.date):
        raise TypeError("d 必须是 datetime.date 或其子类，收到 %s" % type(d).__name__)
    if isinstance(d, datetime.datetime):
        return d.date()
    return d


def _parse_birthday(value):
    """把 birthday 字段归一成 datetime.date；非法时返回 None。"""
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        if len(value) != 10 or value[4] != "-" or value[7] != "-":
            return None
        try:
            return datetime.date.fromisoformat(value)
        except ValueError:
            return None
    return None


def age_at(cfg: dict, d: datetime.date) -> int | None:
    """返回 cfg 描述的角色在 d 当天的周岁；算不出来返回 None。"""
    if not isinstance(cfg, dict):
        raise TypeError("cfg 必须是 dict，收到 %s" % type(cfg).__name__)
    d = _normalize_day(d)

    if "birthday" not in cfg:
        return None  # 缺字段是合法语义，静默不注入

    bot_id = _bot_id(cfg)
    birthday = _parse_birthday(cfg["birthday"])
    if birthday is None:
        _warn(bot_id, "birthday", "bad_value", "不是合法的 YYYY-MM-DD 日期，已忽略",
              cfg["birthday"])
        return None

    if birthday > d:
        _warn(bot_id, "birthday", "future", "出生日在未来，已忽略", cfg["birthday"])
        return None

    age = d.year - birthday.year - ((d.month, d.day) < (birthday.month, birthday.day))
    if age > _MAX_AGE:
        _warn(bot_id, "birthday", "too_old", "超过最大可支持年龄，已忽略", cfg["birthday"])
        return None
    return age


def schooling_at(cfg: dict, d: datetime.date) -> str | None:
    """返回学制状态短语；算不出来返回 None。"""
    if not isinstance(cfg, dict):
        raise TypeError("cfg 必须是 dict，收到 %s" % type(cfg).__name__)
    d = _normalize_day(d)

    if "schooling" not in cfg:
        return None  # 成年/无学制是合法语义，静默

    bot_id = _bot_id(cfg)
    sc = cfg["schooling"]
    if not isinstance(sc, dict):
        _warn(bot_id, "schooling", "not_a_dict", "不是字典，整块学制已忽略", sc)
        return None

    stage = sc.get("stage", _MISSING)
    if stage is _MISSING:
        _warn(bot_id, "stage", "missing", "缺少学段 stage")
        return None
    if not isinstance(stage, str) or stage not in KNOWN_STAGES:
        _warn(bot_id, "stage", "unknown", "不是已知学段，已忽略", stage)
        return None

    enrollment_year = sc.get("enrollment_year", _MISSING)
    if enrollment_year is _MISSING:
        _warn(bot_id, "enrollment_year", "missing", "缺少入学年份 enrollment_year")
        return None
    if isinstance(enrollment_year, bool) or not isinstance(enrollment_year, int):
        _warn(bot_id, "enrollment_year", "bad_type", "入学年份必须是 int，已忽略",
              enrollment_year)
        return None
    if not 1900 <= enrollment_year < 2100:
        _warn(bot_id, "enrollment_year", "out_of_range",
              "入学年份不在 [1900, 2100)，已忽略", enrollment_year)
        return None

    labels = KNOWN_STAGES[stage]
    graduation_date = datetime.date(
        enrollment_year + len(labels), GRADUATION[0], GRADUATION[1]
    )
    if d >= graduation_date:
        return "已从%s毕业" % stage

    academic_year = d.year if (d.month, d.day) >= ACADEMIC_ROLLOVER else d.year - 1
    idx = academic_year - enrollment_year
    if idx < 0:
        _warn(bot_id, "enrollment_year", "future",
              "入学年晚于当前学年，已忽略", enrollment_year)
        return None
    if idx >= len(labels):
        _warn(bot_id, "enrollment_year", "out_of_range",
              "年级序号越界且尚未到毕业日，已忽略", enrollment_year)
        return None
    return "%s在读" % labels[idx]


def identity_line(cfg: dict, d: datetime.date) -> str:
    """拼装身份串；永不返回 None，算不出来返回空串。

    年龄段模板固定为 f"{age} 岁"（数字与「岁」之间有一个半角空格）；
    与 face_traits 的 f"{age}岁"（无空格）是有意不一致，两处测试各测各的。
    """
    if not isinstance(cfg, dict):
        raise TypeError("cfg 必须是 dict，收到 %s" % type(cfg).__name__)
    d = _normalize_day(d)

    age = age_at(cfg, d)
    schooling = schooling_at(cfg, d)

    parts: list[str] = []
    if age is not None and age >= MIN_AGE_IN_PROMPT:
        parts.append("%d 岁" % age)
    if schooling:
        parts.append(schooling)
    return " · ".join(parts)
