# -*- coding: utf-8 -*-
"""字段写错时的降级结果（这里只断结果，警告在另一个文件断）。"""
from datetime import date

import pytest

import school_calendar as SC

import sc_helpers as H

BAD_DATE_VALUES = ["7-1", "8-20", "2026-07-01", 707, None, "", "07-1", "0701",
                   "02-30", "13-01", "00-05", "12-32"]


@pytest.mark.parametrize("bad", BAD_DATE_VALUES)
def test_暑假开始日写错_跳过暑假判定(bad):
    sc = {"summer_start": bad, "summer_end": "08-31"}
    assert SC.term_state(H.cfg(sc), date(2026, 8, 21)) == "in_session"


@pytest.mark.parametrize("bad", BAD_DATE_VALUES)
def test_暑假结束日写错_跳过暑假判定(bad):
    sc = {"summer_start": "07-01", "summer_end": bad}
    assert SC.term_state(H.cfg(sc), date(2026, 8, 21)) == "in_session"


def test_暑假开始日缺失_跳过暑假判定():
    assert SC.term_state(H.cfg({"summer_end": "08-31"}), date(2026, 8, 21)) == "in_session"


def test_暑假结束日缺失_跳过暑假判定():
    assert SC.term_state(H.cfg({"summer_start": "07-01"}), date(2026, 8, 21)) == "in_session"


def test_暑假字段写错时_寒假判定照常工作():
    """一个字段坏掉不能连累另一维度。"""
    sc = {"summer_start": "02-30", "summer_end": "08-31"}
    assert SC.term_state(H.cfg(sc), date(2026, 2, 17)) == "winter_break"


def test_暑假起止颠倒_跳过暑假判定():
    sc = {"summer_start": "09-01", "summer_end": "07-01"}
    assert SC.term_state(H.cfg(sc), date(2026, 8, 21)) == "in_session"


@pytest.mark.parametrize("day", [date(2026, 9, 15), date(2026, 6, 15), date(2026, 1, 5)])
def test_暑假起止颠倒_不得被当成跨年区间(day):
    """反向用例：09-01~07-01 不许被解释成『跨过年底的暑假』。"""
    sc = {"summer_start": "09-01", "summer_end": "07-01"}
    assert SC.term_state(H.cfg(sc), day) == "in_session"


@pytest.mark.parametrize("bad", ["8-20", 820, "02-30", None, "2026-08-20", ""])
def test_提前返校日写错_暑假判定不受影响(bad):
    sc = {"summer_start": "07-01", "summer_end": "08-31", "early_return": bad}
    assert SC.term_state(H.cfg(sc), date(2026, 8, 21)) == "summer_break"


@pytest.mark.parametrize("out_of_range", ["06-01", "09-05"])
def test_提前返校日落在暑假区间外_被忽略(out_of_range):
    sc = {"summer_start": "07-01", "summer_end": "08-31", "early_return": out_of_range}
    assert SC.term_state(H.cfg(sc), date(2026, 8, 21)) == "summer_break"


def test_提前返校日在暑假区间外_那一天本身也不算提前返校():
    """反向用例：early_return=06-01 被忽略后，6/1 当天不能变成提前返校期。"""
    sc = {"summer_start": "07-01", "summer_end": "08-31", "early_return": "06-01"}
    assert SC.term_state(H.cfg(sc), date(2026, 6, 1)) == "in_session"


def test_只有提前返校日而暑假字段无效_整体降级为在校():
    sc = {"summer_start": "02-30", "summer_end": "08-31", "early_return": "08-20"}
    assert SC.term_state(H.cfg(sc), date(2026, 8, 21)) == "in_session"


@pytest.mark.parametrize("bad", ["15", -3, True, False, 500, 15.0, None, [15], "abc"])
def test_寒假天数写错_回落默认15天(bad):
    """默认 15 时 2026-02-02 是寒假第一天，用它验证确实回落到了 15。"""
    assert SC.term_state(H.cfg({"winter_days": bad}), date(2026, 2, 2)) == "winter_break"


@pytest.mark.parametrize("bad", ["15", -3, True, 500, 183])
def test_寒假天数写错_回落后区间外仍是在校(bad):
    assert SC.term_state(H.cfg({"winter_days": bad}), date(2026, 2, 1)) == "in_session"


def test_寒假天数超过上限183_回落默认15():
    """182 合法、183 越界回落：2026-01-01 只有在大区间下才会是寒假。"""
    assert SC.term_state(H.cfg({"winter_days": 183}), date(2026, 1, 1)) == "in_session"
