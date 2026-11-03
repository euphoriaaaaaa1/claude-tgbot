# -*- coding: utf-8 -*-
"""pub-memo-restart · 锁后修订 1/2/3：POSIX 单只入口 dispatcher/restart-one.sh 的对外行为。

只通过命令行调用（bash dispatcher/restart-one.sh <bot>），环境变量全指向临时目录；PATH 与 CLAUDEBOT_BUN
都指向替身 bun（只记录被调，睡 20 秒后自己退出），不可能起真 dispatcher。
「别的 bot 的 dispatcher」「占端口的进程」都是本用例自己起的长寿命子进程，结束时一律杀掉。
存活判断用 Popen.poll()，并用后台线程及时回收子进程：僵尸进程 os.kill(pid, 0) 仍成功，会让脚本误以为停不掉。
Windows 一侧（restart-bots.ps1 / stop-bots.ps1）本机跑不了，不在此写用例。
"""
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from pmr_fixtures import PROD_PORTS, no_prod_port_connect  # noqa: F401  autouse 护栏（不用 conftest.py，见 pmr_fixtures 文件头）
from pmr_helpers import REPO

SCRIPT = REPO / "dispatcher" / "restart-one.sh"
DISPATCHER_TS = REPO / "dispatcher" / "dispatcher.ts"
NS_ALPHA = "11111111-1111-4111-8111-111111111111"
NS_BETA = "22222222-2222-4222-8222-222222222222"
TOKEN_SENTINEL = "123456789:AA-pmr-fake-token-for-test-only"

# 替身进程：FAKE_LISTEN=1 时在 127.0.0.1 随机端口监听；就绪后把端口写进 FAKE_READY，再睡 120 秒
_FAKE_PROC_PY = """#!/usr/bin/env python3
import os, socket, time
port = 0
if os.environ.get("FAKE_LISTEN") == "1":
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    port = s.getsockname()[1]
with open(os.environ["FAKE_READY"], "w") as f:
    f.write(str(port))
time.sleep(120)
"""

# 替身 bun：记录被调（含自己的 pid），睡 20 秒；不 exec，进程命令行保留 .../bun .../dispatcher.ts
_FAKE_BUN_SH = """#!/bin/sh
{
  printf 'BOT_NAME=%s\\n' "$BOT_NAME"
  printf 'DISPATCHER_PORT=%s\\n' "$DISPATCHER_PORT"
  printf 'ARGS=%s\\n' "$*"
  printf 'PID=%s\\n' "$$"
} >> "$FAKE_BUN_LOG"
sleep 20
"""


class Sandbox:
    def __init__(self, root: Path):
        self.root = root
        self.home = root / "home"
        self.configs = root / "configs"
        self.channels = root / "channels"
        self.run_dir = root / "run"
        self.bun_log = root / "bun.log"
        self.stub_bin = root / "stub-bin"
        self.fake_dir = root / "fake-proc"
        for d in (self.home, self.configs, self.channels, self.run_dir, self.stub_bin, self.fake_dir):
            d.mkdir(parents=True, exist_ok=True)
        bun = self.stub_bin / "bun"
        bun.write_text(_FAKE_BUN_SH, encoding="utf-8")
        bun.chmod(0o755)
        self.fake_bun = bun
        # 「像 dispatcher 的进程」：可执行文件叫 bun，命令行带本仓库的 dispatcher.ts 路径
        self.lookalike_exe = self.fake_dir / "bun"
        self.lookalike_exe.write_text(_FAKE_PROC_PY, encoding="utf-8")
        self.lookalike_exe.chmod(0o755)
        self.plain_script = self.fake_dir / "plain.py"
        self.plain_script.write_text(_FAKE_PROC_PY, encoding="utf-8")
        self.procs = []
        self._seq = 0

    # ---------- 环境 ----------
    def env(self):
        env = {
            "PATH": str(self.stub_bin) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
            "TZ": "UTC",
            "no_proxy": "*",
            "NO_PROXY": "*",
            "CLAUDEBOT_CONFIG_DIR": str(self.configs),
            "DIRECTOR_DISABLED_DIR": str(self.root / "disabled"),
            "CLAUDEBOT_CHANNELS_ROOT": str(self.channels),
            "CLAUDEBOT_RUN_DIR": str(self.run_dir),
            "CLAUDEBOT_LOG_DIR": str(self.root / "logs"),
            "CLAUDEBOT_PYTHON": sys.executable,
            "CLAUDEBOT_BUN": str(self.fake_bun),
            "FAKE_BUN_LOG": str(self.bun_log),
        }
        return env

    # ---------- 注册 bot ----------
    def register(self, bot, port, enabled=True, namespace=NS_ALPHA):
        lines = ["id: %s" % bot, "display_name: %s" % bot, "dispatcher_port: %d" % port,
                 "namespace_uuid: %s" % namespace]
        if not enabled:
            lines.append("enabled: false")
        (self.configs / ("%s.yml" % bot)).write_text("\n".join(lines) + "\n", encoding="utf-8")
        chan = self.channels / bot
        chan.mkdir(parents=True, exist_ok=True)
        (chan / ".env").write_text("TELEGRAM_BOT_TOKEN=%s\n" % TOKEN_SENTINEL, encoding="utf-8")

    def write_pid_file(self, bot, pid, directory=None):
        d = directory or self.run_dir
        (d / ("%s.pid" % bot)).write_text("%d\n" % pid, encoding="utf-8")

    # ---------- 替身进程 ----------
    def _spawn(self, argv, extra_env):
        self._seq += 1
        ready = self.fake_dir / ("ready-%d" % self._seq)
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "FAKE_READY": str(ready)}
        env.update(extra_env)
        proc = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        self.procs.append(proc)
        # 后台回收：被脚本 TERM 掉的子进程不能留成僵尸，否则脚本里的 kill -0 会一直认为它还活着
        threading.Thread(target=proc.wait, daemon=True).start()
        end = time.time() + 10
        while time.time() < end:
            if ready.exists() and ready.read_text().strip():
                break
            assert proc.poll() is None, "替身进程自己先退出了"
            time.sleep(0.02)
        else:
            raise AssertionError("替身进程 10 秒内没就绪")
        port = int(ready.read_text().strip())
        assert port not in PROD_PORTS, "替身拿到了生产端口 %d" % port
        return proc, port

    def spawn_lookalike(self, listen=False, bot_name="beta", bot_flag=None):
        """命令行 = <fake>/bun <仓库>/dispatcher/dispatcher.ts 的长寿命进程；listen=True 时监听随机本机端口。
        bot_flag 给了（如 "alpha"）就在命令行末尾追加 --bot=<bot_flag>（本 bot 标记）；默认不带，行为不变。"""
        argv = [str(self.lookalike_exe), str(DISPATCHER_TS)]
        if bot_flag is not None:
            argv.append("--bot=%s" % bot_flag)
        return self._spawn(argv, {"FAKE_LISTEN": "1" if listen else "0", "BOT_NAME": bot_name})

    def spawn_plain_listener(self):
        """没有 pid 文件、命令行也不像 dispatcher 的占端口进程。"""
        return self._spawn([sys.executable, str(self.plain_script)], {"FAKE_LISTEN": "1"})

    def spawn_plain_sleeper(self):
        """命令行不像 dispatcher 的普通进程（不监听）。"""
        return self._spawn([sys.executable, str(self.plain_script)], {"FAKE_LISTEN": "0"})

    def dead_pid(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        return p.pid

    # ---------- 运行与观察 ----------
    def run(self, *args):
        try:
            r = subprocess.run(["bash", str(SCRIPT), *args], env=self.env(), cwd=str(self.root),
                               capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired as exc:
            raise AssertionError("restart-one.sh 60 秒没退出: %s" % exc)
        return r.returncode, r.stdout + r.stderr

    def bun_calls(self):
        if not self.bun_log.exists():
            return []
        return [ln for ln in self.bun_log.read_text(encoding="utf-8").splitlines() if ln.startswith("PID=")]

    def cleanup(self):
        for p in self.procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    pass
            try:
                p.wait(timeout=5)
            except Exception:
                pass
        # 脚本若真拉起了替身 bun，按它自己记在日志里的 pid 清掉（连同它的 sleep 子进程，不留孤儿）
        if self.bun_log.exists():
            for ln in self.bun_log.read_text(encoding="utf-8").splitlines():
                if ln.startswith("PID=") and ln[4:].isdigit():
                    kids = subprocess.run(["pgrep", "-P", ln[4:]], capture_output=True, text=True).stdout.split()
                    for target in kids + [ln[4:]]:
                        try:
                            os.kill(int(target), signal.SIGKILL)
                        except OSError:
                            pass


@pytest.fixture
def sb(tmp_path):
    box = Sandbox(tmp_path)
    yield box
    box.cleanup()


def alive(proc):
    return proc.poll() is None


def port_open(port):
    import socket
    assert port not in PROD_PORTS
    s = socket.socket()
    s.settimeout(1)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def unused_port():
    import socket
    for _ in range(50):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        if port not in PROD_PORTS:
            return port
    raise AssertionError("找不到非生产空闲端口")


# ───────────────── 修订2-a：未注册 / 已停用 → 退出 2，不停任何进程 ─────────────────
def test_未注册的bot_退出2_不停pid文件里的进程_不起bun(sb):
    other, other_port = sb.spawn_lookalike(listen=True)
    sb.register("alpha", other_port)               # 注册表非空，但没有 ghost
    victim, _ = sb.spawn_lookalike()
    sb.write_pid_file("ghost", victim.pid)
    rc, out = sb.run("ghost")
    assert rc == 2, out
    assert alive(victim), "未注册的 bot 却停了 pid 文件里的进程"
    assert alive(other)
    assert sb.bun_calls() == [], "未注册的 bot 却尝试起了 bun"


def test_已停用的唯一一只bot_退出2_不停pid文件里的进程_不起bun(sb):
    """注册表因此为空：条文写「不在注册表或已停用 → 2」，空注册表不应变成别的退出码。"""
    sb.register("alpha", unused_port(), enabled=False)
    victim, _ = sb.spawn_lookalike()
    sb.write_pid_file("alpha", victim.pid)
    rc, out = sb.run("alpha")
    assert rc == 2, out
    assert alive(victim), "已停用的 bot 却停了 pid 文件里的进程"
    assert sb.bun_calls() == []


def test_已停用的bot_退出2_不停pid文件里的进程_不起bun(sb):
    sb.register("beta", unused_port(), namespace=NS_BETA)       # 注册表里还有别的启用 bot
    port = unused_port()
    sb.register("alpha", port, enabled=False)
    victim, _ = sb.spawn_lookalike()
    sb.write_pid_file("alpha", victim.pid)
    rc, out = sb.run("alpha")
    assert rc == 2, out
    assert alive(victim), "已停用的 bot 却停了 pid 文件里的进程"
    assert sb.bun_calls() == [], "已停用的 bot 却尝试起了 bun"


# ───────────────── 修订2-b：非法编号 → 退出 2 ─────────────────
@pytest.mark.parametrize("bad", ["a/b", "a b", "..", "../alpha", "a;b", "a\tb", "a" * 65])
def test_非法编号_退出2_文案是非法编号_不停进程_不起bun(sb, bad):
    sb.register("alpha", unused_port())
    victim, _ = sb.spawn_lookalike()
    sb.write_pid_file("alpha", victim.pid)                           # run/alpha.pid
    sb.write_pid_file("alpha", victim.pid, directory=sb.root)        # 给 "../alpha" 的路径穿越留的陷阱：<root>/alpha.pid
    rc, out = sb.run(bad)
    assert rc == 2, out
    assert "非法" in out, "非法编号的报错文案里应有「非法编号」，实际: %r" % out
    assert alive(victim), "非法编号却停了进程"
    assert sb.bun_calls() == []


def test_没带参数_退出2_不起bun(sb):
    sb.register("alpha", unused_port())
    rc, out = sb.run()
    assert rc == 2, out
    assert sb.bun_calls() == []


def test_空字符串编号_退出2_不起bun(sb):
    sb.register("alpha", unused_port())
    rc, out = sb.run("")
    assert rc == 2, out
    assert sb.bun_calls() == []


# ───────────────── 修订3：合法编号（含点号、下划线、连字符）入口接受，走到查注册表 ─────────────────
@pytest.mark.parametrize("good", ["my.bot", "my_bot", "my-bot", "bot2", "a.b-c_d", "a..b"])
def test_合法编号_未注册_退出2_但不是非法编号文案(sb, good):
    sb.register("alpha", unused_port())
    rc, out = sb.run(good)
    assert rc == 2, out
    assert "非法" not in out, "合法编号 %r 被当成非法编号拒绝了: %r" % (good, out)
    assert sb.bun_calls() == []


# ───────────────── 修订1/2-c：pid 文件指向别的 bot 的进程，不得停它 ─────────────────
def test_pid文件指向别的bot的dispatcher_该进程仍存活(sb):
    beta_proc, beta_port = sb.spawn_lookalike(listen=True, bot_name="beta")
    sb.register("beta", beta_port, namespace=NS_BETA)
    alpha_port = unused_port()
    sb.register("alpha", alpha_port)
    sb.write_pid_file("alpha", beta_proc.pid)        # alpha 的 pid 文件被别的 bot 的进程复用
    rc, out = sb.run("alpha")
    assert alive(beta_proc), "停了别的 bot 的 dispatcher（退出码 %s）: %s" % (rc, out)
    assert port_open(beta_port), "别的 bot 的端口不再可连"
    assert rc in (0, 3), "按「这只没在跑」处理后只能是起来了(0)或宁可不动(3)，实际 %s: %s" % (rc, out)


def test_pid文件指向别的bot的dispatcher_且本bot端口被无关进程占着_退出3_两个进程都存活_不起bun(sb):
    beta_proc, beta_port = sb.spawn_lookalike(listen=True, bot_name="beta")
    sb.register("beta", beta_port, namespace=NS_BETA)
    squatter, alpha_port = sb.spawn_plain_listener()
    sb.register("alpha", alpha_port)
    sb.write_pid_file("alpha", beta_proc.pid)
    rc, out = sb.run("alpha")
    assert alive(beta_proc), "停了别的 bot 的 dispatcher: %s" % out
    assert alive(squatter), "杀了占端口的无关进程: %s" % out
    assert rc == 3, out
    assert sb.bun_calls() == [], "端口被占却尝试起了 bun"


def test_pid文件指向不像dispatcher的普通进程_该进程仍存活(sb):
    sb.register("alpha", unused_port())
    plain, _ = sb.spawn_plain_sleeper()
    sb.write_pid_file("alpha", plain.pid)
    rc, out = sb.run("alpha")
    assert alive(plain), "停了不是 dispatcher 的进程: %s" % out
    assert rc in (0, 3), out


def test_本bot自己的dispatcher_命令行带本bot标记_停掉旧进程并拉起新的_退出0(sb):
    """正向路径：命令行带 --bot=alpha、pid 文件记在 alpha 名下，只允许退出 0（不再接受 3）。"""
    mine, port = sb.spawn_lookalike(listen=True, bot_name="alpha", bot_flag="alpha")
    sb.register("alpha", port)
    sb.write_pid_file("alpha", mine.pid)
    rc, out = sb.run("alpha")
    assert rc == 0, out
    deadline = time.time() + 3
    while alive(mine) and time.time() < deadline:
        time.sleep(0.05)
    assert not alive(mine), "退出码 0（已重启）但旧进程还活着"
    assert len(sb.bun_calls()) == 1, "应恰好拉起 1 个新进程: %r" % sb.bun_calls()
    log = sb.bun_log.read_text(encoding="utf-8")
    assert "BOT_NAME=alpha" in log and "DISPATCHER_PORT=%d" % port in log, log
    args_lines = [ln for ln in log.splitlines() if ln.startswith("ARGS=")]
    assert len(args_lines) == 1 and "--bot=alpha" in args_lines[0].split(), "新进程命令行应带 --bot=alpha: %r" % args_lines


def test_命令行带别的bot标记但pid文件记在本bot名下_不停该进程_本bot端口空闲时照常拉起退出0(sb):
    """反向：--bot=beta 的进程被记在 alpha.pid 里（pid 复用），重启 alpha 不得停它。"""
    beta_proc, beta_port = sb.spawn_lookalike(listen=True, bot_name="beta", bot_flag="beta")
    sb.register("beta", beta_port, namespace=NS_BETA)
    alpha_port = unused_port()
    sb.register("alpha", alpha_port)
    sb.write_pid_file("alpha", beta_proc.pid)
    rc, out = sb.run("alpha")
    assert alive(beta_proc), "停了命令行带 --bot=beta 的进程（退出码 %s）: %s" % (rc, out)
    assert port_open(beta_port), "beta 的端口不再可连"
    assert rc == 0, out
    assert len(sb.bun_calls()) == 1, "alpha 没在跑且端口空闲，应拉起 1 个新进程: %r" % sb.bun_calls()


# ───────────────── 修订2-d：本 bot 端口被无 pid 记录的进程占着 → 退出 3 ─────────────────
def test_端口被无pid文件的进程占着_退出3_占端口进程仍存活_没有启动新进程(sb):
    squatter, port = sb.spawn_plain_listener()
    sb.register("alpha", port)
    rc, out = sb.run("alpha")
    assert rc == 3, out
    assert alive(squatter), "盲杀了占端口的进程: %s" % out
    assert port_open(port)
    assert sb.bun_calls() == [], "端口被占却尝试起了 bun"
    assert not (sb.run_dir / "alpha.pid").exists(), "端口被占却写了 pid 文件"


def test_端口被占且pid文件是已死进程的陈旧pid_退出3_占端口进程仍存活_没有启动新进程(sb):
    squatter, port = sb.spawn_plain_listener()
    sb.register("alpha", port)
    sb.write_pid_file("alpha", sb.dead_pid())
    rc, out = sb.run("alpha")
    assert rc == 3, out
    assert alive(squatter), "盲杀了占端口的进程: %s" % out
    assert sb.bun_calls() == []


def test_端口被占的输出不含令牌(sb):
    squatter, port = sb.spawn_plain_listener()
    sb.register("alpha", port)
    rc, out = sb.run("alpha")
    assert rc == 3, out
    assert TOKEN_SENTINEL not in out


# ───────────────── 修订2-e：shellcheck ─────────────────
def test_shellcheck可用时restart_one零告警():
    exe = shutil.which("shellcheck")
    if not exe:
        pytest.skip("本机没装 shellcheck")
    r = subprocess.run([exe, "-S", "warning", str(SCRIPT)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
