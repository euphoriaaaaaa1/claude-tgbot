# -*- coding: utf-8 -*-
"""寒假区间（由春节锚点推算）的边界。

春节日期取自契约表，直接硬编码断言。
"""
from datetime import date, timedelta

import pytest

import school_calendar as SC

import sc_helpers as H

# 年份 -> (寒假起点, 寒假终点)，winter_days = 15，闭区间
WINTER_15 = {
    2025: (date(2025, 1, 14), date(2025, 2, 13)),
    2026: (date(2026, 2, 2), date(2026, 3, 4)),
    2027: (date(2027, 1, 22), date(2027, 2, 21)),
    2028: (date(2028, 1, 11), date(2028, 2, 10)),
    2029: (date(2029, 1, 29), date(2029, 2, 28)),
    2030: (date(2030, 1, 19), date(2030, 2, 18)),
}


@pytest.mark.parametrize("year", sorted(WINTER_15))
def test_寒假第一天算寒假(year):
    assert SC.term_state(H.cfg({}), WINTER_15[year][0]) == "winter_break"


@pytest.mark.parametrize("year", sorted(WINTER_15))
def test_寒假最后一天算寒假(year):
    assert SC.term_state(H.cfg({}), WINTER_15[year][1]) == "winter_break"


@pytest.mark.parametrize("year", sorted(WINTER_15))
def test_寒假开始前一天不算寒假(year):
    assert SC.term_state(H.cfg({}), WINTER_15[year][0] - timedelta(days=1)) == "in_session"


@pytest.mark.parametrize("year", sorted(WINTER_15))
def test_寒假结束后一天不算寒假(year):
    assert SC.term_state(H.cfg({}), WINTER_15[year][1] + timedelta(days=1)) == "in_session"


def test_春节当天算寒假():
    assert SC.term_state(H.cfg({}), date(2026, 2, 17)) == "winter_break"


def test_默认寒假共31天():
    """契约裁决：winter_days=15 时寒假是 15+1+15=31 天，不是 30 天。"""
    c = H.cfg({})
    start, end = WINTER_15[2026]
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    assert len(days) == 31
    assert all(SC.term_state(c, d) == "winter_break" for d in days)


def test_十二月整月都不是寒假():
    """真实春节最早 1/21，默认 winter_days 下 12 月恒不落在寒假里。"""
    c = H.cfg(H.TEACHER_SC)
    bad = [d for d in (date(2026, 12, i) for i in range(1, 32))
           if SC.term_state(c, d) != "in_session"]
    assert bad == []


def test_寒假长度可按角色调整_起点前移():
    c = H.cfg({"winter_days": 20})
    assert SC.term_state(c, date(2026, 1, 28)) == "winter_break"


def test_寒假长度可按角色调整_新起点前一天仍在校():
    c = H.cfg({"winter_days": 20})
    assert SC.term_state(c, date(2026, 1, 27)) == "in_session"


def test_寒假长度为0_只有春节当天算寒假():
    c = H.cfg({"winter_days": 0})
    assert SC.term_state(c, date(2026, 2, 17)) == "winter_break"


@pytest.mark.parametrize("day", [date(2026, 2, 16), date(2026, 2, 18)])
def test_寒假长度为0_春节前后一天都在校(day):
    assert SC.term_state(H.cfg({"winter_days": 0}), day) == "in_session"


def test_寒假长度上限182合法():
    """182 是契约允许的最大值：2026 春节前推 182 天 = 2025-08-19。"""
    assert SC.term_state(H.cfg({"winter_days": 182}), date(2026, 1, 1)) == "winter_break"


def test_跨年寒假_起点落在上一年12月():
    """winter_days=100 时 2026 春节的寒假从 2025-11-09 开始，
    2025-12-25 只能靠"看下一年春节"才判得出来。"""
    assert SC.term_state(H.cfg({"winter_days": 100}), date(2025, 12, 25)) == "winter_break"


def test_跨年寒假_同一天在默认配置下是在校():
    assert SC.term_state(H.cfg({}), date(2025, 12, 25)) == "in_session"
