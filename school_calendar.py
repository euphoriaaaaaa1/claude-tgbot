# -*- coding: utf-8 -*-
"""学期状态判定：在校 / 寒假 / 暑假 / 提前返校期。

只回答一个问题——"今天这个角色在上学吗"。判定全靠本地计算：暑假是每个 bot
自己配的一组固定 MM-DD（逐年复用，不做逐年表），寒假由春节公历日期两侧对称
推算。

- 零联网、零文件读写。春节取自 lunardate（requirements.txt 既有依赖，
  generators/world.py 已在用），支持区间 [1900, 2100)，越界即跳过寒假判定。
- 1900–2099 年无闰正月，故"农历正月初一 = 春节"在整个支持区间恒成立。
- 配置写错一律不抛异常，只降级 + 往 stderr 报一次警。警告只允许出现 bot 标识、
  字段名和截断后的非法值——configs/*.yml 里有 token 和 chat_id，绝不 dump cfg。

前提假设（挪 early_return 窗口时必须复查）：该窗口现在落在 8 月下旬，其间无任何
法定节假日，因此本模块不与 holiday.py 交叉判优先级，不做工作日/周末细分。
"""
from __future__ import annotations

import datetime
import functools
import sys

IN_SESSION = "in_session"
WINTER_BREAK = "winter_break"
SUMMER_BREAK = "summer_break"
EARLY_RETURN = "early_return"

TERM_LABEL_ZH = {
    IN_SESSION: "在校",
    WINTER_BREAK: "寒假",
    SUMMER_BREAK: "暑假",
    EARLY_RETURN: "提前返校期",
}

DEFAULT_WINTER_DAYS = 15
MAX_WINTER_DAYS = 182      # 半年，再长就不是寒假了，按配错处理
WARN_VALUE_MAX_CHARS = 16  # 非法值最多回显这么多字符，防有人把凭证填进日期字段
_LEAP_YEAR = 2000          # 校验 MM-DD 用闰年，好让 "02-29" 算合法日历日

_MISSING = object()

# 进程内警告去重：键为 (bot 标识, 字段名, 错误类型)。带 bot 标识是为了让多 bot
# 同进程时，每个 bot 的同类错误各出一条，不被先出现的那个吞掉。
_WARNED: set[tuple[str, str, str]] = set()


def _bot_id(cfg: dict) -> str:
    """警告用的 bot 标识。缺 id 不能因此崩，给个占位符。"""
    raw = cfg.get("id")
    return str(raw) if raw else "?"


def _warn(bot_id: str, field: str, kind: str, detail: str, value=_MISSING) -> None:
    """同一 (bot, 字段, 错误) 进程内只报一次，防 tick 循环刷屏。"""
    key = (bot_id, field, kind)
    if key in _WARNED:
        return
    _WARNED.add(key)
    tail = ""
    if value is not _MISSING:
        tail = "（%s）" % str(value)[:WARN_VALUE_MAX_CHARS]
    sys.stderr.write("[school_calendar] %s: %s %s%s\n" % (bot_id, field, detail, tail))


def _parse_mmdd(value) -> tuple[int, int] | None:
    """把 "MM-DD" 解析成 (月, 日)。非字符串 / 格式不对 / 不是合法日历日都返回 None。"""
    if not isinstance(value, str) or len(value) != 5 or value[2] != "-":
        return None
    mm_s, dd_s = value[:2], value[3:]
    if not (mm_s.isdigit() and dd_s.isdigit()):
        return None
    try:
        mm, dd = int(mm_s), int(dd_s)
        datetime.date(_LEAP_YEAR, mm, dd)
    except ValueError:
        return None
    return (mm, dd)


def _field_mmdd(sc: dict, field: str, bot_id: str):
    """读一个 MM-DD 字段。返回 (月, 日)、None（缺失）或 _MISSING（有值但非法）。

    缺失与非法要分开，因为契约对二者的警告文案不同。
    """
    value = sc.get(field, _MISSING)
    if value is _MISSING:
        return None
    parsed = _parse_mmdd(value)
    if parsed is None:
        _warn(bot_id, field, "bad_value", "不是合法的 MM-DD 日期，已忽略", value)
        return _MISSING
    return parsed


@functools.lru_cache(maxsize=None)
def _spring_festival(year: int):
    """该年春节的公历日期；农历能力不可用或年份越界时返回 None（不抛）。

    结果按年缓存：同一年在整个进程里只算一次。失败也缓存，避免每 tick 重试
    一次注定失败的 import。
    """
    try:
        import lunardate
        return lunardate.LunarDate(year, 1, 1).toSolarDate()
    except Exception:
        return None


def _early_return_day(sc: dict, bot_id: str, start, end):
    """提前返校日。缺失/非法/越界一律返回 None，且都不连累暑假判定。"""
    day = _field_mmdd(sc, "early_return", bot_id)
    if not isinstance(day, tuple):
        return None
    if not (start <= day <= end):
        _warn(bot_id, "early_return", "out_of_range",
              "不在暑假区间内，已忽略", "%02d-%02d" % day)
        return None
    if day == start:
        # 合法，但这么配"暑假"状态永远出不来，八成是把返校日当成放假日写了
        _warn(bot_id, "early_return", "equals_summer_start",
              "等于暑假开始日，整个暑假都会算作提前返校期")
    return day


def _summer_window(sc: dict, bot_id: str):
    """解析暑假三字段。返回 (起, 止, 提前返校日或 None)；暑假不可用时返回 None。"""
    start = _field_mmdd(sc, "summer_start", bot_id)
    end = _field_mmdd(sc, "summer_end", bot_id)
    if start is None or end is None:
        # 两个都不写是"只跟寒假"的合法写法，合成一条警告，不刷两条
        _warn(bot_id, "summer_start/summer_end", "missing",
              "缺少暑假起止日期，已跳过暑假判定")
    usable = isinstance(start, tuple) and isinstance(end, tuple)
    if usable and start > end:
        # 起点晚于终点只可能是配错，绝不解释成"跨过年底的暑假"
        _warn(bot_id, "summer_start/summer_end", "reversed",
              "暑假起点晚于终点，已跳过暑假判定")
        usable = False
    if not usable:
        if "early_return" in sc:
            _warn(bot_id, "early_return", "no_summer",
                  "暑假区间不可用，已忽略该字段")
        return None
    return (start, end, _early_return_day(sc, bot_id, start, end))


def _winter_days(sc: dict, bot_id: str) -> int:
    """寒假半径（天）。缺失静默取默认；写错则报警后回落默认。"""
    value = sc.get("winter_days", _MISSING)
    if value is _MISSING:
        return DEFAULT_WINTER_DAYS
    # bool 是 int 的子类，但 winter_days: true 显然是配错了，单独挡掉
    if (isinstance(value, bool) or not isinstance(value, int)
            or not 0 <= value <= MAX_WINTER_DAYS):
        _warn(bot_id, "winter_days", "invalid",
              "不是 0~%d 的整数，已回落默认 %d" % (MAX_WINTER_DAYS, DEFAULT_WINTER_DAYS),
              value)
        return DEFAULT_WINTER_DAYS
    return value


def _in_winter_break(d: datetime.date, days: int, bot_id: str) -> bool:
    """寒假 = [春节 − days, 春节 + days] 闭区间，共 2*days+1 天。

    同时看 d.year 与 d.year+1 两个春节：winter_days 配得够大时（如 100），
    12 月的日期要靠下一年的春节才判得出来。
    """
    span = datetime.timedelta(days=days)
    lunar_unavailable = False
    for year in (d.year, d.year + 1):
        spring_festival = _spring_festival(year)
        if spring_festival is None:
            lunar_unavailable = True
            continue
        if spring_festival - span <= d <= spring_festival + span:
            return True
    if lunar_unavailable:
        _warn(bot_id, "winter_break", "no_spring_festival",
              "算不出春节公历日期（农历能力不可用或年份超出 [1900, 2100)），已跳过寒假判定")
    return False


def term_state(cfg: dict, d: datetime.date) -> str:
    """判定某 bot 在某天的学期状态，返回本模块四个常量之一。

    除参数类型错误抛 TypeError 外不抛任何异常；无副作用：不联网、不读写文件、
    不修改 cfg（包括不把默认值写回去）。同一 (cfg, d) 恒返回同一结果。
    """
    if not isinstance(cfg, dict):
        raise TypeError("cfg 必须是 dict，收到 %s" % type(cfg).__name__)
    if not isinstance(d, datetime.date):
        raise TypeError("d 必须是 datetime.date 或其子类，收到 %s" % type(d).__name__)
    if isinstance(d, datetime.datetime):
        d = d.date()  # datetime 是 date 的子类；时刻与 tzinfo 一律忽略

    sc = cfg.get("school_calendar", _MISSING)
    if sc is _MISSING:
        return IN_SESSION  # 不跟校历的角色，静默走原来的路
    bot_id = _bot_id(cfg)
    if not isinstance(sc, dict):
        _warn(bot_id, "school_calendar", "not_a_dict", "不是字典，整块校历已忽略", sc)
        return IN_SESSION

    summer = _summer_window(sc, bot_id)
    if summer is not None:
        start, end, early = summer
        today = (d.month, d.day)
        if early is not None and early <= today <= end:
            return EARLY_RETURN
        if start <= today <= end:
            return SUMMER_BREAK

    if _in_winter_break(d, _winter_days(sc, bot_id), bot_id):
        return WINTER_BREAK
    return IN_SESSION
