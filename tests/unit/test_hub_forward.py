# -*- coding: utf-8 -*-
"""hub 转发安全边界：目标必须回环、路径归一化后拒绝穿越。"""
import sys
from pathlib import Path

import pytest
from flask import Flask

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from moments import hub_forward  # noqa: E402


@pytest.mark.parametrize("raw,expect", [
    ("api/provider", "/api/provider"),
    ("/api/provider", "/api/provider"),
    ("a/./b", "/a/b"),
])
def test_safe_path_正常路径(raw, expect):
    assert hub_forward._safe_path(raw) == expect


@pytest.mark.parametrize("raw", [
    "", "/", "../x", "/hub/../x", "a/../../x", "//evil", "a//b",
    "%2e%2e/x", "a/%2e%2e/b", "a\\b", "a\nb", "a\x00b", "a\x7fb",
])
def test_safe_path_拒绝穿越与控制字符(raw):
    assert hub_forward._safe_path(raw) is None


def test_target_拒绝非回环(monkeypatch):
    monkeypatch.setenv("CONSOLE_DSH_ADAPTER_URL", "http://example.com:1234")
    with pytest.raises(ValueError):
        hub_forward._target()


def test_target_接受回环(monkeypatch):
    monkeypatch.setenv("CONSOLE_DSH_ADAPTER_URL", "http://127.0.0.1:9100/adapter")
    assert hub_forward._target() == ("127.0.0.1", 9100, "/adapter")
    monkeypatch.setenv("CONSOLE_DSH_ADAPTER_URL", "http://localhost:9100")
    assert hub_forward._target()[0] in ("127.0.0.1", "::1")


def test_路由_外部目标503(monkeypatch):
    monkeypatch.setenv("CONSOLE_DSH_ADAPTER_URL", "http://example.com:1234")
    app = Flask(__name__)
    app.register_blueprint(hub_forward.hub_forward_bp)
    r = app.test_client().get("/hub/api/provider")
    assert r.status_code == 503
    assert r.get_json() == {"error": "backend_unconfigured"}


def test_路由_非法路径404(monkeypatch):
    monkeypatch.setenv("CONSOLE_DSH_ADAPTER_URL", "http://127.0.0.1:9100")
    app = Flask(__name__)
    app.register_blueprint(hub_forward.hub_forward_bp)
    # 直接用 view 函数测非法 path（URL 里的 ../ 会被测试客户端/服务器先归一化）
    with app.test_request_context("/hub/x"):
        r = hub_forward.hub_proxy("../x")
        assert r.status_code == 404
