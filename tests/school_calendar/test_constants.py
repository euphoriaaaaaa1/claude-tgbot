# -*- coding: utf-8 -*-
"""school_calendar 的四个公开常量与中文标签表。"""
import school_calendar


def test_四个学期状态常量的取值():
    assert school_calendar.IN_SESSION == "in_session"
    assert school_calendar.WINTER_BREAK == "winter_break"
    assert school_calendar.SUMMER_BREAK == "summer_break"
    assert school_calendar.EARLY_RETURN == "early_return"


def test_中文标签表覆盖全部四个状态():
    assert school_calendar.TERM_LABEL_ZH == {
        "in_session": "在校",
        "winter_break": "寒假",
        "summer_break": "暑假",
        "early_return": "提前返校期",
    }


def test_中文标签全部为非空字符串():
    for key, label in school_calendar.TERM_LABEL_ZH.items():
        assert isinstance(label, str) and label.strip(), key


def test_未知学期状态取标签_返回None而不报错():
    """提示词拼接要靠这条容错：拿不到标签就不输出，不能抛 KeyError。"""
    assert school_calendar.TERM_LABEL_ZH.get("foo") is None
