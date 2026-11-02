# -*- coding: utf-8 -*-
"""pub-memo-restart 验收公共工具（只依据 pub-memo-restart-接口.md 的对外约定）。

零副作用：HOME/USERPROFILE 一律指向 pytest 临时目录；假 dispatcher 只监听 127.0.0.1 随机端口；
整只重启一律经接口文件给的测试接缝（CLAUDEBOT_TEST=1 + CLAUDEBOT_RESTART_ONE_CMD）换成替身脚本，
绝不真的停起任何进程；替身起的假服务由 RestartStub.cleanup 清掉。
模块名特意叫 pmr_helpers，避免与其他验收目录的 helpers 撞名。
"""
import datetime
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "scripts"
INSTALLER = SCRIPTS / "install_compact_hook.py"
RECALL = SCRIPTS / "recall_private.py"
COMPACT_PRIVATE = SCRIPTS / "compact_private_context.py"
SAMPLE_PATH = REPO / "tests" / "fixtures" / "inbound_private.txt"

BOT = "mybot"
API_TOKEN = "pmr-test-api-token-" + "0" * 40
HEAD = "【私聊记忆："
REPLY_TOOL = "mcp__telegram-worker__reply"

skip_on_windows = pytest.mark.skipif(os.name == "nt", reason="POSIX 权限位 / bash 语义")
skip_if_root = pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                                  reason="root 不受目录权限限制，无法构造写失败")


# ════════════════════════════ C：找回钩子（子进程）════════════════════════════
def child_env(home, **extra):
    """最小子进程环境：不继承外部变量，HOME 指向临时目录，时区固定 UTC。"""
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "USERPROFILE": str(home),
           "TZ": "UTC", "PYTHONDONTWRITEBYTECODE": "1"}
    env.update(extra)
    return env


def run_py(script, home, args=(), stdin_text="", timeout=60):
    r = subprocess.run([sys.executable, str(script), *args], input=stdin_text.encode("utf-8"),
                       capture_output=True, env=child_env(home), timeout=timeout)
    return r.returncode, r.stdout.decode("utf-8"), r.stderr.decode("utf-8", "replace")


def hook_payload(source, cwd, transcript_path):
    return {"hook_event_name": "SessionStart", "session_id": "sess-1", "source": source,
            "cwd": str(cwd), "transcript_path": str(transcript_path)}


def run_recall(s, source="resume"):
    """以钩子方式跑 recall_private.py；返回 (退出码, stdout, stderr)。"""
    return run_py(RECALL, s["home"], stdin_text=json.dumps(hook_payload(source, s["bot"], s["tpath"]), ensure_ascii=False))


def arm(s):
    """经补装命令 --recall 布防（保证标记格式与产品一致）。返回 armed 路径。"""
    rc, out, err = run_py(INSTALLER, s["home"], args=(str(s["bot"]), "--recall"))
    assert rc == 0, "前置失败：install_compact_hook.py --recall 退出码 %s: %s %s" % (rc, out, err)
    return armed_path(s["bot"])


def armed_path(bot):
    return Path(bot) / ".claude" / "compact-recall.armed"


def inflight_path(bot):
    return Path(bot) / ".claude" / "compact-recall.inflight"


def read_inflight(bot):
    return json.loads(inflight_path(bot).read_text(encoding="utf-8"))


def used_files(bot):
    d = Path(bot) / ".claude"
    return sorted(n for n in os.listdir(d) if n.startswith("compact-recall.used-"))


def inflight_used_files(bot):
    d = Path(bot) / ".claude"
    return sorted(n for n in os.listdir(d) if n.startswith("compact-recall.inflight.used-"))


def file_state(path):
    """(字节, 修改时刻 ns)：用来断言'原样不动'。"""
    p = Path(path)
    return p.read_bytes(), p.stat().st_mtime_ns


def stderr_lines(err):
    return [l for l in err.splitlines() if l.strip()]


OK_LINE_PREFIX = "recall_private: ok n="


def iso_ms(epoch):
    d = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (d.microsecond // 1000)


def write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def append_row(path, row):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def user_row(uuid, parent, content, ts):
    row = {"type": "user", "uuid": uuid, "parentUuid": parent,
           "message": {"role": "user", "content": content}}
    if ts is not None:
        row["timestamp"] = ts
    return row


def reply_row(uuid, parent, chat_id, text, ts):
    return {"type": "assistant", "uuid": uuid, "parentUuid": parent, "timestamp": ts,
            "message": {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_" + uuid, "name": REPLY_TOOL,
                 "input": {"chat_id": str(chat_id), "text": text}}]}}


def api_error_row(uuid, parent, ts):
    return {"type": "assistant", "uuid": uuid, "parentUuid": parent, "timestamp": ts,
            "isApiErrorMessage": True,
            "message": {"role": "assistant", "model": "<synthetic>",
                        "content": [{"type": "text", "text": "API Error: 500"}]}}


def load_sample():
    assert SAMPLE_PATH.is_file(), "样例文件不存在: tests/fixtures/inbound_private.txt"
    return SAMPLE_PATH.read_text(encoding="utf-8")


def _split_sample(sample):
    t = sample.rstrip()
    assert t.endswith("</channel>")
    i = t.rindex("<channel")
    rest = t[i:]
    gt = rest.index(">")
    return t[:i], rest[:gt + 1]


def inbound_text(sample, body, ts_iso, message_id, extra_prefix=""):
    """以样例为底：换正文、时刻、message_id；extra_prefix 加在整段最前（如 [auto-photo] 注入行）。"""
    import re
    prefix, tag = _split_sample(sample)
    tag = re.sub(r' ts="[^"]*"', ' ts="%s"' % ts_iso, tag)
    tag = re.sub(r' message_id="[^"]*"', ' message_id="%s"' % message_id, tag)
    return extra_prefix + prefix + tag + "\n" + body + "\n</channel>"


def sample_chat_id(sample):
    import re
    return re.search(r'chat_id="([^"]*)"', _split_sample(sample)[1]).group(1)


def sample_base_time(sample):
    import re
    ts = re.search(r' ts="([^"]*)"', _split_sample(sample)[1]).group(1)
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def make_session(base_dir, bodies, reset_index=None):
    """造 HOME / bot 目录 / 会话文件。

    bodies: 每项是 str，或 (正文, 前置注入行) 元组；每条私聊后跟一条 bot 回复。
    reset_index: 该条用户行 parentUuid 为 null（守卫压缩后的'重置点'）；它之前的私聊就是'被丢的'。
    第 0 条本身也是 null 根行，但不是重置点。reset_index=None 表示会话里没有重置点（补出 0 条）。
    """
    base_dir = Path(base_dir)
    sample = load_sample()
    chat_id = sample_chat_id(sample)
    t0 = sample_base_time(sample)
    home = base_dir / "home"
    bot = base_dir / "channels" / "bot1"
    (bot / ".claude").mkdir(parents=True)
    (bot / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    proj = home / ".claude" / "projects" / "-bot1"
    proj.mkdir(parents=True)
    tpath = proj / "22222222-3333-4444-5555-666666666666.jsonl"
    rows, prev = [], None
    for i, item in enumerate(bodies):
        body, extra = (item, "") if isinstance(item, str) else item
        ts = (t0 + datetime.timedelta(minutes=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        parent = None if (i == 0 or i == reset_index) else prev
        rows.append(user_row("u%d" % i, parent, inbound_text(sample, body, ts, 200 + i, extra), ts))
        rows.append(reply_row("a%d" % i, "u%d" % i, chat_id, "回复%d" % i, ts))
        prev = "a%d" % i
    write_jsonl(tpath, rows)
    return {"home": home, "bot": bot, "tpath": tpath, "last_uuid": prev, "sample": sample}


EARLIER = ["早先私聊甲", "早先私聊乙"]
IN_CONTEXT = "上下文内私聊丙"


def standard_session(base_dir):
    """甲乙在重置行之前（应被补回），丙是重置行本身（已在上下文里）。"""
    return make_session(base_dir, EARLIER + [IN_CONTEXT], reset_index=2)


# ════════════════════════════ R：假 dispatcher 与适配器环境 ════════════════════════════
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


MISSING = object()


def status_body(last_poll_ok=MISSING, phase="running", pid=999999, bot=BOT):
    body = {"ok": True, "bot": bot, "phase": phase, "pid": pid, "queue_depth": 0}
    if last_poll_ok is not MISSING:
        body["last_poll_ok"] = last_poll_ok
    return body


class FakeDispatcher:
    """回环假 dispatcher。status/health 可传回调：返回 (状态码, JSON 对象|None|原始文本) 或 "hang"（接连接不回）。
    workers=True 时用真 sleep 子进程模拟 worker（/status 的 pid 指向它；被杀后 phase=stopped；/ensure_worker 重新拉起）。
    hits 记录 (方法, 路径)。"""

    HANG_MAX = 25

    def __init__(self, status=None, health=None, workers=False, token=API_TOKEN):
        self.hits = []
        self.status_fn = status
        self.health_fn = health
        self.token = token
        self.workers = workers
        self._procs = []
        self._stop = threading.Event()
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _serve(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                path = self.path.split("?", 1)[0]
                outer.hits.append((self.command, path))
                if path == "/status":
                    res = outer._status(self)
                elif path == "/health":
                    res = outer._health()
                elif path == "/ensure_worker":
                    res = outer._ensure()
                else:
                    res = (404, {"ok": False})
                if res == "hang":
                    outer._stop.wait(outer.HANG_MAX)
                    self.close_connection = True
                    return
                code, obj = res
                if obj is None:
                    raw, ctype = b"unauthorized", "text/plain"
                elif isinstance(obj, (bytes, str)):
                    raw, ctype = (obj if isinstance(obj, bytes) else obj.encode("utf-8")), "application/json"
                else:
                    raw, ctype = json.dumps(obj).encode("utf-8"), "application/json"
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = _serve
            do_POST = _serve

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        if workers:
            self._spawn_worker()

    # worker 模拟
    def _spawn_worker(self):
        p = subprocess.Popen(["sleep", "300"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        threading.Thread(target=p.wait, daemon=True).start()   # 收尸：被杀后不留僵尸，pid 探活才准
        self._procs.append(p)

    def _worker(self):
        return self._procs[-1] if self._procs else None

    def _status(self, req):
        if req.headers.get("X-Api-Token") != self.token:
            return 401, None
        if self.status_fn is not None:
            return self.status_fn()
        if self.workers:
            w = self._worker()
            alive = w is not None and w.poll() is None
            return 200, status_body(time.time(), "running" if alive else "stopped", w.pid if w else 0)
        return 200, status_body(time.time())

    def _health(self):
        if self.health_fn is not None:
            return self.health_fn()
        return 200, {"ok": True, "last_poll_ok": time.time()}

    def _ensure(self):
        if self.workers:
            w = self._worker()
            if w is None or w.poll() is not None:
                self._spawn_worker()
        return 200, {"ok": True, "running": True, "spawned": False}

    def paths(self):
        return [p for _, p in self.hits]

    def stop(self):
        self._stop.set()
        self.httpd.shutdown()
        self.httpd.server_close()
        for p in self._procs:
            if p.poll() is None:
                p.kill()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()


class HangServer:
    """接受连接但永远不回的 TCP 服务（模拟主程序卡死）。"""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.conns = []
        self._stopped = False
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while not self._stopped:
            try:
                c, _ = self.sock.accept()
                self.conns.append(c)
            except OSError:
                return

    def stop(self):
        self._stopped = True
        for c in self.conns:
            try:
                c.close()
            except OSError:
                pass
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()


def write_bot(env, bot_id=BOT, port=None, enabled=None, port_raw=None):
    """写一只 bot 的配置与 .api-token。返回频道目录。

    port=None 且 port_raw=None：配置里不写 dispatcher_port 这一行（此时适配器会按注册表派生成本机生产端口，
    用例不要用这种写法去测“端口未配”）。port_raw 是原样写进 yaml 的值文本，如 "null"、"abc"、"70000"。
    """
    channel = Path(env["channels"]) / bot_id
    channel.mkdir(parents=True, exist_ok=True)
    (channel / ".api-token").write_text(API_TOKEN, encoding="utf-8")
    lines = ["display_name: 测试 Bot", 'bot_channel_path: "%s"' % channel, "chat_id: 987654321"]
    if port_raw is not None:
        lines.append("dispatcher_port: %s" % port_raw)
    elif port is not None:
        lines.append("dispatcher_port: %d" % port)
    lines += ["namespace_uuid: 550e8400-e29b-41d4-a716-446655440001", "telegram_username: test_bot",
              "launcher: registry"]
    if enabled is not None:
        lines.append("enabled: %s" % ("true" if enabled else "false"))
    (Path(env["configs"]) / (bot_id + ".yml")).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return channel


def write_link_state(env, bot_id, text):
    d = Path(env["link_dir"])
    d.mkdir(parents=True, exist_ok=True)
    (d / bot_id).write_text(text, encoding="utf-8")


def switch_file(env):
    return Path(env["home"]) / ".claude-tgbot" / "dispatcher-check.off"


def set_switch(env):
    p = switch_file(env)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("", encoding="utf-8")


# ════════════════════════════ R：整只重启替身 ════════════════════════════
LEAK = "SENTINEL-TOKEN-do-not-leak-7f3a9c"

_SERVER_PY = r'''
import json, os, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
cfg = json.load(open(sys.argv[1]))
open(cfg["pidfile"], "a").write(str(os.getpid()) + "\n")
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _go(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n: self.rfile.read(n)
        path = self.path.split("?")[0]
        open(cfg["hits"], "a").write(self.command + " " + path + "\n")
        if path == "/status":
            body = {"ok": True, "bot": cfg["bot"], "phase": "running", "pid": os.getpid(), "queue_depth": 0,
                    "last_poll_ok": time.time()}
        elif path == "/health":
            body = {"ok": True, "last_poll_ok": time.time()}
        elif path == "/ensure_worker":
            body = {"ok": True, "running": True, "spawned": False}
        else:
            body = {"ok": False}
        raw = json.dumps(body).encode()
        self.send_response(200 if path in ("/status", "/health", "/ensure_worker") else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers(); self.wfile.write(raw)
    do_GET = _go
    do_POST = _go
threading.Timer(120, lambda: os._exit(0)).start()
ThreadingHTTPServer(("127.0.0.1", cfg["port"]), H).serve_forever()
'''

_STUB_PY = r'''
import json, os, socket, subprocess, sys, time
cfg = json.load(open(sys.argv[1]))
with open(cfg["log"], "a") as f:
    f.write(json.dumps(sys.argv) + "\n")
with open(cfg["pidfile"], "a") as f:
    f.write(str(os.getpid()) + "\n")
print(cfg["leak"]); print(cfg["leak"], file=sys.stderr); sys.stdout.flush(); sys.stderr.flush()
def start_server():
    subprocess.Popen([sys.executable, cfg["server"], sys.argv[1]], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", cfg["port"]), timeout=0.2).close()
            return
        except OSError:
            time.sleep(0.1)
mode = cfg["mode"]
if mode == "up":
    start_server()
elif mode == "exit1":
    sys.exit(1)
elif mode == "noserve":
    pass
elif mode == "hang":
    time.sleep(cfg["hang_s"])
elif mode == "block":
    for _ in range(400):
        if os.path.exists(cfg["release"]):
            break
        time.sleep(0.1)
    start_server()
'''


class RestartStub:
    """CLAUDEBOT_RESTART_ONE_CMD 的替身。mode:
    up=被调后在该端口起假 dispatcher(/status 带新鲜 last_poll_ok)后退出 0；exit1=退出 1；noserve=退出 0 但不起服务；
    hang=睡 hang_s 秒不退出；block=等 release 文件出现后才起服务并退出 0（用来造'整只重启进行中'）。"""

    def __init__(self, tmp_path, bot, port, mode, hang_s=25):
        self.dir = Path(tmp_path) / "restart-stub"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log = self.dir / "calls.log"
        self.hits_file = self.dir / "server-hits.log"
        self.pidfile = self.dir / "pids.txt"
        self.release = self.dir / "release"
        for f in (self.log, self.hits_file, self.pidfile):
            f.write_text("", encoding="utf-8")
        (self.dir / "stub.py").write_text(_STUB_PY, encoding="utf-8")
        (self.dir / "server.py").write_text(_SERVER_PY, encoding="utf-8")
        self.cfg_path = self.dir / "cfg.json"
        self.cfg_path.write_text(json.dumps({
            "mode": mode, "port": port, "bot": bot, "log": str(self.log), "hits": str(self.hits_file),
            "pidfile": str(self.pidfile), "release": str(self.release), "hang_s": hang_s,
            "server": str(self.dir / "server.py"), "leak": LEAK}), encoding="utf-8")

    def env(self):
        return {"CLAUDEBOT_TEST": "1",
                "CLAUDEBOT_RESTART_ONE_CMD": json.dumps([sys.executable, str(self.dir / "stub.py"), str(self.cfg_path)])}

    def apply(self, monkeypatch):
        for k, v in self.env().items():
            monkeypatch.setenv(k, v)

    def calls(self):
        return [json.loads(l) for l in self.log.read_text(encoding="utf-8").splitlines() if l.strip()]

    def server_hits(self):
        return [l.strip() for l in self.hits_file.read_text(encoding="utf-8").splitlines() if l.strip()]

    def pids(self):
        return [int(x) for x in self.pidfile.read_text(encoding="utf-8").split() if x.strip().isdigit()]

    def stub_pid(self):
        return self.pids()[0]

    def cleanup(self):
        self.release.write_text("", encoding="utf-8")   # 放行还在等的替身
        for pid in self.pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass


def pid_alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def perm_bits(path):
    return stat.S_IMODE(os.stat(path).st_mode)
