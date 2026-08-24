# -*- coding: utf-8 -*-
"""Black-box acceptance tests for persona_clock.schooling_at (INTERFACE §1.3, §1.7, §4.2)."""
from __future__ import annotations

import datetime as dt
import pytest

import persona_clock  # noqa: F401  # module does not exist yet -> intentionally red


def _no_warn(capsys):
    err = capsys.readouterr().err
    assert "[persona_clock] " not in err


def _has_warn(capsys, *fields: str):
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if line.startswith("[persona_clock] ")]
    assert len(lines) >= 1
    if fields:
        assert any(any(f in line for f in fields) for line in lines)


def _cfg(schooling, suffix: str) -> dict:
    return {"id": f"schooling-{suffix}", "schooling": schooling}


@pytest.mark.parametrize(
    ("schooling", "d", "expected", "want_warn"),
    [
        # INTERFACE §4.2 official fixtures
        ({"stage": "高中", "enrollment_year": 2024}, dt.date(2026, 8, 22), "高三在读", False),
        ({"stage": "高中", "enrollment_year": 2024}, dt.date(2024, 7, 1), "高一在读", False),
        ({"stage": "高中", "enrollment_year": 2024}, dt.date(2025, 6, 30), "高一在读", False),
        ({"stage": "高中", "enrollment_year": 2024}, dt.date(2025, 7, 1), "高二在读", False),
        ({"stage": "高中", "enrollment_year": 2024}, dt.date(2027, 6, 29), "高三在读", False),
        ({"stage": "高中", "enrollment_year": 2024}, dt.date(2027, 6, 30), "已从高中毕业", False),
        ({"stage": "高中", "enrollment_year": 2024}, dt.date(2027, 7, 1), "已从高中毕业", False),
        ({"stage": "高中", "enrollment_year": 2024}, dt.date(2030, 1, 1), "已从高中毕业", False),
        ({"stage": "大学", "enrollment_year": 2027}, dt.date(2027, 7, 1), "大一在读", False),
        ({"stage": "大学", "enrollment_year": 2027}, dt.date(2030, 9, 1), "大四在读", False),
        ({"stage": "大学", "enrollment_year": 2027}, dt.date(2031, 6, 30), "已从大学毕业", False),
        # cross-year boundaries
        ({"stage": "高中", "enrollment_year": 2024}, dt.date(2026, 12, 31), "高三在读", False),
        ({"stage": "高中", "enrollment_year": 2024}, dt.date(2027, 1, 1), "高三在读", False),
        # error contract
        ({"stage": "高中", "enrollment_year": 2024}, dt.date(2024, 6, 30), None, True),
        ({"stage": "初中", "enrollment_year": 2024}, dt.date(2026, 8, 22), None, True),
        ({"stage": "高中", "enrollment_year": "2024"}, dt.date(2026, 8, 22), None, True),
        # missing enrollment_year handled separately below
        # missing schooling key handled separately below
    ],
)
def test_schooling_at_returns_or_warns(capsys, schooling, d, expected, want_warn):
    cfg = _cfg(schooling, f"{schooling['stage']}-{schooling.get('enrollment_year')}-{d.isoformat()}")
    result = persona_clock.schooling_at(cfg, d)
    assert result == expected
    if want_warn:
        _has_warn(capsys, "schooling", "stage", "enrollment_year")
    else:
        _no_warn(capsys)


def test_schooling_at_missing_schooling_key_is_silent(capsys):
    cfg = {"id": "schooling-no-key"}
    assert persona_clock.schooling_at(cfg, dt.date(2026, 8, 22)) is None
    _no_warn(capsys)


@pytest.mark.parametrize(
    ("schooling", "suffix"),
    [
        (None, "none"),
        ("高中", "str"),
        ("高中", "str2"),
        ([], "list"),
        ({"stage": "高中"}, "no-year"),
        ({"stage": "高中", "enrollment_year": True}, "bool-year"),
        ({"stage": "高中", "enrollment_year": "2024"}, "str-year"),
        ({"stage": "高中", "enrollment_year": 1899}, "low-year"),
        ({"stage": "高中", "enrollment_year": 2100}, "high-year"),
        ({"stage": "研究生", "enrollment_year": 2024}, "unknown-stage"),
        ({"stage": "", "enrollment_year": 2024}, "empty-stage"),
        ({"stage": 1, "enrollment_year": 2024}, "int-stage"),
        ({"stage": "高中", "enrollment_year": 2030}, "future-enroll"),  # index < 0
    ],
)
def test_schooling_at_bad_cfg_warns_and_returns_none(capsys, schooling, suffix):
    cfg = _cfg(schooling, suffix)
    assert persona_clock.schooling_at(cfg, dt.date(2026, 8, 22)) is None
    _has_warn(capsys, "schooling", "stage", "enrollment_year")


@pytest.mark.parametrize(
    "bad_cfg",
    [42, "cfg", [], None, True],
)
def test_schooling_at_cfg_not_dict_raises_typeerror(bad_cfg):
    with pytest.raises(TypeError):
        persona_clock.schooling_at(bad_cfg, dt.date(2026, 8, 22))


@pytest.mark.parametrize(
    "bad_d",
    ["2026-08-22", 20260822, None, 1.5, [], {}],
)
def test_schooling_at_d_not_date_raises_typeerror(bad_d):
    cfg = _cfg({"stage": "高中", "enrollment_year": 2024}, "bad-d")
    with pytest.raises(TypeError):
        persona_clock.schooling_at(cfg, bad_d)


def test_schooling_at_datetime_d_is_legal_and_ignores_time():
    cfg = {"id": "schooling-datetime-d", "schooling": {"stage": "高中", "enrollment_year": 2024}}
    assert persona_clock.schooling_at(cfg, dt.datetime(2026, 8, 22, 23, 59)) == "高三在读"


def test_schooling_at_out_of_range_but_before_graduation_returns_none_with_warning(monkeypatch, capsys):
    """M7: if ACADEMIC_ROLLOVER is changed so an out-of-range index can still be before graduation,
    schooling_at must return None + warning instead of IndexError."""
    monkeypatch.setattr(persona_clock, "ACADEMIC_ROLLOVER", (9, 1))
    monkeypatch.setattr(persona_clock, "GRADUATION", (12, 31))
    cfg = {"id": "schooling-m7-range", "schooling": {"stage": "高中", "enrollment_year": 2023}}
    assert persona_clock.schooling_at(cfg, dt.date(2026, 9, 1)) is None
    _has_warn(capsys, "schooling", "stage", "enrollment_year")
