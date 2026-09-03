# -*- coding: utf-8 -*-
"""M4 纯函数层单测夹具。

与验收测试的区别：这里直接调函数，不起 Flask、不起桩、不发 HTTP、不解析域名
（SSRF 用例一律注入假解析器）。落盘只在 tmp_path 下，绝不碰真实 ~/.claude/settings.json。
"""
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture(autouse=True)
def _no_real_settings(monkeypatch, tmp_path):
    """任何用例都不许继承外部 env 的真实 settings 路径。"""
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(tmp_path / "settings.json"))


def payload(**over):
    """§3.3 的合法请求体（与验收 conftest.provider_payload 同形）。"""
    body = {"label": "DeepSeek", "kind": "openai",
            "base_url": "https://api.deepseek.com/v1", "api_key": "sk-test-0000000000",
            "upstream_model": "deepseek-v4-flash",
            "model_alias": "claude-sonnet-4-5-20250929"}
    body.update(over)
    return body


def no_dns(_host):
    """假解析器：域名一律解析不出 → 按"挡不掉就放行"处理，且保证单测不联网。"""
    return []


def dns_to(*addrs):
    return lambda _host: list(addrs)
