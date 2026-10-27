# -*- coding: utf-8 -*-
"""pub-draft 白盒：生活设定的嵌套读写（jiwen.delta_hints）、I5 拒写、字段表提示文案。

只经 life_settings 的公开函数与模块级 NESTED_FIELDS；落盘只在 tmp_path。
"""
import hashlib

import pytest
import yaml

import bots_registry
import life_settings


@pytest.fixture
def configs(tmp_path, monkeypatch):
    d = tmp_path / "configs"
    d.mkdir()
    monkeypatch.setattr(bots_registry, "configs_dir", lambda: str(d))
    return d


def _write(configs, body, bot="b1"):
    p = configs / ("%s.yml" % bot)
    p.write_text(body, encoding="utf-8")
    return p


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


NESTED = """id: b1
chat_id: "1"
bio: 旧简介
jiwen:
  # 注释要保住
  delta_hints: 旧规则
  rates_override:
    connection_per_min: 0.1
  x_unknown: keep
speaking_threshold: 旧阈值
"""


def test_字段表顺序类型上限与提示文案():
    fields = life_settings.field_table()
    keys = [f["key"] for f in fields]
    assert keys.index("jiwen_delta_hints") == keys.index("speaking_threshold") + 1
    by = {f["key"]: f for f in fields}
    assert by["jiwen_delta_hints"]["type"] == "textarea"
    assert by["jiwen_delta_hints"]["max_len"] == 4000
    assert by["persona_summary"]["empty_hint"] == life_settings.PROACTIVE_EMPTY_HINT
    assert by["speaking_threshold"]["empty_hint"] == life_settings.PROACTIVE_EMPTY_HINT
    assert by["sleep_hours"]["empty_hint"] == life_settings.SLEEP_EMPTY_HINT
    assert "empty_hint" not in by["jiwen_delta_hints"]
    assert "empty_hint" not in by["bio"]
    assert life_settings.NESTED_FIELDS == {"jiwen_delta_hints": ("jiwen", "delta_hints")}


def test_嵌套写保留注释未知子键与其他顶层键(configs):
    path = _write(configs, NESTED)
    before = yaml.safe_load(path.read_text(encoding="utf-8"))
    life_settings.set_life("b1", {"jiwen_delta_hints": "新规则"})
    text = path.read_text(encoding="utf-8")
    after = yaml.safe_load(text)
    assert after["jiwen"]["delta_hints"] == "新规则"
    assert after["jiwen"]["rates_override"] == before["jiwen"]["rates_override"]
    assert after["jiwen"]["x_unknown"] == "keep"
    assert "# 注释要保住" in text
    assert after["bio"] == "旧简介" and after["speaking_threshold"] == "旧阈值"
    assert life_settings.get_life("b1")["values"]["jiwen_delta_hints"] == "新规则"


def test_读回缺省为空串(configs):
    _write(configs, "id: b1\njiwen:\n  rates_override:\n    connection_per_min: 0.1\n")
    assert life_settings.get_life("b1")["values"]["jiwen_delta_hints"] == ""
    _write(configs, "id: b1\n")
    assert life_settings.get_life("b1")["values"]["jiwen_delta_hints"] == ""


def test_清空子键_只剩它时整块删除(configs):
    path = _write(configs, "id: b1\njiwen:\n  delta_hints: 旧\n")
    life_settings.set_life("b1", {"jiwen_delta_hints": ""})
    assert "jiwen" not in yaml.safe_load(path.read_text(encoding="utf-8"))


def test_清空子键_还有其他子键时保留(configs):
    path = _write(configs, "id: b1\njiwen:\n  delta_hints: 旧\n  rates_override:\n    connection_per_min: 0.1\n")
    life_settings.set_life("b1", {"jiwen_delta_hints": ""})
    after = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "delta_hints" not in after["jiwen"]
    assert after["jiwen"]["rates_override"] == {"connection_per_min": 0.1}


def test_jiwen不是键值表_拒写且文件不变(configs):
    path = _write(configs, "id: b1\njiwen:\n  - 1\n  - 2\n")
    before = _sha(path)
    with pytest.raises(life_settings.LifeSettingsError) as ei:
        life_settings.set_life("b1", {"jiwen_delta_hints": "新"})
    assert ei.value.code == "internal"
    assert _sha(path) == before


def test_flow写法无注释可写_含注释拒写且文件不变(configs):
    path = _write(configs, 'id: b1\njiwen: {delta_hints: "旧", rates_override: {connection_per_min: 0.1}}\n')
    life_settings.set_life("b1", {"jiwen_delta_hints": "新"})
    after = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert after["jiwen"]["delta_hints"] == "新"
    assert after["jiwen"]["rates_override"] == {"connection_per_min": 0.1}

    path2 = _write(configs, 'id: b1\n# 这行注释让改写必须失败\n'
                            'jiwen: {delta_hints: "旧", rates_override: {connection_per_min: 0.1}}\n', bot="b2")
    before2 = _sha(path2)
    with pytest.raises(life_settings.LifeSettingsError) as ei:
        life_settings.set_life("b2", {"jiwen_delta_hints": "新"})
    assert ei.value.code == "internal"
    assert _sha(path2) == before2


def test_未知子键与超长被拒且文件不变(configs):
    path = _write(configs, NESTED)
    before = _sha(path)
    with pytest.raises(life_settings.LifeSettingsError) as ei:
        life_settings.set_life("b1", {"jiwen_rates_override": {}})
    assert ei.value.code == "invalid" and "jiwen_rates_override" in ei.value.fields
    with pytest.raises(life_settings.LifeSettingsError) as ei2:
        life_settings.set_life("b1", {"jiwen_delta_hints": "字" * 4001})
    assert ei2.value.code == "invalid"
    with pytest.raises(life_settings.LifeSettingsError) as ei3:
        life_settings.set_life("b1", {"jiwen_delta_hints": 12345})
    assert ei3.value.code == "invalid"
    assert _sha(path) == before
