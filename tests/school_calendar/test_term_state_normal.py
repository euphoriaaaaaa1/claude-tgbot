# -*- coding: utf-8 -*-
"""term_state 的正常路径。"""
from datetime import date, datetime

import school_calendar as SC

import sc_helpers as H


def test_没有school_calendar键_恒为在校():
    assert SC.term_state(H.cfg(), date(2026, 8, 21)) == "in_session"


def test_空的school_calendar字典_寒假仍生效():
    """只有 school_calendar 是 dict，寒假判定就生效（不需要任何字段）。"""
    assert SC.term_state(H.cfg({}), date(2026, 2, 17)) == "winter_break"


def test_空的school_calendar字典_暑假不生效():
    assert SC.term_state(H.cfg({}), date(2026, 8, 21)) == "in_session"


def test_暑假区间内的普通一天():
    assert SC.term_state(H.cfg(H.TEACHER_SC), date(2026, 7, 15)) == "summer_break"


def test_暑假结束后一天_回到在校():
    assert SC.term_state(H.cfg(H.STUDENT_SC), date(2026, 8, 30)) == "in_session"


def test_提前返校优先于暑假():
    """8/20 起是提前返校期，即使它同时落在暑假区间里。"""
    assert SC.term_state(H.cfg(H.STUDENT_SC), date(2026, 8, 25)) == "early_return"


def test_没有配置early_return时_整段都是暑假():
    sc = {"summer_start": "07-01", "summer_end": "08-31"}
    assert SC.term_state(H.cfg(sc), date(2026, 8, 26)) == "summer_break"


def test_传入datetime也能判定():
    c = H.cfg(H.TEACHER_SC)
    assert SC.term_state(c, datetime(2026, 8, 21, 23, 59)) == "summer_break"


def test_传入datetime与传入date结果一致():
    c = H.cfg(H.TEACHER_SC)
    assert SC.term_state(c, datetime(2026, 8, 21, 0, 0)) == SC.term_state(c, date(2026, 8, 21))


def test_带时区的datetime_时区被忽略():
    tz = datetime(2026, 8, 21, 23, 30).astimezone()  # 本地时区的 aware datetime
    assert SC.term_state(H.cfg(H.TEACHER_SC), tz) == "summer_break"


def test_返回值永远是四个合法状态之一_全年扫描():
    """替用户想到的：一整年逐日跑，防止某个分支漏 return 返回 None。"""
    allowed = {"in_session", "winter_break", "summer_break", "early_return"}
    c = H.cfg(H.TEACHER_SC)
    d = date(2026, 1, 1)
    while d <= date(2026, 12, 31):
        got = SC.term_state(c, d)
        assert got in allowed, "%s 返回了 %r" % (d, got)
        d = date.fromordinal(d.toordinal() + 1)
