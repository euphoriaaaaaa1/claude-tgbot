# -*- coding: utf-8 -*-
"""底层登记：启用名单由 ``CONSOLE_BACKENDS`` 决定，缺省 ``claude,dsh``，顺序即显示顺序。

适配器按名字**延迟**导入：``claude_local`` 实现会碰进程/平台模块，导入期出错不能拖垮
``adapters`` 包本身；``dsh_http`` 只用标准库。登记表里只有名字与模块路径，导入动作发生在
第一次 ``adapter()`` 调用时。

不可用到 HTTP 的映射（INTERFACE §9）：

============ ===== =====================
status       HTTP  code
============ ===== =====================
not_installed 404  unknown_backend
unconfigured  503  backend_unconfigured
unauthorized  503  backend_unconfigured
down          502  backend_down
error         502  backend_down
============ ===== =====================

能力不支持是单次操作的拒绝，不是整侧不可用：由 ``base.require_capability`` 抛
``AdapterError("unsupported", 501)``。
"""
import importlib
import logging
import os

from .base import AdapterUnavailable

_log = logging.getLogger(__name__)
DEFAULT_BACKENDS = "claude,dsh"

# 底层名 → (显示名, 适配器模块名, 类名)
_KNOWN = {
    "claude": ("Claude", "adapters.claude_local", "ClaudeLocalAdapter"),
    "dsh": ("dsh", "adapters.dsh_http", "DshHttpAdapter"),
}

# AdapterUnavailable.status → 写接口的 (HTTP, code)；未知 status 按 backend_down 兜底。
UNAVAILABLE_HTTP = {
    "down": (502, "backend_down"),
    "error": (502, "backend_down"),
    "unconfigured": (503, "backend_unconfigured"),
    "unauthorized": (503, "backend_unconfigured"),
    "not_installed": (404, "unknown_backend"),
}


def unavailable_http(status: str):
    """整侧不可用 → ``(http_status, code)``。未知状态按 502 backend_down 兜底。"""
    return UNAVAILABLE_HTTP.get(status, (502, "backend_down"))


def enabled_backends() -> list:
    """启用名单（去空白、去重、保序）。名字不在 ``_KNOWN`` 里的也保留：总览里按“未安装”如实显示。"""
    raw = os.environ.get("CONSOLE_BACKENDS")
    if raw is None or not raw.strip():
        raw = DEFAULT_BACKENDS
    out = []
    for name in raw.split(","):
        name = name.strip()
        if name and name not in out:
            out.append(name)
    return out


def label(backend: str) -> str:
    known = _KNOWN.get(backend)
    return known[0] if known else backend


def adapter(backend: str):
    """取该底层的适配器实例。未登记 → not_installed；模块加载失败 / 构造失败 → error
    （异常原文不外泄，日志只记类型名）。适配器自己抛的 ``AdapterUnavailable`` 原样透传。"""
    known = _KNOWN.get(backend)
    if known is None:
        raise AdapterUnavailable("not_installed")
    _label, module_name, class_name = known
    try:
        module = importlib.import_module(module_name)
        cls = getattr(module, class_name)
    except Exception:
        raise AdapterUnavailable("error", "适配器加载失败")
    try:
        return cls()
    except AdapterUnavailable:
        raise
    except Exception as e:
        # 构造失败（如 dsh 基址配置不合法）：总览标“出错了”、写接口回 502 backend_down。
        # 日志只记异常类型，不记原文——原文可能带配置值。
        _log.warning("console 适配器 %s 构造失败：%s", backend, type(e).__name__)
        raise AdapterUnavailable("error")
