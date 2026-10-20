# -*- coding: utf-8 -*-
"""claude 底层适配器（占位，T13 填充）。

T12 只保证两件事：注册表能按名字拿到实例；``meta()`` 给出能力矩阵与思考强度枚举。
进程拉起、dispatcher ``/status``、新建、归档等行为在 T13 实现；未实现的方法按基类抛
``NotImplementedError``，不会静默成功。

占位能力值照 ``docs/unified-console-adapter.md`` 第 6 节；T13 按真实实现更新。
"""
from .base import BackendAdapter, CLAUDE_CAPABILITIES, EFFORT_LEVELS


class ClaudeLocalAdapter(BackendAdapter):
    backend = "claude"

    def meta(self) -> dict:
        return {
            "backend": "claude",
            "backend_version": None,
            "adapter": "placeholder",
            "capabilities": dict(CLAUDE_CAPABILITIES),
            "effort_levels": list(EFFORT_LEVELS),
        }
