# -*- coding: utf-8 -*-
"""Acceptance tests for formatter.format_self_initiate identity insertion (INTERFACE §3 and §4.5)."""
from __future__ import annotations

import datetime as dt
import re

import pytest

import config_loader
from bg_helpers import (
    render_self_initiate,
    use_fake_configs,
    write_bot_config,
)


def _separator_indices(text: str):
    return [i for i, line in enumerate(text.splitlines()) if "以下是你当下的情境" in line]


def _academic_year(today: dt.date) -> int:
    return today.year if (today.month, today.day) >= (7, 1) else today.year - 1


def _adult_38_birthday(today: dt.date) -> str:
    return dt.date(today.year - 38, 1, 1).isoformat()


def _minor_17_birthday(today: dt.date) -> str:
    return dt.date(today.year - 17, 1, 1).isoformat()


def _minor_17_schooling(today: dt.date) -> dict:
    return {"stage": "高中", "enrollment_year": _academic_year(today) - 2}


def test_p1_identity_line_appears_exactly_once():
    out = render_self_initiate("18 岁 · 高三在读")

    lines = out.splitlines()
    identity_lines = [ln for ln in lines if "你的当前身份" in ln]
    assert identity_lines == ["你的当前身份：18 岁 · 高三在读"]


def test_p2_identity_default_empty_omits_line():
    out = render_self_initiate()

    assert "你的当前身份" not in out


def test_p3_identity_whitespace_omits_line():
    out = render_self_initiate("   ")

    assert "你的当前身份" not in out


def test_p4_identity_line_precedes_term_status_when_present():
    out = render_self_initiate("18 岁 · 高三在读")
    lines = out.splitlines()
    identity_line = [ln for ln in lines if "你的当前身份" in ln]
    assert len(identity_line) == 1
    identity_idx = lines.index(identity_line[0])
    term_idx = [i for i, ln in enumerate(lines) if ln.startswith("学期状态")]
    if term_idx:
        assert identity_idx < term_idx[0]


def test_p5_non_str_identity_raises_typeerror():
    for bad in (None, 123, [], {}, True):
        with pytest.raises(TypeError):
            render_self_initiate(bad)


def test_p6_identity_is_first_line_after_separator_when_no_term_row():
    out = render_self_initiate("38 岁")
    lines = out.splitlines()
    separators = _separator_indices(out)
    assert len(separators) == 1
    sep_idx = separators[0]
    assert lines[sep_idx + 1].startswith("你的当前身份：38 岁")
    assert lines.count("你的当前身份：38 岁") == 1


def test_minor_17_with_schooling_identity_through_formatter_has_no_age(tmp_path, monkeypatch):
    """End-to-end negative proof: load_bot -> _identity -> formatter text has no age."""
    configs_dir = use_fake_configs(tmp_path, monkeypatch)
    today = dt.date.today()
    data = {
        "id": "minor-school-e2e",
        "birthday": _minor_17_birthday(today),
        "schooling": _minor_17_schooling(today),
        "persona_summary": "原文案",
        "face_traits": "原脸",
    }
    write_bot_config(configs_dir, "minor-school-e2e", data)

    loaded = config_loader.load_bot("minor-school-e2e")
    identity = loaded.get("_identity", "")

    assert identity == "高三在读"
    out = render_self_initiate(identity)
    assert "你的当前身份：高三在读" in out
    assert not re.search(r"\d+\s*岁", out)


def test_minor_17_without_schooling_identity_through_formatter_has_no_age(tmp_path, monkeypatch):
    """End-to-end negative proof for the no-schooling minor path."""
    configs_dir = use_fake_configs(tmp_path, monkeypatch)
    today = dt.date.today()
    data = {
        "id": "minor-noschool-e2e",
        "birthday": _minor_17_birthday(today),
        "persona_summary": "原文案",
        "face_traits": "原脸",
    }
    write_bot_config(configs_dir, "minor-noschool-e2e", data)

    loaded = config_loader.load_bot("minor-noschool-e2e")
    identity = loaded.get("_identity", "")

    assert identity == ""
    assert "你的当前身份" not in render_self_initiate(identity)
    assert not re.search(r"\d+\s*岁", render_self_initiate(identity))


def test_adult_38_identity_through_formatter_contains_age(tmp_path, monkeypatch):
    """End-to-end positive proof: adult _identity reaches formatter text with its age."""
    configs_dir = use_fake_configs(tmp_path, monkeypatch)
    today = dt.date.today()
    data = {
        "id": "adult-e2e",
        "birthday": _adult_38_birthday(today),
        "persona_summary": "原文案",
        "face_traits": "原脸",
    }
    write_bot_config(configs_dir, "adult-e2e", data)

    loaded = config_loader.load_bot("adult-e2e")
    identity = loaded.get("_identity", "")

    assert identity == "38 岁"
    out = render_self_initiate(identity)
    assert "你的当前身份：38 岁" in out
    assert re.search(r"38\s*岁", out)
