# -*- coding: utf-8 -*-
"""Plain builders shared by the school_calendar tests.

Functions only -- no fixtures, no module-level mutable state that a test can
change. Every test calls these to build a *fresh* config dict, so tests are
order independent.

All ids and dates below are invented for testing. Nothing here is real.
"""
import itertools

_SEQ = itertools.count()


def unique_id(prefix="accbot"):
    """A bot id no other test uses.

    The contract de-dups warnings per (bot id, field, error) for the whole
    process lifetime, so reusing an id across tests would let one test
    swallow another test's expected warning.
    """
    return "%s%d" % (prefix, next(_SEQ))


# --- school_calendar blocks (sample values, invented for the tests) --------
# A teacher: summer holiday 07-01 ~ 08-31, back at school from 08-26.
TEACHER_SC = {"summer_start": "07-01", "summer_end": "08-31", "early_return": "08-26"}
# A senior student: shorter holiday, voluntary self-study from 08-20.
STUDENT_SC = {"summer_start": "07-12", "summer_end": "08-29", "early_return": "08-20"}


def cfg(school_calendar=None, bot_id=None, **extra):
    """A bot config dict. Omit school_calendar to leave the key out entirely."""
    out = {"id": bot_id or unique_id()}
    if school_calendar is not None:
        out["school_calendar"] = school_calendar
    out.update(extra)
    return out


# --- recurring_activities blocks ------------------------------------------
def class_block():
    """The buggy weekday routine -- teacher at school."""
    return [
        {"name": "上课/办公", "when": "07:30-12:00", "state": "busy_work",
         "description": "在学校上课、批作业"},
        {"name": "上课/办公", "when": "13:30-17:00", "state": "busy_work",
         "description": "下午课和备课"},
    ]


def weekend_block():
    return [{"name": "周末休息", "when": "00:00-23:00", "state": "free",
             "description": "周末在家躺着"}]


def break_block():
    return [{"name": "假期在家", "when": "00:00-23:00", "state": "free",
             "description": "在家待着，自己安排时间"}]


def early_return_block():
    return [{"name": "到校自习", "when": "00:00-23:00", "state": "busy_other",
             "description": "在教室自习写卷子"}]
