# -*- coding: utf-8 -*-
"""adapters.registry 单测：启用名单、延迟导入、未知 backend、不可用映射。

不起 8767、不连任何真实服务；假模块只注入 ``sys.modules``。
"""
from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from adapters import base, registry

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── 启用名单与显示名 ─────────────────────────────────────────────────────────

def test_默认启用名单(monkeypatch):
    monkeypatch.delenv("CONSOLE_BACKENDS", raising=False)
    assert registry.enabled_backends() == ["claude", "dsh"]


def test_启用名单去空白去重保序并保留未知名字(monkeypatch):
    monkeypatch.setenv("CONSOLE_BACKENDS", " dsh, claude , unknown, dsh ,, ")
    assert registry.enabled_backends() == ["dsh", "claude", "unknown"]


def test_启用名单空值回默认(monkeypatch):
    monkeypatch.setenv("CONSOLE_BACKENDS", "   ")
    assert registry.enabled_backends() == ["claude", "dsh"]


def test_label():
    assert registry.label("claude") == "Claude"
    assert registry.label("dsh") == "dsh"
    assert registry.label("unknown") == "unknown"


# ── 延迟导入 ─────────────────────────────────────────────────────────────────

def test_导入包时不加载具体适配器():
    code = (
        "import sys, adapters\n"
        "assert 'adapters.claude_local' not in sys.modules\n"
        "assert 'adapters.dsh_http' not in sys.modules\n"
        "a = adapters.registry.adapter('claude')\n"
        "assert 'adapters.claude_local' in sys.modules\n"
        "assert a.backend == 'claude'\n"
    )
    subprocess.run([sys.executable, "-c", code], cwd=str(REPO_ROOT), check=True,
                   env=dict(os.environ, PYTHONPATH=str(REPO_ROOT)))


def test_adapter返回实例():
    a = registry.adapter("claude")
    assert isinstance(a, base.BackendAdapter)
    assert a.backend == "claude"
    b = registry.adapter("dsh")
    assert isinstance(b, base.BackendAdapter)
    assert b.backend == "dsh"


# ── 未知 backend 与加载/构造失败 ─────────────────────────────────────────────

def test_未知backend映射not_installed():
    with pytest.raises(base.AdapterUnavailable) as ei:
        registry.adapter("telegram")
    assert ei.value.status == "not_installed"
    assert registry.unavailable_http(ei.value.status) == (404, "unknown_backend")


def test_模块加载失败映射error(monkeypatch):
    monkeypatch.setitem(registry._KNOWN, "broken",
                        ("Broken", "adapters.no_such_module_for_test", "NoSuchClass"))
    with pytest.raises(base.AdapterUnavailable) as ei:
        registry.adapter("broken")
    assert ei.value.status == "error"
    assert ei.value.message == "适配器加载失败"
    assert registry.unavailable_http(ei.value.status) == (502, "backend_down")


def _install_fake(monkeypatch, name, cls):
    module = types.ModuleType(name)
    setattr(module, cls.__name__, cls)
    monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setitem(registry._KNOWN, "fake", ("Fake", name, cls.__name__))


def test_构造失败映射error且日志不含异常原文(monkeypatch, caplog):
    class Boom:
        def __init__(self):
            raise RuntimeError("SECRET-config-value")

    _install_fake(monkeypatch, "adapters._fake_boom", Boom)
    with caplog.at_level("WARNING"):
        with pytest.raises(base.AdapterUnavailable) as ei:
            registry.adapter("fake")
    assert ei.value.status == "error"
    assert "SECRET-config-value" not in str(ei.value)
    assert "RuntimeError" in caplog.text
    assert "SECRET-config-value" not in caplog.text


def test_构造抛unavailable原样透传(monkeypatch):
    class Unconfigured:
        def __init__(self):
            raise base.AdapterUnavailable("unconfigured")

    _install_fake(monkeypatch, "adapters._fake_unconfigured", Unconfigured)
    with pytest.raises(base.AdapterUnavailable) as ei:
        registry.adapter("fake")
    assert ei.value.status == "unconfigured"
    assert registry.unavailable_http(ei.value.status) == (503, "backend_unconfigured")


def test_构造成功返回实例(monkeypatch):
    class Ok:
        def __init__(self):
            self.ready = True

    _install_fake(monkeypatch, "adapters._fake_ok", Ok)
    got = registry.adapter("fake")
    assert got.ready is True


# ── 不可用映射 ───────────────────────────────────────────────────────────────

def test_不可用映射与接口一致():
    assert registry.unavailable_http("not_installed") == (404, "unknown_backend")
    assert registry.unavailable_http("unconfigured") == (503, "backend_unconfigured")
    assert registry.unavailable_http("unauthorized") == (503, "backend_unconfigured")
    assert registry.unavailable_http("down") == (502, "backend_down")
    assert registry.unavailable_http("error") == (502, "backend_down")
    assert registry.unavailable_http("nonsense") == (502, "backend_down")
