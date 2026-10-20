# -*- coding: utf-8 -*-
"""暑假 / 提前返校区间的首尾边界，以及闰年、农历范围两端。"""
from datetime import date

import pytest

import school_calendar as SC

import sc_helpers as H

PLAIN_SUMMER = {"summer_start": "07-01", "summer_end": "08-31"}


def test_暑假第一天算暑假():
    assert SC.term_state(H.cfg(PLAIN_SUMMER), date(2026, 7, 1)) == "summer_break"


def test_暑假前一天在校():
    assert SC.term_state(H.cfg(PLAIN_SUMMER), date(2026, 6, 30)) == "in_session"


def test_暑假最后一天算暑假():
    assert SC.term_state(H.cfg(PLAIN_SUMMER), date(2026, 8, 31)) == "summer_break"


def test_暑假结束后一天在校():
    assert SC.term_state(H.cfg(PLAIN_SUMMER), date(2026, 9, 1)) == "in_session"


def test_提前返校第一天():
    assert SC.term_state(H.cfg(H.TEACHER_SC), date(2026, 8, 26)) == "early_return"


def test_提前返校前一天仍是暑假():
    assert SC.term_state(H.cfg(H.TEACHER_SC), date(2026, 8, 25)) == "summer_break"


def test_提前返校最后一天等于暑假最后一天():
    assert SC.term_state(H.cfg(H.TEACHER_SC), date(2026, 8, 31)) == "early_return"


def test_提前返校等于暑假结束日_只有那一天算提前返校():
    sc = {"summer_start": "07-01", "summer_end": "08-31", "early_return": "08-31"}
    assert SC.term_state(H.cfg(sc), date(2026, 8, 30)) == "summer_break"
    assert SC.term_state(H.cfg(sc), date(2026, 8, 31)) == "early_return"


def test_暑假只有一天_起止相同():
    sc = {"summer_start": "07-05", "summer_end": "07-05"}
    assert SC.term_state(H.cfg(sc), date(2026, 7, 5)) == "summer_break"


@pytest.mark.parametrize("day", [date(2026, 7, 4), date(2026, 7, 6)])
def test_一天暑假的前后两天都在校(day):
    sc = {"summer_start": "07-05", "summer_end": "07-05"}
    assert SC.term_state(H.cfg(sc), day) == "in_session"


def test_提前返校等于暑假开始日_整段都算提前返校():
    sc = {"summer_start": "07-01", "summer_end": "08-31", "early_return": "07-01"}
    c = H.cfg(sc)
    assert SC.term_state(c, date(2026, 7, 1)) == "early_return"
    assert SC.term_state(c, date(2026, 8, 31)) == "early_return"


def test_闰年229落在配置区间内_算暑假():
    """替用户想到的：MM-DD 区间比较必须认得 2/29。2028 是闰年。"""
    sc = {"summer_start": "02-28", "summer_end": "03-01"}
    assert SC.term_state(H.cfg(sc), date(2028, 2, 29)) == "summer_break"


def test_闰年229不在任何假期区间时_是在校():
    """2028 寒假 01-11~02-10，暑假 07-01~08-31，2/29 两头都不沾。"""
    assert SC.term_state(H.cfg(H.TEACHER_SC), date(2028, 2, 29)) == "in_session"


def test_农历支持范围下端1900_不抛异常且返回合法状态():
    got = SC.term_state(H.cfg(H.TEACHER_SC), date(1900, 6, 15))
    assert got in ("in_session", "winter_break", "summer_break", "early_return")


def test_超出农历支持范围的年份_暑假判定仍然正常():
    """算不出春节时只跳过寒假，暑假必须照常工作。"""
    assert SC.term_state(H.cfg(PLAIN_SUMMER), date(2101, 7, 15)) == "summer_break"


def test_超出农历支持范围的年份_寒假降级为在校():
    assert SC.term_state(H.cfg(PLAIN_SUMMER), date(2101, 2, 10)) == "in_session"
