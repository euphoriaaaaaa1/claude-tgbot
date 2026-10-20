# -*- coding: utf-8 -*-
"""config_loader 的身份注入验收（黑盒）。

只调 load_bot / load_global / list_enabled_bots 三个公开入口，配置全部写在 tmp_path
的假目录里；仓库自带的 configs/ 只读不写，也不读任何真实人设内容。
"""
from __future__ import annotations

import datetime as dt
import re

import pytest

import config_loader
from bg_helpers import use_fake_configs, write_bot_config, write_global_config


def _academic_year(today: dt.date) -> int:
    return today.year if (today.month, today.day) >= (7, 1) else today.year - 1


def _adult_38_birthday(today: dt.date) -> str:
    """无论今天是哪天，都恰好算出 38 岁的生日。"""
    return dt.date(today.year - 38, 1, 1).isoformat()


def _minor_17_birthday(today: dt.date) -> str:
    return dt.date(today.year - 17, 1, 1).isoformat()


def _minor_17_schooling(today: dt.date) -> dict:
    """让 17 岁角色恒为高三在读的入学年。"""
    return {"stage": "高中", "enrollment_year": _academic_year(today) - 2}


def _no_warn(capsys):
    err = capsys.readouterr().err
    assert "[persona_clock] " not in err, "不该有 persona_clock 告警"


def _has_warn(capsys, *keywords):
    err = capsys.readouterr().err
    warn_lines = [ln for ln in err.splitlines() if ln.startswith("[persona_clock] ")]
    assert len(warn_lines) >= 1, "期望至少一条 persona_clock 告警"
    for keyword in keywords:
        assert any(keyword in ln for ln in warn_lines), f"告警里必须出现 {keyword!r}"


def test_load_bot_adult_injects_age_identity_summary_and_face_traits(tmp_path, monkeypatch):
    configs_dir = use_fake_configs(tmp_path, monkeypatch)
    today = dt.date.today()
    write_bot_config(configs_dir, "adult38", {
        "id": "adult38",
        "birthday": _adult_38_birthday(today),
        "persona_summary": "原文案",
        "face_traits": "原脸",
    })

    loaded = config_loader.load_bot("adult38")

    assert loaded["_age"] == 38
    assert "_schooling" not in loaded
    assert loaded["_identity"] == "38 岁"
    assert loaded["persona_summary"] == "【当前身份】38 岁\n原文案"
    assert loaded["face_traits"] == "38岁原脸"


def test_load_bot_adult_with_schooling_injects_schooling_too(tmp_path, monkeypatch):
    configs_dir = use_fake_configs(tmp_path, monkeypatch)
    today = dt.date.today()
    write_bot_config(configs_dir, "adult38-school", {
        "id": "adult38-school",
        "birthday": _adult_38_birthday(today),
        "schooling": {"stage": "大学", "enrollment_year": _academic_year(today) - 20},
        "persona_summary": "原文案",
        "face_traits": "原脸",
    })

    loaded = config_loader.load_bot("adult38-school")

    assert loaded["_age"] == 38
    assert loaded["_schooling"] == "已从大学毕业"
    assert loaded["_identity"] == "38 岁 · 已从大学毕业"
    assert loaded["persona_summary"].startswith("【当前身份】38 岁 · 已从大学毕业\n")
    assert loaded["face_traits"] == "38岁原脸"


def test_load_bot_minor_17_with_schooling_hides_age_and_warns_face(capsys, tmp_path, monkeypatch):
    configs_dir = use_fake_configs(tmp_path, monkeypatch)
    today = dt.date.today()
    write_bot_config(configs_dir, "minor17school", {
        "id": "minor17school",
        "birthday": _minor_17_birthday(today),
        "schooling": _minor_17_schooling(today),
        "persona_summary": "原文案",
        "face_traits": "原脸",
    })

    loaded = config_loader.load_bot("minor17school")

    assert loaded["_age"] == 17
    assert loaded["_schooling"] == "高三在读"
    assert loaded["_identity"] == "高三在读"
    assert not re.search(r"\d+\s*岁", loaded["_identity"])
    assert loaded["persona_summary"] == "【当前身份】高三在读\n原文案"
    assert loaded["face_traits"] == "原脸", "未成年年龄绝不能进生图提示词"
    _has_warn(capsys, "face_traits")


def test_load_bot_minor_17_without_schooling_hides_identity_and_warns_face(capsys, tmp_path, monkeypatch):
    configs_dir = use_fake_configs(tmp_path, monkeypatch)
    today = dt.date.today()
    write_bot_config(configs_dir, "minor17noschool", {
        "id": "minor17noschool",
        "birthday": _minor_17_birthday(today),
        "persona_summary": "原文案",
        "face_traits": "原脸",
    })

    loaded = config_loader.load_bot("minor17noschool")

    assert loaded["_age"] == 17
    assert "_schooling" not in loaded
    assert "_identity" not in loaded
    assert loaded["persona_summary"] == "原文案"
    assert loaded["face_traits"] == "原脸"
    _has_warn(capsys, "face_traits")


def test_load_bot_no_birthday_no_schooling_is_silent_and_unchanged(capsys, tmp_path, monkeypatch):
    configs_dir = use_fake_configs(tmp_path, monkeypatch)
    write_bot_config(configs_dir, "nobirthday", {
        "id": "nobirthday", "persona_summary": "原文案", "face_traits": "原脸",
    })

    loaded = config_loader.load_bot("nobirthday")

    assert "_age" not in loaded
    assert "_schooling" not in loaded
    assert "_identity" not in loaded
    assert loaded["persona_summary"] == "原文案"
    assert loaded["face_traits"] == "原脸"
    _no_warn(capsys)


def test_load_bot_removes_stale_runtime_keys(tmp_path, monkeypatch):
    """yml 里手写的 _age/_identity 属于伪造的运行时字段，必须先清掉再算。"""
    configs_dir = use_fake_configs(tmp_path, monkeypatch)
    write_bot_config(configs_dir, "stale-runtime", {
        "id": "stale-runtime",
        "_age": 999,
        "_schooling": "已从大学退学",
        "_identity": "999 岁 · 已从大学退学",
        "persona_summary": "原文案",
        "face_traits": "原脸",
    })

    loaded = config_loader.load_bot("stale-runtime")

    assert "_age" not in loaded
    assert "_schooling" not in loaded
    assert "_identity" not in loaded
    assert loaded["persona_summary"] == "原文案"
    assert loaded["face_traits"] == "原脸"


def test_load_bot_birthday_without_persona_summary_does_not_create_key(tmp_path, monkeypatch):
    configs_dir = use_fake_configs(tmp_path, monkeypatch)
    write_bot_config(configs_dir, "no-summary", {
        "id": "no-summary",
        "birthday": _adult_38_birthday(dt.date.today()),
        "face_traits": "原脸",
    })

    loaded = config_loader.load_bot("no-summary")

    assert loaded["_age"] == 38
    assert loaded["_identity"] == "38 岁"
    assert "persona_summary" not in loaded


@pytest.mark.parametrize("summary_value", ["", "   ", " \t\r\n "],
                         ids=["empty", "spaces", "blank-lines"])
def test_load_bot_blank_persona_summary_is_not_prefixed(tmp_path, monkeypatch, summary_value):
    configs_dir = use_fake_configs(tmp_path, monkeypatch)
    write_bot_config(configs_dir, "blank-summary", {
        "id": "blank-summary",
        "birthday": _adult_38_birthday(dt.date.today()),
        "persona_summary": summary_value,
        "face_traits": "原脸",
    })

    loaded = config_loader.load_bot("blank-summary")

    assert loaded["persona_summary"] == summary_value
    assert not loaded["persona_summary"].startswith("【当前身份】")


def test_load_bot_missing_face_traits_does_not_create_key(tmp_path, monkeypatch):
    configs_dir = use_fake_configs(tmp_path, monkeypatch)
    write_bot_config(configs_dir, "no-face", {
        "id": "no-face",
        "birthday": _adult_38_birthday(dt.date.today()),
        "persona_summary": "原文案",
    })

    loaded = config_loader.load_bot("no-face")

    assert "face_traits" not in loaded
    assert loaded["persona_summary"] == "【当前身份】38 岁\n原文案"


def test_load_bot_unrelated_fields_unchanged(tmp_path, monkeypatch):
    configs_dir = use_fake_configs(tmp_path, monkeypatch)
    original_fields = {
        "bio": "原 bio",
        "speaking_threshold": "原阈值",
        "school_calendar": {"summer_start": "07-01", "summer_end": "08-31"},
        "recurring_activities": [1, 2],
        "nested": {"x": [1, 2], "y": None},
    }
    write_bot_config(configs_dir, "fields", {
        "id": "fields",
        "birthday": _adult_38_birthday(dt.date.today()),
        "persona_summary": "原文案",
        "face_traits": "原脸",
        **original_fields,
    })

    loaded = config_loader.load_bot("fields")

    for key, original in original_fields.items():
        assert loaded[key] == original, f"无关字段被改动：{key}"
    assert loaded["persona_summary"] == "【当前身份】38 岁\n原文案"
    assert loaded["face_traits"] == "38岁原脸"


def test_load_bot_is_idempotent_and_prefix_not_stacked(tmp_path, monkeypatch):
    configs_dir = use_fake_configs(tmp_path, monkeypatch)
    write_bot_config(configs_dir, "idempotent", {
        "id": "idempotent",
        "birthday": _adult_38_birthday(dt.date.today()),
        "persona_summary": "原文案",
        "face_traits": "原脸",
    })

    first = config_loader.load_bot("idempotent")
    second = config_loader.load_bot("idempotent")

    assert first == second
    assert first["persona_summary"].count("【当前身份】") == 1
    assert first["face_traits"] == "38岁原脸"


def test_load_bot_does_not_write_back(tmp_path, monkeypatch):
    """注入是内存态行为：三个入口跑完，磁盘上的 yml 必须逐字节不变。"""
    configs_dir = use_fake_configs(tmp_path, monkeypatch)
    write_bot_config(configs_dir, "noback", {
        "id": "noback",
        "birthday": _adult_38_birthday(dt.date.today()),
        "persona_summary": "原文案",
        "face_traits": "原脸",
    })
    write_global_config(configs_dir, {"user_display_name": "测试用户"})

    paths = sorted(configs_dir.glob("*.yml"))
    before = {p.name: p.read_bytes() for p in paths}

    config_loader.load_bot("noback")
    config_loader.load_global()
    config_loader.list_enabled_bots()

    assert {p.name: p.read_bytes() for p in paths} == before


def test_load_global_does_not_inject_computed_keys_or_prefix(tmp_path, monkeypatch):
    configs_dir = use_fake_configs(tmp_path, monkeypatch)
    write_global_config(configs_dir, {"user_display_name": "测试用户",
                                      "server": {"key": "global-value"}})

    loaded = config_loader.load_global()

    assert loaded.get("user_display_name") == "测试用户"
    assert loaded.get("server", {}).get("key") == "global-value"
    for key in ("_age", "_schooling", "_identity"):
        assert key not in loaded
    for field, value in loaded.items():
        if isinstance(value, str):
            assert not value.startswith("【当前身份】"), f"全局字段被前置了身份行：{field}"


def test_list_enabled_bots_matches_load_bot(tmp_path, monkeypatch):
    configs_dir = use_fake_configs(tmp_path, monkeypatch)
    today = dt.date.today()
    write_bot_config(configs_dir, "alpha", {
        "id": "alpha", "birthday": _adult_38_birthday(today),
        "persona_summary": "原文案 alpha", "face_traits": "原脸 alpha",
    })
    write_bot_config(configs_dir, "beta", {
        "id": "beta", "birthday": _minor_17_birthday(today),
        "schooling": _minor_17_schooling(today),
        "persona_summary": "原文案 beta", "face_traits": "原脸 beta",
    })
    write_global_config(configs_dir, {"user_display_name": "测试用户"})

    by_id = {b["_bot_id"]: b for b in config_loader.list_enabled_bots()}

    assert set(by_id) == {"alpha", "beta"}, "_global.yml 不该被当成 bot 枚举出来"
    for bot_id in ("alpha", "beta"):
        direct = config_loader.load_bot(bot_id)
        item = {k: v for k, v in by_id[bot_id].items() if k != "_bot_id"}
        assert item == direct, f"{bot_id}: list_enabled_bots 与 load_bot 的加工结果必须一致"
