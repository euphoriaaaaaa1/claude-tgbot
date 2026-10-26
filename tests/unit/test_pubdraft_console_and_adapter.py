# -*- coding: utf-8 -*-
"""pub-draft 白盒：管理台留空提示的模板挂点（B5b 的静态覆盖）+ claude 适配器生活设定错误码映射。

B5b 的渲染行为需要 DOM，验收的 bun 用例跳过了；这里读模板脚本块，静态断言元素、
纯函数、渲染调用与 input 监听都在，且只用 textContent（不拼 innerHTML）。
"""
import re
from pathlib import Path

import pytest

import bots_registry
from adapters.base import AdapterError
from adapters.claude_local import ClaudeLocalAdapter

TEMPLATE = Path(__file__).resolve().parents[2] / "moments" / "templates" / "console.html"


def _html():
    return TEMPLATE.read_text(encoding="utf-8")


def _script_block():
    m = re.search(r'<script id="lifeHintsLogic">(.*?)</script>', _html(), re.S)
    assert m, "缺少独立的 lifeHintsLogic 脚本块"
    return m.group(1)


def test_提示条元素存在且默认隐藏():
    m = re.search(r'<div[^>]*id="lifeEmptyHint"[^>]*>', _html())
    assert m, "缺少 id=lifeEmptyHint 的提示条"
    tag = m.group(0)
    assert "role=\"status\"" in tag
    assert re.search(r"\bhidden\b", tag), tag
    assert "banner" in tag


def test_纯函数与刷新函数在独立脚本块里():
    block = _script_block()
    assert "function lifeEmptyHints(" in block
    assert "function refreshLifeHint(" in block
    assert "window.LifeHints" in block
    # 空值口径：字符串 trim、空数组、空对象、null/undefined 都算没填
    assert "v.trim() === ''" in block
    assert "v.length === 0" in block
    assert "Object.keys(v).length === 0" in block
    assert "v === null || v === undefined" in block
    # 提示去重、只在 empty_hint 存在时收
    assert "out.includes(hint)" in block
    assert "typeof f.empty_hint === 'string'" in block


def test_提示条只写_textContent_与_hidden():
    block = _script_block()
    assert "bar.textContent = hints.join('')" in block
    assert "bar.hidden = hints.length === 0" in block
    assert "innerHTML" not in block
    # 没有生活设定数据（null）时隐藏且不抛错
    assert "bar.textContent = ''; bar.hidden = true; return;" in block


def test_渲染与_input_监听挂点():
    html = _html()
    # renderProfile 末尾刷新提示条
    assert "document.getElementById('voiceSave').disabled = PROF.saving || BUSY;\n  refreshLifeHint();" in html
    # renderProfileArea 的早退分支也要隐藏提示条
    m = re.search(r"if \(!on\) \{[^}]*refreshLifeHint\(\); return; \}", html)
    assert m, "早退分支没有刷新提示条"
    # lifeRows 上的委托 input 监听
    assert "document.getElementById('lifeRows').addEventListener('input', refreshLifeHint);" in html


def test_提示文案来自字段表不硬编码():
    html = _html()
    assert "人设摘要或「什么情况下主动说话」没填时" not in html
    assert "睡觉时段没填时" not in html


# ── claude 适配器：LifeSettingsError.code → HTTP ──────────────────────────────

@pytest.fixture
def adapter_env(tmp_path, monkeypatch):
    configs = tmp_path / "configs"
    configs.mkdir()
    channels = tmp_path / "channels"
    (channels / "b1").mkdir(parents=True)
    (configs / "b1.yml").write_text(
        "id: b1\nenabled: true\nchat_id: \"1\"\nbot_channel_path: %s\n" % (channels / "b1"),
        encoding="utf-8")
    monkeypatch.setenv("CLAUDEBOT_CONFIG_DIR", str(configs))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(configs))
    monkeypatch.setenv("HUB_CHANNELS_DIR", str(channels))
    monkeypatch.setattr(bots_registry, "configs_dir", lambda: configs)
    return ClaudeLocalAdapter(), configs


def _raises(fn, code, status):
    with pytest.raises(AdapterError) as ei:
        fn()
    assert (ei.value.code, ei.value.http_status) == (code, status), (ei.value.code, ei.value.http_status)
    return ei.value


def test_get_life_未知bot_404(adapter_env):
    ad, _configs = adapter_env
    _raises(lambda: ad.get_life("ghost"), "unknown_bot", 404)


def test_set_life_invalid_422且保留fields(adapter_env):
    ad, configs = adapter_env
    path = configs / "b1.yml"
    before = path.read_bytes()
    e = _raises(lambda: ad.set_life("b1", {"jiwen_delta_hints": "字" * 4001}), "invalid", 422)
    assert e.fields, "invalid 要带回逐键原因"
    assert path.read_bytes() == before
    _raises(lambda: ad.set_life("b1", {"jiwen_rates_override": {}}), "invalid", 422)


def test_set_life_internal_500(adapter_env):
    ad, configs = adapter_env
    path = configs / "b1.yml"
    path.write_text("id: b1\njiwen:\n  - 1\n", encoding="utf-8")
    before = path.read_bytes()
    _raises(lambda: ad.set_life("b1", {"jiwen_delta_hints": "新"}), "internal", 500)
    assert path.read_bytes() == before


def test_get_life_读回嵌套值(adapter_env):
    ad, configs = adapter_env
    (configs / "b1.yml").write_text("id: b1\njiwen:\n  delta_hints: 已有\n", encoding="utf-8")
    result = ad.get_life("b1")
    assert result["values"]["jiwen_delta_hints"] == "已有"
