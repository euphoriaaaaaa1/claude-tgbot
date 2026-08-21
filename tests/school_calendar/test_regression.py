# -*- coding: utf-8 -*-
"""REGRESSION GUARDS -- behaviour that must not change."""
from datetime import date, datetime

import pytest

import school_calendar
from generators.situation import get_current_recurring

import sc_helpers as H

TERM_DAY = date(2026, 3, 15)                    # ordinary date inside the term
TERM_MORNING = datetime(2026, 3, 15, 9, 0)
NATIONAL_HOLIDAY = date(2026, 10, 2)            # legal holiday, a Friday
NATIONAL_HOLIDAY_MORNING = datetime(2026, 10, 2, 9, 0)
HOLIDAY_ONLY_NAMES = ("假期在家", "到校自习")


def _full_cfg():
    c = H.cfg(H.TEACHER_SC)
    c["recurring_activities"] = {
        "weekday": H.class_block(),
        "weekend": H.weekend_block(),
        "break": H.break_block(),
        "early_return": H.early_return_block(),
    }
    return c


def test_学期内普通日期_判定为在校():
    assert school_calendar.term_state(H.cfg(H.TEACHER_SC), TERM_DAY) == "in_session"


def test_学期内普通日期_作息仍取自平日或周末块():
    act = get_current_recurring(_full_cfg(), TERM_MORNING)
    assert act.term == "in_session"
    assert act.name in ("上课/办公", "周末休息")
    assert act.name not in HOLIDAY_ONLY_NAMES


def test_法定节假日_学期状态仍是在校():
    """国庆当天不是寒暑假；法定节假日由原有 holiday 逻辑负责，不归校历管。"""
    assert school_calendar.term_state(H.cfg(H.TEACHER_SC), NATIONAL_HOLIDAY) == "in_session"


def test_法定节假日_走周末作息():
    holiday = pytest.importorskip("holiday")
    try:
        workday = holiday.is_workday(NATIONAL_HOLIDAY)
    except Exception as exc:  # 离线且无缓存时无法判定，跳过而不是误报
        pytest.skip("holiday.is_workday 不可用：%r" % (exc,))
    if workday:
        pytest.skip("holiday 数据未识别 2026-10-02 为节假日，无法验证节假日分支")
    act = get_current_recurring(_full_cfg(), NATIONAL_HOLIDAY_MORNING)
    assert act.name == "周末休息"
    assert act.term == "in_session"


def test_无校历配置的角色_暑假期间仍按原行为():
    """不配校历的角色，8/21 也不该被判成假期。"""
    c = H.cfg()  # 没有 school_calendar 键
    assert school_calendar.term_state(c, date(2026, 8, 21)) == "in_session"


def test_无校历配置的角色_作息不取假期块():
    c = H.cfg()
    c["recurring_activities"] = {
        "weekday": H.class_block(),
        "weekend": H.weekend_block(),
        "break": H.break_block(),
    }
    act = get_current_recurring(c, datetime(2026, 8, 21, 9, 0))
    assert act.term == "in_session"
    assert act.name not in HOLIDAY_ONLY_NAMES
