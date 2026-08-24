# -*- coding: utf-8 -*-
"""Black-box acceptance tests for persona_clock.identity_line (INTERFACE §1.4, §1.8, §4.3)."""
from __future__ import annotations

import datetime as dt
import pytest

import persona_clock  # noqa: F401  # module does not exist yet -> intentionally red


def test_identity_full_adult_student():
    cfg = {
        "id": "identity-i1",
        "birthday": "2007-09-09",
        "schooling": {"stage": "高中", "enrollment_year": 2024},
    }
    assert persona_clock.identity_line(cfg, dt.date(2026, 8, 22)) == "18 岁 · 高三在读"


def test_identity_birthday_only_adult():
    cfg = {"id": "identity-i2", "birthday": "1988-05-06"}
    assert persona_clock.identity_line(cfg, dt.date(2026, 8, 22)) == "38 岁"


def test_identity_schooling_only():
    cfg = {"id": "identity-i3", "schooling": {"stage": "高中", "enrollment_year": 2024}}
    assert persona_clock.identity_line(cfg, dt.date(2026, 8, 22)) == "高三在读"


def test_identity_neither_returns_empty_string():
    cfg = {"id": "identity-i4"}
    assert persona_clock.identity_line(cfg, dt.date(2026, 8, 22)) == ""


def test_identity_invalid_birthday_does_not_break_schooling():
    cfg = {
        "id": "identity-i5",
        "birthday": "1988/05/06",
        "schooling": {"stage": "高中", "enrollment_year": 2024},
    }
    assert persona_clock.identity_line(cfg, dt.date(2026, 8, 22)) == "高三在读"


def test_identity_minor_age_is_hidden_but_schooling_remains():
    cfg = {
        "id": "identity-i6",
        "birthday": "2008-09-09",
        "schooling": {"stage": "高中", "enrollment_year": 2024},
    }
    # On 2026-08-22 this bot is 17 (< MIN_AGE_IN_PROMPT); age must not appear.
    assert persona_clock.identity_line(cfg, dt.date(2026, 8, 22)) == "高三在读"


def test_identity_minor_without_schooling_is_empty_string():
    cfg = {"id": "identity-i7", "birthday": "2008-09-09"}
    assert persona_clock.identity_line(cfg, dt.date(2026, 8, 22)) == ""


def test_identity_age_at_18_boundary_is_shown():
    cfg = {"id": "identity-boundary-18", "birthday": "2008-09-09"}
    # After the 18th birthday the full age text is allowed.
    assert persona_clock.identity_line(cfg, dt.date(2026, 9, 9)) == "18 岁"


@pytest.mark.parametrize("bad_cfg", [42, "cfg", [], None, True])
def test_identity_cfg_not_dict_raises_typeerror(bad_cfg):
    with pytest.raises(TypeError):
        persona_clock.identity_line(bad_cfg, dt.date(2026, 8, 22))


@pytest.mark.parametrize("bad_d", ["2026-08-22", 20260822, None, 1.5, [], {}])
def test_identity_d_not_date_raises_typeerror(bad_d):
    with pytest.raises(TypeError):
        persona_clock.identity_line({"id": "identity-bad-d"}, bad_d)


def test_identity_cfg_is_not_mutated():
    cfg = {
        "id": "identity-pure",
        "birthday": "2007-09-09",
        "schooling": {"stage": "高中", "enrollment_year": 2024},
        "persona_summary": "原值",
    }
    before = dict(cfg)
    before_schooling = dict(cfg["schooling"])
    persona_clock.identity_line(cfg, dt.date(2026, 8, 22))
    assert cfg == before
    assert cfg["schooling"] == before_schooling


def test_identity_warning_is_per_bot_deduplicated(capsys):
    cfg_a = {"id": "identity-warn-a", "birthday": "bad/birthday"}
    cfg_b = {"id": "identity-warn-b", "birthday": "bad/birthday"}
    persona_clock.identity_line(cfg_a, dt.date(2026, 8, 22))
    persona_clock.identity_line(cfg_b, dt.date(2026, 8, 22))
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if line.startswith("[persona_clock] ")]
    assert len(lines) >= 2  # each bot gets its own warning; same-bot repeat may be suppressed
