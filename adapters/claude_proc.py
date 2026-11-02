# -*- coding: utf-8 -*-
"""claude 侧的进程层：只经 dispatcher 本机 HTTP 探活与拉起（T13）。

旧栈用终端会话与平台启动项判断 worker 在不在、拉起 worker；公开版 dispatcher 把这些能力收敛成
``POST /status`` 与 ``POST /ensure_worker`` 两个本机接口（INTERFACE §6.2）。本模块是适配器唯一
的进程接缝：

- ``status`` / ``polling``：``POST /status``，phase 为 ``starting`` / ``running`` 即视为在跑；
- ``ensure_worker``：``POST /ensure_worker``，dispatcher 决定是否拉起 worker；
- ``stop_worker``：按 ``/status`` 给出的 pid 结束 worker（dispatcher 本体不动），用于停用与重启；
- ``restart_worker``：先停 worker 再 ensure，dispatcher 与 Telegram 连接不断。
- ``dispatcher_down``：主程序没响应的判定（``/status`` 带回的 ``last_poll_ok`` 陈旧，或 ``/status`` 与
  ``GET /health`` 两次都没答上）；``run_restart_one`` / ``wait_fresh``：只重启这一只 bot 的主程序并等它恢复。
  开关文件 ``~/.claude-tgbot/dispatcher-check.off`` 存在时判活与整只重启都关。

所有请求带 ``X-Api-Token``（读 ``<频道目录>/.api-token``）；令牌只出现在请求头里，
不进日志、异常与返回值。HTTP 错误按 INTERFACE §9 映射成 ``DispatcherError.status``：
``unauthorized`` / ``down`` / ``error``。``_request``、``_signal``、``_sleep`` 是测试注入点，
单测不连真实端口、不真杀进程、不真睡。
"""
import http.client
import json
import logging
import math
import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request

import bots_registry
from .base import valid_bot_id

_log = logging.getLogger(__name__)

HTTP_TIMEOUT = 2.0          # 秒：本机回环，正常应远小于它
POLL_EVERY = 0.2            # 秒：等状态翻转时的间隔
STOP_GRACE = 5.0            # 秒：SIGTERM 后等 worker 自己退出的上限
STOP_TIMEOUT = 15.0         # 秒：stop_worker 的总上限

_request = urllib.request.urlopen   # 注入点：单测替换成假 dispatcher
_sleep = time.sleep                 # 注入点：单测不真睡
_kill = os.kill                     # 注入点：单测不真杀
_popen = subprocess.Popen           # 注入点：Windows taskkill
_popen_restart = subprocess.Popen   # 注入点：单只整只重启入口

HEALTH_TIMEOUT = 2.0        # 秒：GET /health 的超时
HEALTH_MAX_AGE = 300        # 秒：last_poll_ok 距今超过它算陈旧（长轮询周期的 10 倍）
HEALTH_MAX_BYTES = 4096     # /health 正文上限；超过说明端口上是别的程序
LINK_STALE_SEC = 120        # 秒：线路状态文件 checked_at 距今在这个范围内才算“线路不通”
SCRIPT_WAIT = 20.0          # 秒：等单只重启脚本退出的上限（不杀它）
FULL_RESTART_WAIT = 60.0    # 秒：整只重启从调用起到主程序恢复的总上限
FRESH_POLL_EVERY = 0.5      # 秒：等主程序恢复时的间隔
CHECK_OFF_FILE = os.path.join("~", ".claude-tgbot", "dispatcher-check.off")
_CHECKED_AT_RE = re.compile(r"(?:^|\s)checked_at=(\d{1,18})(?=\s|$)")
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


# ── 主程序判活与整只重启 ─────────────────────────────────────────────────────

def dispatcher_check_enabled() -> bool:
    """开关：``~/.claude-tgbot/dispatcher-check.off`` 存在（文件、目录、链接都算）→ 关。每次现查。"""
    return not os.path.lexists(os.path.expanduser(CHECK_OFF_FILE))


def _health_get(port: int):
    """GET /health 一次（免口令，不走代理）。→ ("down", None) 拒连/超时/重置/协议错；
    ("other", None) 非 200、正文过大或不是 JSON 对象；("ok", dict)。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=HEALTH_TIMEOUT)
    try:
        conn.request("GET", "/health", headers={"Accept": "application/json"})
        resp = conn.getresponse()
        code = resp.status
        raw = resp.read(HEALTH_MAX_BYTES + 1)
    except (OSError, http.client.HTTPException):
        return "down", None
    finally:
        conn.close()
    if code != 200 or len(raw) > HEALTH_MAX_BYTES:
        return "other", None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return "other", None
    return ("ok", data) if isinstance(data, dict) else ("other", None)


def _link_down(bot_id: str) -> bool:
    """dispatcher 自己写的线路状态文件报“连不上 Telegram”（checked_at 距今 ±120 秒内）。
    读不出、格式不对、过期 → False（按线路通处理）。"""
    if not valid_bot_id(bot_id):
        return False
    root = os.environ.get("LINK_STATE_DIR") or os.path.join(os.path.expanduser("~"), ".claude", "dispatcher",
                                                             ".link-state")
    try:
        with open(os.path.join(root, bot_id), "rb") as f:
            raw = f.read(256)
    except OSError:
        return False
    m = _CHECKED_AT_RE.search(raw.decode("utf-8", "replace"))
    return bool(m) and abs(time.time() - int(m.group(1))) <= LINK_STALE_SEC


def _poll_age_stale(bot_id: str, value) -> bool:
    """last_poll_ok 合法且距今超过 300 秒、线路状态文件又没报断线 → True。缺失或不合法 → False。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if not math.isfinite(value) or value <= 0:
        return False
    if time.time() - value <= HEALTH_MAX_AGE:
        return False
    return not _link_down(bot_id)


def dispatcher_down(bot_id: str, port, st) -> bool:
    """主程序没响应？``st`` 是 ``status()`` 成功时的字典，或 None（``status()`` 刚以 down 失败）。
    不抛：任何内部异常按正常处理（返回 False，与改前一致）。"""
    try:
        if not dispatcher_check_enabled():
            return False
        value = _port_value(port)
        if value is None:
            return False
        if st is not None:
            return _poll_age_stale(bot_id, st.get("last_poll_ok") if isinstance(st, dict) else None)
        kind, data = _health_get(value)
        if kind == "down":
            return True            # /status 与 /health 两次都没答上
        if kind != "ok":
            return False           # 端口上有别的程序在答，重启也解决不了
        return _poll_age_stale(bot_id, data.get("last_poll_ok"))
    except Exception:
        return False


def restart_one_argv(bot_id: str) -> list:
    """单只整只重启入口的命令行。测试接缝：``CLAUDEBOT_TEST=1`` 且 ``CLAUDEBOT_RESTART_ONE_CMD`` 是非空字符串
    数组的 JSON → 用它，末尾加 bot 编号。否则 POSIX 跑仓内 ``dispatcher/restart-one.sh``，Windows 跑
    ``windows\\restart-bots.ps1 -Only``。"""
    if os.environ.get("CLAUDEBOT_TEST") == "1":
        raw = os.environ.get("CLAUDEBOT_RESTART_ONE_CMD")
        if raw:
            try:
                cmd = json.loads(raw)
            except ValueError:
                cmd = None
            if isinstance(cmd, list) and cmd and all(isinstance(x, str) and x for x in cmd):
                return list(cmd) + [bot_id]
    if os.name == "nt":   # pragma: no cover - Windows 条件测试
        return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                os.path.join(_REPO, "windows", "restart-bots.ps1"), "-Only", bot_id]
    return ["bash", os.path.join(_REPO, "dispatcher", "restart-one.sh"), bot_id]


def run_restart_one(bot_id: str):
    """跑单只入口并等它退出（至多 SCRIPT_WAIT 秒）。→ 退出码；没退出或起不来 → None。
    脚本输出一律丢弃（可能含令牌）；超时不杀它（它可能正在停旧进程）。不抛。"""
    argv = restart_one_argv(bot_id)
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    try:
        if os.name == "nt":   # pragma: no cover - Windows 条件测试
            base = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            try:
                proc = _popen_restart(argv, creationflags=base | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kw)
            except OSError:   # 作业对象不允许脱离：退回不带该标志
                proc = _popen_restart(argv, creationflags=base, **kw)
        else:
            proc = _popen_restart(argv, start_new_session=True, **kw)
    except (OSError, ValueError):
        return None
    try:
        return proc.wait(timeout=SCRIPT_WAIT)
    except subprocess.TimeoutExpired:
        threading.Thread(target=proc.wait, daemon=True).start()   # 后台收尸，不留僵尸
        return None
    except Exception:
        return None


def wait_fresh(bot_id: str, port, deadline: float) -> bool:
    """每 0.5 秒问一次 ``/status``：成功且主程序不陈旧 → True；到 ``deadline``（monotonic）→ False。不抛。"""
    while True:
        try:
            st = status(bot_id, port)
            if not dispatcher_down(bot_id, port, st):
                return True
        except DispatcherError:
            pass
        except Exception:
            return False
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        _sleep(min(FRESH_POLL_EVERY, left))
