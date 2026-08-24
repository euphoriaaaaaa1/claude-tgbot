# -*- coding: utf-8 -*-
"""Black-box acceptance tests for persona_clock.age_at (INTERFACE §1.2, §1.6, §4.1)."""
from __future__ import annotations

import datetime as dt
import pytest

import persona_clock  # noqa: F401  # module does not exist yet -> intentionally red


_MISSING = object()


def _no_warn(capsys):
    err = capsys.readouterr().err
    assert "[persona_clock] " not in err


def _has_warn(capsys, field: str = "birthday"):
    err = capsys.readouterr().err
    warn_lines = [line for line in err.splitlines() if line.startswith("[persona_clock] ")]
    assert len(warn_lines) >= 1
    assert any(field in line for line in warn_lines)


def _age_case_id(birthday, d, expected, want_warn):
    """Human-readable parametrize id so test output maps straight to TEST-PLAN."""
    if birthday is _MISSING:
        b = "(no birthday)"
    elif isinstance(birthday, (dt.date, dt.datetime)):
        b = birthday.isoformat()
    else:
        b = repr(birthday)
    d_str = d.isoformat() if isinstance(d, (dt.date, dt.datetime)) else repr(d)
    if expected is None:
        exp = "None+warn" if want_warn else "None"
    else:
        exp = str(expected)
    return f"birthday={b} d={d_str} -> {exp}"


_AGE_CASES = [
    # INTERFACE §4.1 official fixtures
    ("1988-05-06", dt.date(2026, 8, 22), 38, False),
    ("1982-08-12", dt.date(2026, 8, 22), 44, False),
    ("1991-04-10", dt.date(2026, 8, 22), 35, False),
    ("2007-09-09", dt.date(2026, 8, 22), 18, False),
    ("2007-09-09", dt.date(2026, 9, 8), 18, False),
    ("2007-09-09", dt.date(2026, 9, 9), 19, False),
    (dt.date(1988, 5, 6), dt.date(2026, 8, 22), 38, False),
    (dt.datetime(1988, 5, 6, 0, 0), dt.date(2026, 8, 22), 38, False),
    ("2008-02-29", dt.date(2027, 2, 28), 18, False),
    ("2008-02-29", dt.date(2027, 3, 1), 19, False),
    ("2008-02-29", dt.date(2028, 2, 29), 20, False),
    ("2026-08-22", dt.date(2026, 8, 22), 0, False),
    ("2027-01-01", dt.date(2026, 8, 22), None, True),
    ("1088-05-06", dt.date(2026, 8, 22), None, True),
    ("1988/05/06", dt.date(2026, 8, 22), None, True),
    ("1988-02-30", dt.date(2026, 8, 22), None, True),
    # missing birthday: silent None
    (_MISSING, dt.date(2026, 8, 22), None, False),
    # datetime is legal and normalizes to date
    ("1988-05-06", dt.datetime(2026, 8, 22, 23, 59), 38, False),
    # cross-year boundary
    ("2007-12-31", dt.date(2027, 1, 1), 19, False),
    # 150 is the legal upper boundary
    ("1876-08-22", dt.date(2026, 8, 22), 150, False),
    # extra user-visible boundary: exactly 1 year old
    ("2025-08-22", dt.date(2026, 8, 22), 1, False),
    # extra invalid-format / empty-string cases from the error contract
    ("", dt.date(2026, 8, 22), None, True),
    ("1988-13-01", dt.date(2026, 8, 22), None, True),
    ("1987-02-29", dt.date(2026, 8, 22), None, True),
    ("1988-05-06T00:00", dt.date(2026, 8, 22), None, True),
    ("88-5-6", dt.date(2026, 8, 22), None, True),
    (19880506, dt.date(2026, 8, 22), None, True),
    (None, dt.date(2026, 8, 22), None, True),  # birthday key present but None
    (["1988-05-06"], dt.date(2026, 8, 22), None, True),
    ({"year": 1988}, dt.date(2026, 8, 22), None, True),
    (True, dt.date(2026, 8, 22), None, True),
]


@pytest.mark.parametrize(
    ("birthday", "d", "expected", "want_warn"),
    _AGE_CASES,
    ids=[_age_case_id(*case) for case in _AGE_CASES],
)
def test_age_at_returns_or_warns(capsys, birthday, d, expected, want_warn):
    if birthday is _MISSING:
        cfg = {"id": "age-missing"}
    else:
        cfg = {"id": f"age-{repr(birthday)[:16]}", "birthday": birthday}
    result = persona_clock.age_at(cfg, d)
    if expected is None:
        assert result is None
    else:
        assert result == expected
    if want_warn:
        _has_warn(capsys, "birthday")
    else:
        _no_warn(capsys)


def test_age_at_datetime_d_is_legal_and_ignores_time():
    cfg = {"id": "age-datetime-d", "birthday": "1988-05-06"}
    assert persona_clock.age_at(cfg, dt.datetime(2026, 8, 22, 23, 59)) == 38
    assert persona_clock.age_at(cfg, dt.date(2026, 8, 22)) == 38


def test_age_at_no_birthday_key_is_silent(capsys):
    cfg = {"id": "age-no-key-1"}
    assert persona_clock.age_at(cfg, dt.date(2026, 8, 22)) is None
    _no_warn(capsys)


@pytest.mark.parametrize(
    "bad_cfg",
    [
        42,
        "cfg",
        [],
        None,
        True,
    ],
)
def test_age_at_cfg_not_dict_raises_typeerror(bad_cfg):
    with pytest.raises(TypeError):
        persona_clock.age_at(bad_cfg, dt.date(2026, 8, 22))


@pytest.mark.parametrize(
    "bad_d",
    [
        "2026-08-22",
        20260822,
        None,
        1.5,
        [],
        {},
    ],
)
def test_age_at_d_not_date_raises_typeerror(bad_d):
    cfg = {"id": "age-bad-d", "birthday": "1988-05-06"}
    with pytest.raises(TypeError):
        persona_clock.age_at(cfg, bad_d)


def test_warning_truncates_long_invalid_birthday_violation(capsys):
    long_value = "1988-05-06T00:00:00"
    cfg = {"id": "age-long-warn", "birthday": long_value}
    assert persona_clock.age_at(cfg, dt.date(2026, 8, 22)) is None
    err = capsys.readouterr().err
    assert "[persona_clock] " in err
    # The whole raw value must not leak into the warning.
    assert long_value not in err
