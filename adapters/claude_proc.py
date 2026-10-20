# -*- coding: utf-8 -*-
"""claude 侧的进程层：只经 dispatcher 本机 HTTP 探活与拉起（T13）。

旧栈用终端会话与平台启动项判断 worker 在不在、拉起 worker；公开版 dispatcher 把这些能力收敛成
``POST /status`` 与 ``POST /ensure_worker`` 两个本机接口（INTERFACE §6.2）。本模块是适配器唯一
的进程接缝：

- ``status`` / ``polling``：``POST /status``，phase 为 ``starting`` / ``running`` 即视为在跑；
- ``ensure_worker``：``POST /ensure_worker``，dispatcher 决定是否拉起 worker；
- ``stop_worker``：按 ``/status`` 给出的 pid 结束 worker（dispatcher 本体不动），用于停用与重启；
- ``restart_worker``：先停 worker 再 ensure，dispatcher 与 Telegram 连接不断。

所有请求带 ``X-Api-Token``（读 ``<频道目录>/.api-token``）；令牌只出现在请求头里，
不进日志、异常与返回值。HTTP 错误按 INTERFACE §9 映射成 ``DispatcherError.status``：
``unauthorized`` / ``down`` / ``error``。``_request``、``_signal``、``_sleep`` 是测试注入点，
单测不连真实端口、不真杀进程、不真睡。
"""
import json
import logging
import os
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request

import bots_registry

_log = logging.getLogger(__name__)

HTTP_TIMEOUT = 2.0          # 秒：本机回环，正常应远小于它
POLL_EVERY = 0.2            # 秒：等状态翻转时的间隔
STOP_GRACE = 5.0            # 秒：SIGTERM 后等 worker 自己退出的上限
STOP_TIMEOUT = 15.0         # 秒：stop_worker 的总上限

_request = urllib.request.urlopen   # 注入点：单测替换成假 dispatcher
_sleep = time.sleep                 # 注入点：单测不真睡
_kill = os.kill                     # 注入点：单测不真杀
_popen = subprocess.Popen           # 注入点：Windows taskkill


class DispatcherError(Exception):
    """dispatcher 本机接口不可用或拒绝。``status`` ∈ down / unauthorized / error。"""

    def __init__(self, status: str, message: str, http_status: int = 0):
        if status not in ("down", "unauthorized", "error"):
            status = "error"
        super().__init__(message)
        self.status = status
        self.message = message
        self.http_status = http_status


def _port_value(port):
    if isinstance(port, bool):
        return None
    if isinstance(port, str) and port.isascii() and port.isdigit():
        port = int(port)
    return port if isinstance(port, int) and 0 < port < 65536 else None


def _bot_port(bot_id: str, port=None):
    value = _port_value(port)
    if value is not None:
        return value
    ports = bots_registry.ports()
    return _port_value(ports.get(bot_id))


def _channel_dir(bot_id: str):
    """口令文件所在目录：``HUB_CHANNELS_DIR``（测试/部署接缝）→ yml 的 bot_channel_path →
    ``~/.claude/channels/<bot>``。只读，不建目录。"""
    import yaml

    root = os.environ.get("HUB_CHANNELS_DIR")
    if root:
        return os.path.join(os.path.expanduser(root), bot_id)
    path = os.path.join(os.path.expanduser("~"), ".claude", "channels", bot_id)
    try:
        cfg_path = os.path.join(os.environ.get("HUB_CONFIGS_DIR") or
                                os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "configs"),
                                bot_id + ".yml")
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        raw = cfg.get("bot_channel_path") if isinstance(cfg, dict) else None
        if isinstance(raw, str) and raw:
            return os.path.expanduser(raw)
    except (OSError, UnicodeDecodeError, Exception):
        pass
    return path


def _request_json(bot_id: str, port, path: str, timeout: float = HTTP_TIMEOUT) -> dict:
    """POST 一个免 body 的管理端点。错误映射成 ``DispatcherError``；响应不是 JSON 对象也算 ``error``。"""
    base = _bot_port(bot_id, port)
    if base is None:
        raise DispatcherError("down", "这只 bot 没有可用的 dispatcher 端口")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    headers.update(bots_registry.dispatcher_auth_headers(bot_id, _channel_dir(bot_id)))
    body = b"{}"
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (base, path), data=body,
                                 headers=headers, method="POST")
    try:
        with _request(req, timeout=timeout) as resp:
            raw = resp.read(1 << 20)
    except urllib.error.HTTPError as e:
        code = getattr(e, "code", 0)
        if code in (401, 403):
            raise DispatcherError("unauthorized", "dispatcher 口令不对或没读到", code) from None
        raise DispatcherError("error", "dispatcher 返回 %s" % code, code) from None
    except (urllib.error.URLError, OSError, ValueError):
        raise DispatcherError("down", "dispatcher 没在运行") from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise DispatcherError("error", "dispatcher 的回应不是 JSON") from None
    if not isinstance(data, dict):
        raise DispatcherError("error", "dispatcher 的回应不是对象")
    return data


def status(bot_id: str, port=None, timeout: float = HTTP_TIMEOUT) -> dict:
    """``POST /status`` 的响应，并补一个 ``alive``：phase 为 starting/running，或响应明确说 alive。"""
    data = _request_json(bot_id, port, "/status", timeout=timeout)
    phase = data.get("phase")
    alive = data.get("alive")
    if not isinstance(alive, bool):
        if phase in ("starting", "running"):
            alive = True
        elif phase in ("stopped", "stopping"):
            alive = False
        else:
            alive = bool(data.get("pid"))
    return dict(data, alive=alive, phase=phase if isinstance(phase, str) else "stopped")


def ensure_worker(bot_id: str, port=None, timeout: float = HTTP_TIMEOUT) -> dict:
    """``POST /ensure_worker``：worker 不在时由 dispatcher 拉起。"""
    data = _request_json(bot_id, port, "/ensure_worker", timeout=timeout)
    if data.get("ok") is False:
        raise DispatcherError("error", "dispatcher 没能拉起 worker")
    return data


def polling(bot_id: str, port=None) -> bool:
    """这只 bot 的 worker 此刻在不在跑。dispatcher 不可达按“没在跑”处理（页面显示离线）。

    调用方要区分“不可达”与“停了”时直接用 ``status``；``polling`` 只回答是否在收消息。
    """
    if not isinstance(bot_id, str) or not bot_id:
        raise ValueError("bot_id 必须是非空字符串")
    try:
        return status(bot_id, port).get("alive") is True
    except DispatcherError:
        return False


def wait_polling(bot_id: str, port, want: bool, deadline: float) -> bool:
    """反复按 ``polling`` 判定，直到结果等于 ``want`` 或过了 ``deadline``（``time.monotonic()`` 时刻）。
    返回最后一次判定的结果；至少判定一次。"""
    while True:
        now = polling(bot_id, port)
        left = deadline - time.monotonic()
        if now is want or left <= 0:
            return now
        _sleep(min(POLL_EVERY, left))


def _signal_pid(pid: int, force: bool) -> None:
    """结束 dispatcher 拉起的 worker 子进程。POSIX 用 SIGTERM/SIGKILL；Windows 用 taskkill /T /F
    （只杀 claude 会留孤儿，继续写同一份 jsonl）。"""
    if os.name == "nt":   # pragma: no cover - Windows 条件测试
        argv = ["taskkill", "/PID", str(int(pid)), "/T", "/F"]
        _popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
               stdin=subprocess.DEVNULL, close_fds=True)
        return
    _kill(int(pid), signal.SIGKILL if force else signal.SIGTERM)


def stop_worker(bot_id: str, port=None, *, force: bool = False,
                timeout: float = STOP_TIMEOUT) -> dict:
    """把 worker 停下（dispatcher 与 Telegram 长轮询不动）。已经停了 → ``stopped``。

    只信 ``/status`` 给出的进程号：pid 不是正整数、或响应里的 ``bot`` 与请求的不一致时不发信号，
    免得误杀别的进程；此时按“停不下来”报 still_running。
    """
    data = status(bot_id, port)
    if data.get("alive") is not True:
        return {"ok": True, "stopped": True, "already": True}
    pid = data.get("pid")
    owner = data.get("bot")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1 or (owner is not None and owner != bot_id):
        raise DispatcherError("error", "dispatcher 没给出可确认的 worker 进程号")
    deadline = time.monotonic() + max(0.0, timeout)
    try:
        _signal_pid(pid, force)
    except ProcessLookupError:
        return {"ok": True, "stopped": True, "already": True}
    except OSError:
        raise DispatcherError("error", "没能结束 worker 进程") from None
    if not force:
        grace = time.monotonic() + min(STOP_GRACE, max(0.0, timeout))
        while time.monotonic() < grace:
            if not polling(bot_id, port):
                return {"ok": True, "stopped": True, "already": False}
            _sleep(POLL_EVERY)
        _signal_pid(pid, True)
    while time.monotonic() < deadline:
        if not polling(bot_id, port):
            return {"ok": True, "stopped": True, "already": False}
        _sleep(POLL_EVERY)
    raise DispatcherError("error", "worker 还在运行，没能停下")


def restart_worker(bot_id: str, port=None, *, force: bool = False) -> dict:
    """重启 worker：在跑就先停再 ensure；没在跑直接 ensure。返回 ``{"status": ok|failed, "message"}``。"""
    try:
        if polling(bot_id, port):
            stop_worker(bot_id, port, force=force)
        ensure_worker(bot_id, port)
    except DispatcherError as e:
        if e.status == "unauthorized":
            raise
        return {"status": "failed", "message": e.message}
    if wait_polling(bot_id, port, True, time.monotonic() + STOP_GRACE):
        return {"status": "ok", "message": "已重启"}
    return {"status": "failed", "message": "没起来"}


# 兼容旧调用点的名字（尚未移植的代码按它判活）。
def alive(bot_id: str, port=None) -> bool:
    return polling(bot_id, port)
