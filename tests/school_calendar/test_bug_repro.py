# -*- coding: utf-8 -*-
"""BUG REPRODUCTION -- these must FAIL before the fix and PASS after it.

Confirmed bug: every day of the summer holiday is treated as
a working day, so the teacher character "goes to school to teach" all summer.
"""
from datetime import date, datetime

import pytest

import school_calendar
from generators.situation import get_current_recurring

import sc_helpers as H

SUMMER_DAY = date(2026, 8, 21)          # Friday, middle of the summer holiday
SUMMER_MORNING = datetime(2026, 8, 21, 9, 0)   # inside the old 07:30-12:00 slot
SCHOOL_WORDS = ("上课", "办公", "批作业", "备课")


@pytest.mark.bug_repro
def test_暑假中的日期_对教师角色_判定为暑假():
    assert school_calendar.term_state(H.cfg(H.TEACHER_SC), SUMMER_DAY) == "summer_break"


@pytest.mark.bug_repro
def test_暑假上午_教师作息不含上课办公():
    c = H.cfg(H.TEACHER_SC)
    c["recurring_activities"] = {
        "weekday": H.class_block(),
        "weekend": H.weekend_block(),
        "break": H.break_block(),
    }
    act = get_current_recurring(c, SUMMER_MORNING)
    blob = "%s|%s" % (act.name, act.description)
    for word in SCHOOL_WORDS:
        assert word not in blob, "暑假里仍出现『%s』：%s" % (word, blob)


@pytest.mark.bug_repro
def test_暑假上午_活动取自break块():
    c = H.cfg(H.TEACHER_SC)
    c["recurring_activities"] = {
        "weekday": H.class_block(),
        "weekend": H.weekend_block(),
        "break": H.break_block(),
    }
    act = get_current_recurring(c, SUMMER_MORNING)
    assert act.name == "假期在家"
    assert act.state == "free"
    assert act.term == "summer_break"


@pytest.mark.bug_repro
def test_学生角色_8月21日_判定为提前返校():
    assert school_calendar.term_state(H.cfg(H.STUDENT_SC), SUMMER_DAY) == "early_return"


@pytest.mark.bug_repro
def test_学生角色_提前返校日_作息为到校自习且不标为上课():
    c = H.cfg(H.STUDENT_SC)
    c["recurring_activities"] = {
        "weekday": H.class_block(),
        "weekend": H.weekend_block(),
        "break": H.break_block(),
        "early_return": H.early_return_block(),
    }
    act = get_current_recurring(c, SUMMER_MORNING)
    assert act.name == "到校自习"
    assert act.state == "busy_other"
    assert act.term == "early_return"


@pytest.mark.bug_repro
def test_整个暑假区间_每一天都不是在校():
    c = H.cfg(H.TEACHER_SC)
    d = date(2026, 7, 1)
    bad = []
    while d <= date(2026, 8, 31):
        if school_calendar.term_state(c, d) == "in_session":
            bad.append(d.isoformat())
        d = date.fromordinal(d.toordinal() + 1)
    assert bad == [], "这些暑假日期被判成了在校：%s" % bad[:5]
