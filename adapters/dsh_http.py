# -*- coding: utf-8 -*-
"""dsh 底层适配器（占位，T14 填充）。

T12 只保证两件事：注册表能按名字拿到实例；``meta()`` 给出能力矩阵与思考强度枚举。
回环 17990、``/adapter/v1``、令牌文件、请求超时与错误映射在 T14 实现；未实现的方法按基类抛
``NotImplementedError``，不会静默成功，也不会在本模块导入任何 HTTP/Telegram 客户端。

占位能力值照 ``docs/unified-console-adapter.md`` 第 6 节。真实 dsh 的能力以对方
``GET /adapter/v1/meta`` 的返回为准，T14 会改为请求结果。
"""
from .base import BackendAdapter, DSH_CAPABILITIES, EFFORT_LEVELS


class DshHttpAdapter(BackendAdapter):
    backend = "dsh"

    def meta(self) -> dict:
        return {
            "backend": "dsh",
            "backend_version": None,
            "adapter": "placeholder",
            "capabilities": dict(DSH_CAPABILITIES),
            "effort_levels": list(EFFORT_LEVELS),
        }
