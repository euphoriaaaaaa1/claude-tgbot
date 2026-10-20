# -*- coding: utf-8 -*-
"""worker 拉起（T13 改写）：只调 dispatcher 的 ``POST /ensure_worker``，不再依赖终端会话。

旧版由面板进程执行会话探测 + ``spawn-worker.sh``；公开版 worker 的生命周期
完全归 dispatcher 的 WorkerManager（``/status`` / ``/ensure_worker``，INTERFACE §6.2）。
本模块保留原来的调用面，内部改成一次本机 HTTP 请求：

- 停用的 bot 一律不拉起（注册表 ``enabled:false`` 是唯一判定）；
- 端口从 ``bots_registry.ports()`` 现读，不缓存；
- dispatcher 不可达时返回 None 并只写一行 stderr（不含令牌、不含响应正文），
  由 dispatcher 自启服务负责把它拉回来。
"""
import os
import sys

import bots_registry
from adapters import claude_proc


def spawn_env() -> dict:
    """拉起进程用的环境：去掉管理台自己的运行开关。worker 由 dispatcher 拉起，本函数只为兼容旧调用点。"""
    return {k: v for k, v in os.environ.items()
            if not k.startswith("MOMENTS_WEB_") and k != "MOMENTS_DISABLE_CONVERGER"}


def ensure_worker_alive(bot_id: str, chat_id: str = None, bot_dir: str = None, *, ports=None):
    """确保这只 bot 的 worker 在跑。返回 dispatcher 的响应字典；停用或不可达时返回 None。

    ``chat_id`` / ``bot_dir`` 保留在签名里：旧调用方还在传，但 dispatcher 已从自己的配置
    取这些值，适配器不再需要它们。
    """
    if bot_id in bots_registry.disabled_ids_safe():
        return None
    table = ports if isinstance(ports, dict) else bots_registry.ports()
    port = table.get(bot_id)
    if port is None:
        sys.stderr.write("[ensure_worker] 未知 bot %s，跳过\n" % bot_id)
        return None
    try:
        return claude_proc.ensure_worker(bot_id, port)
    except claude_proc.DispatcherError as e:
        sys.stderr.write("[ensure_worker] %s 不可用：%s\n" % (bot_id, e.status))
        return None
