# -*- coding: utf-8 -*-
"""dispatcher 交互层（INTERFACE §6.0-§6.2 / PLAN D8 / [RA] 建议3）。

门户与 bot dispatcher 之间**唯一**的出口：bot 名单、并发探活、重启命令构造与执行。
与 ``cliproxy_client`` 对称 —— 路由层不自己发请求、不自己拼平台分支。

**依赖方向只许 web.py → 本模块**（[RA] M1）：``BOT_PORTS`` 落在这里，
web.py 反向 import 它；反过来会因为 web.py 要 register_blueprint 而循环 import。

两个测试注入接缝（§6.0，生产零行为变化，README 不写）：
``HUB_RESTART_CMD`` 只替换"执行哪个命令串"，``HUB_BOTS_FILE`` 只替换"名单从哪来"，
其余（202 受理 / job 状态机 / 互斥 / 超时 / 脱敏 / 只留 5 条）一律不变。
两者都只在进程 env 里生效，攻击者无法经 HTTP 设置，不构成新增攻击面。
"""
import json
import os
import shlex
import signal
import subprocess
import threading
import time
import urllib.request
import uuid
from collections import OrderedDict, namedtuple
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

from moments import redact

REPO_ROOT = Path(__file__).resolve().parents[1]

# bot 端口映射（加 bot 就在这里加一行）。由 moments/web.py 的 _BOT_PORTS 迁来 ——
# 那边改成 import 本表后即可删掉它自己那份，别再各写一张。
BOT_PORTS = {"chenlulu": 17801}

PROBE_TIMEOUT = 1.5       # §6.1 单次探活超时
PROBE_BUDGET = 2.0        # §6.1 整体封顶：慢 bot 不许拖住门户
PROBE_WORKERS = 8         # 并发封顶：bot 再多也不炸线程
RESTART_TIMEOUT = 180.0   # §6.2 超时后强杀进程树
TAIL_MAX = 2000           # §6.2 tail 取尾 2000 字
JOBS_KEPT = 5             # §6.2 内存只留最近 5 条

# 门户只跟 127.0.0.1 说话，urllib 却默认吃 http_proxy —— 机器上挂着透明代理时
# 本机请求会被送出去，把在线 bot 误判成 offline（cliproxy_client 同款坑）。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

Accepted = namedtuple("Accepted", "job_id started_at busy")


# ---------------------------------------------------------------- 名单与端口

def _entry(bot_id, display_name):
    """规范化一条名单项；id 缺失/不是字符串一律抛（路由层转 500 config_unreadable）。"""
    if not isinstance(bot_id, str) or not bot_id.strip():
        raise ValueError("bot 条目缺 id")
    bot_id = bot_id.strip()
    ok = isinstance(display_name, str) and display_name.strip()
    return {"id": bot_id, "display_name": display_name.strip() if ok else bot_id}


def list_bots():
    """bot 名单 ``[{"id", "display_name"}]``。

    读不出来一律**抛异常**，由路由层统一转 ``500 config_unreadable``（§6.0 三态表第一行）——
    异常消息不出站，出站的只有类名，所以这里的文案可以写路径之外的任何提示。
    """
    path = os.environ.get("HUB_BOTS_FILE")
    if not path:
        import config_loader          # 延迟 import：名单是请求期才要的，别拖慢模块导入
        return [_entry(c.get("_bot_id"), c.get("display_name"))
                for c in config_loader.list_enabled_bots()]
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    if not isinstance(raw, list):
        raise TypeError("bots 名单顶层不是数组")
    out = []
    for e in raw:
        if not isinstance(e, dict):
            raise TypeError("bots 名单元素不是对象")
        out.append(_entry(e.get("id"), e.get("display_name")))
    return out


def bot_port(bot_id):
    """端口优先级：``DISPATCHER_PORT_<BOT大写>`` → ``BOT_PORTS``；都查不到回 None（unknown 态）。

    口径与 scripts/self_initiate.py 一致。非法值（空串/不是数字/越界）当查不到处理，
    宁可显示 unknown 也不要拿垃圾端口去连一个不相干的服务。
    """
    raw = os.environ.get("DISPATCHER_PORT_%s" % bot_id.upper(), BOT_PORTS.get(bot_id))
    try:
        port = int(raw)
    except (TypeError, ValueError):
        return None
    return port if 0 < port < 65536 else None


# ---------------------------------------------------------------- 探活（§6.1）

def _scalar(v, maxlen=80):
    """只让标量出站：dispatcher 将来往 /status 里塞对象也不会整包漏出去（S8）。"""
    if v is None or isinstance(v, (bool, int, float)):
        return v
    if isinstance(v, str):
        return v[:maxlen]
    return None


def _probe_one(port):
    """``POST /status``（§6.1：dispatcher 对非 POST 一律 405）。返回 dict，离线回 None。"""
    req = urllib.request.Request("http://127.0.0.1:%d/status" % port, data=b"{}",
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with _OPENER.open(req, timeout=PROBE_TIMEOUT) as r:
            body = json.loads(r.read(64 * 1024).decode("utf-8", "replace"))
    except Exception:      # 拒连/超时/非 2xx/垃圾响应 —— §6.1 表里一律 offline
        return None
    return body if isinstance(body, dict) else {}


def _settled(fut):
    """已完成且没抛异常才取值；封顶时间到还在跑的按 offline 处理。"""
    if not fut.done() or fut.cancelled() or fut.exception() is not None:
        return None
    return fut.result()


def _row(entry, port, status):
    """§6.1 的一行。offline/unknown 时后四个字段一律 null；在线也只投影这 5 个字段
    （dispatcher 还会回 session_uuid 之类，不该出站）。"""
    if port is None:
        state = "unknown"
    elif status is None:
        state = "offline"
    else:
        state = "online" if status.get("phase") == "ready" else "starting"
    row = {"id": entry["id"], "display_name": entry["display_name"], "port": port,
           "state": state, "phase": None, "pid": None,
           "queue_depth": None, "in_flight": None}
    if status is not None and port is not None:
        for k in ("phase", "pid", "queue_depth", "in_flight"):
            row[k] = _scalar(status.get(k))
    return row


def probe_bots(entries):
    """并发探活，整体封顶 ``PROBE_BUDGET`` 秒（[R4D] I7：串行时 N 个离线 bot 要等 N×1.5 秒）。

    自己吞掉全部探活异常 —— 抛出去会被路由层误判成 config_unreadable。
    """
    rows, targets = {}, []
    for e in entries:
        port = bot_port(e["id"])
        if port is None:
            rows[e["id"]] = _row(e, None, None)   # unknown：没端口可打，不发探活、不占预算
        else:
            targets.append((e, port))
    if targets:
        ex = ThreadPoolExecutor(max_workers=min(PROBE_WORKERS, len(targets)))
        try:
            pairs = [(e, p, ex.submit(_probe_one, p)) for e, p in targets]
            wait([f for _, _, f in pairs], timeout=PROBE_BUDGET)
            for e, port, fut in pairs:
                rows[e["id"]] = _row(e, port, _settled(fut))
        finally:
            # 封顶到了就走人：join 慢线程等于没封顶（残留线程最多再挂 PROBE_TIMEOUT 秒）
            ex.shutdown(wait=False, cancel_futures=True)
    return [rows[e["id"]] for e in entries]


# ---------------------------------------------------------------- 重启（§6.2）

def restart_plan():
    """``(argv, restart_available)`` —— 命令构造与可用性判定的**唯一**出处（[RA] 建议3）。

    §6.0：设了 ``HUB_RESTART_CMD`` 就恒可用（不再 stat 脚本），
    所以 ``409 restart_script_missing`` 只在未设该变量且脚本缺失时出现。
    """
    override = os.environ.get("HUB_RESTART_CMD")
    if override:
        return shlex.split(override), True
    if os.name == "nt":
        script = REPO_ROOT / "windows" / "restart-bots.ps1"
        argv = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
    else:
        script = REPO_ROOT / "restart-bots.sh"
        argv = ["bash", str(script)]
    # 只判"存在且可读"：install.sh 是 `cp restart-bots.example.sh` 拷出来的（不带 +x），
    # 而我们走 `bash <脚本>`，再要求 X_OK 会把装好的机器误报成脚本缺失。
    return argv, script.is_file() and os.access(script, os.R_OK)


def _decode(raw):
    return (raw or b"").decode("utf-8", "replace")


def _kill_tree(proc):
    """超时后连子进程一起杀：重启脚本会 spawn 一堆孙子，只杀那层 shell 等于没杀。"""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        proc.kill()


def _run_command(argv, timeout):
    """跑一次重启命令，返回 ``(state, exit_code, 合并输出)``。这里不脱敏，交调用方统一处理。

    - stdout/stderr 合并采集：脚本的报错在 stderr，只收 stdout 等于什么都看不到。
    - ``stdin=DEVNULL``：脚本万一读 stdin 会永远挂着，把互斥锁一起卡死。
    - ``start_new_session``：独立进程组，超时才有整树可杀。
    """
    if not argv:
        return "failed", None, "重启命令为空"
    try:
        proc = subprocess.Popen(argv, cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                start_new_session=(os.name != "nt"))
    except OSError as e:
        # 不回显命令串：它含路径与参数，属配置值（§7 末行同理）
        return "failed", None, "重启命令起不来：%s" % type(e).__name__
    try:
        out, _ = proc.communicate(timeout=timeout)
        return ("done" if proc.returncode == 0 else "failed"), proc.returncode, _decode(out)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, _ = proc.communicate(timeout=10)
        except Exception:
            out = b""
        return "timeout", None, _decode(out)


class RestartJobs:
    """重启任务表 + 互斥（§6.2）。

    每个 Flask app 一份（挂 ``app.extensions``）：生产里一个进程一个 app，
    等价于 §6.2 要的"进程内互斥"；测试反复重建 app 时又互不串味。
    """

    def __init__(self, keep=JOBS_KEPT):
        self._lock = threading.Lock()
        self._jobs = OrderedDict()
        self._running = None
        self._keep = keep

    def start(self, argv, timeout=RESTART_TIMEOUT):
        """受理一次重启。

        空闲 → ``Accepted(job_id, started_at, None)``；已有任务在跑 → ``Accepted(None, None, 那个 job_id)``。
        判定与登记在同一把锁里完成，两个请求同时按下也只会放行一个。
        """
        with self._lock:
            if self._running:
                return Accepted(None, None, self._running)
            job_id = uuid.uuid4().hex[:8]
            started_at = int(time.time())
            self._jobs[job_id] = {"state": "running", "exit_code": None, "took_ms": 0,
                                  "tail": "", "started_at": started_at,
                                  "t0": time.monotonic()}
            while len(self._jobs) > self._keep:
                self._jobs.popitem(last=False)      # 只留最近 keep 条，最新的那条永远在
            self._running = job_id
        threading.Thread(target=self._run, args=(job_id, argv, timeout),
                         daemon=True, name="hub-restart").start()
        return Accepted(job_id, started_at, None)

    def _run(self, job_id, argv, timeout):
        try:
            state, code, out = _run_command(argv, timeout)
            # 先整段脱敏再切尾：反过来会把 token 拦腰截成正则匹配不到的碎片（§6.2）
            tail = redact.scrub_text(out)[-TAIL_MAX:]
        except Exception as e:      # 线程里漏出异常 = 互斥标记永远清不掉，后面全 409
            state, code, tail = "failed", None, type(e).__name__
        with self._lock:
            job = self._jobs.get(job_id)            # 可能已被挤出表，那就只清互斥
            if job is not None:
                job.update(state=state, exit_code=code, tail=tail,
                           took_ms=int((time.monotonic() - job["t0"]) * 1000))
            if self._running == job_id:
                self._running = None

    def get(self, job_id):
        """§6.2 的四态投影；未知 job 回 None（路由层转 404）。不回显命令串与 started_at 之外的内部字段。"""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            took = (int((time.monotonic() - job["t0"]) * 1000)
                    if job["state"] == "running" else job["took_ms"])
            return {"state": job["state"], "exit_code": job["exit_code"],
                    "took_ms": took, "tail": job["tail"]}
