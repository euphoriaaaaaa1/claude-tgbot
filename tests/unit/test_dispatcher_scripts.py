# -*- coding: utf-8 -*-
"""T20 dispatcher 脚本：静态检查、注册表空名单、停用不拉起、真实启停沙箱。

沙箱只跑假 bun（记录环境变量后 sleep），绝不启动真实 dispatcher、不连 Telegram。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTART = REPO_ROOT / "restart-bots.example.sh"
STOP = REPO_ROOT / "stop-bots.example.sh"
LIB = REPO_ROOT / "dispatcher" / "bot-enabled.sh"
IDLE_WAIT = REPO_ROOT / "dispatcher" / "worker-idle-wait.sh"
REAP = REPO_ROOT / "dispatcher" / "reap-stale-workers.sh"


def _env(tmp_path, **extra):
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("DISPATCHER_PORT_"):
            env.pop(key)
    env.update({
        "HOME": str(tmp_path / "home"),
        "CLAUDEBOT_CONFIG_DIR": str(tmp_path / "configs"),
        "DIRECTOR_DISABLED_DIR": str(tmp_path / "disabled"),
        "CLAUDEBOT_CHANNELS_ROOT": str(tmp_path / "channels"),
        "CLAUDEBOT_RUN_DIR": str(tmp_path / "run"),
        "CLAUDEBOT_LOG_DIR": str(tmp_path / "logs"),
        "CLAUDEBOT_PYTHON": sys.executable,
    })
    env.pop("HUB_CONFIGS_DIR", None)
    env.pop("HUB_CHANNELS_DIR", None)
    env.update(extra)
    return env


def _write_bot(cfgdir, bot_id, **fields):
    cfgdir.mkdir(parents=True, exist_ok=True)
    lines = ["id: %s" % bot_id, "display_name: %s" % bot_id]
    lines += ["%s: %s" % (k, v) for k, v in fields.items()]
    (cfgdir / ("%s.yml" % bot_id)).write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_channel(tmp_path, bot_id, token="123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"):
    d = tmp_path / "channels" / bot_id
    d.mkdir(parents=True, exist_ok=True)
    (d / ".env").write_text("TELEGRAM_BOT_TOKEN=%s\n" % token, encoding="utf-8")
    return d


def _wait_for_file(path, timeout=5.0):
    """子进程被调度、写完文件需要一点时间；固定 sleep 会偶发。"""
    end = time.time() + timeout
    while time.time() < end:
        if path.exists() and path.stat().st_size > 0:
            return True
        time.sleep(0.05)
    return False


def _fake_bun(tmp_path):
    p = tmp_path / "bin" / "bun"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "#!/bin/sh\n"
        "{\n"
        "  printf 'BOT_NAME=%s\\n' \"$BOT_NAME\"\n"
        "  printf 'DISPATCHER_PORT=%s\\n' \"$DISPATCHER_PORT\"\n"
        "  printf 'BOT_NAMESPACE=%s\\n' \"$BOT_NAMESPACE\"\n"
        "  printf 'CHANNEL_DIR=%s\\n' \"$CHANNEL_DIR\"\n"
        "} >> \"$FAKE_BUN_LOG\"\n"
        "exec sleep 30\n",
        encoding="utf-8",
    )
    p.chmod(0o755)
    return p


NS_ALPHA = "11111111-1111-4111-8111-111111111111"
NS_BETA = "22222222-2222-4222-8222-222222222222"


# ---------------------------------------------------------------- 静态检查

def _shell_scripts():
    skip = {".git", "node_modules", "__pycache__", "tests", ".devflow"}
    out = []
    for p in REPO_ROOT.rglob("*.sh"):
        if any(part in skip for part in p.relative_to(REPO_ROOT).parts):
            continue
        out.append(p)
    return sorted(out)


def test_全部shell脚本通过bash语法检查():
    scripts = _shell_scripts()
    assert scripts, "没找到任何 shell 脚本"
    for p in scripts:
        r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, "%s: %s" % (p.relative_to(REPO_ROOT), r.stderr)


def test_shellcheck可用时对核心脚本零错误():
    exe = shutil.which("shellcheck")
    if not exe:
        pytest.skip("本机没装 shellcheck")
    targets = [
        REPO_ROOT / "install.sh", RESTART, STOP, LIB, IDLE_WAIT, REAP,
        REPO_ROOT / "scripts" / "watch-bot.sh", REPO_ROOT / "scripts" / "setup_keys.sh",
    ]
    r = subprocess.run([exe, "-S", "warning", *map(str, targets)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


def test_脚本里没有个人路径与真实bot名单():
    skip = {".git", "node_modules", "__pycache__", "tests", ".devflow"}
    bad = re.compile(r"/Users/wsxwj|com\.wsxwj|\byasuna\b|\bbot[234]\b|\blili\b|\bchenlulu\b")
    hits = []
    for p in REPO_ROOT.rglob("*"):
        if not p.is_file() or p.suffix not in (".sh", ".ps1"):
            continue
        if any(part in skip for part in p.relative_to(REPO_ROOT).parts):
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        if bad.search(text):
            hits.append(str(p.relative_to(REPO_ROOT)))
    assert hits == [], "脚本里还有个人痕迹/真实名单：%s" % hits


def test_windows脚本静态契约():
    windows = sorted((REPO_ROOT / "windows").glob("*.ps1"))
    assert windows, "windows/ 下没有 ps1"
    start = (REPO_ROOT / "windows" / "start-bots.ps1").read_text(encoding="utf-8-sig")
    stop = (REPO_ROOT / "windows" / "stop-bots.ps1").read_text(encoding="utf-8-sig")
    restart = (REPO_ROOT / "windows" / "restart-bots.ps1").read_text(encoding="utf-8-sig")
    register = (REPO_ROOT / "windows" / "register-tasks.ps1").read_text(encoding="utf-8-sig")
    provider = (REPO_ROOT / "windows" / "start-provider-web.ps1").read_text(encoding="utf-8-sig")
    watch = (REPO_ROOT / "windows" / "watch-bot.ps1").read_text(encoding="utf-8-sig")

    # 名单只来自注册表；启动判活用免口令的 /health（/status 需要 token）
    for text, name in ((start, "start-bots.ps1"), (register, "register-tasks.ps1"),
                       (watch, "watch-bot.ps1")):
        assert "bots_registry" in text, "%s 没接注册表" % name
    assert "/health" in start and "Invoke-WebRequest" in start
    assert "tmux" not in start and "launchctl" not in start
    assert "$RepoDir" in stop and "dispatcher.ts" in stop
    assert "stop-bots.ps1" in restart and "start-bots.ps1" in restart
    # 注册/卸载只经 platform_services，不直接拼 schtasks
    assert "platform_services.py" in register
    assert "schtasks" not in register
    assert "provider-web" in register and "provider-proxy" in register
    # 不注册旧 cliproxy/Hub/moments-web 任务（注释里提到名字不算，这里查真实注册调用）
    assert not re.search(r'Register-Service\s+"(?:cliproxy|hub|moments-web)"', register, re.I)
    # 8767 入口
    assert "MOMENTS_WEB_MODE" in provider and "moments.web" in provider
    # 看护脚本不再硬编码默认 bot
    assert not re.search(r'param\(\[string\]\$Bot\s*=\s*"[^"]+"', watch), \
        "watch-bot.ps1 还留着硬编码的默认 bot"
    # Windows PowerShell 5.1 需要 UTF-8 BOM 才能正确读中文
    for p in windows:
        assert p.read_bytes().startswith(b"\xef\xbb\xbf"), "%s 缺 UTF-8 BOM" % p.name


def test_powershell可用时全部脚本语法通过():
    exe = shutil.which("pwsh")
    if not exe:
        pytest.skip("本机没有 pwsh/powershell，Windows 语法留给 VM/静态检查")
    for p in sorted((REPO_ROOT / "windows").glob("*.ps1")):
        quoted = "'" + str(p).replace("'", "''") + "'"
        cmd = (
            "$e=$null; [void][System.Management.Automation.Language.Parser]::ParseFile("
            + quoted + ",[ref]$null,[ref]$e); if ($e.Count -gt 0) { "
            "$e | ForEach-Object { $_.Message }; exit 1 }"
        )
        r = subprocess.run([exe, "-NoProfile", "-Command", cmd],
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, "%s: %s" % (p.name, r.stdout + r.stderr)


def test_systemd_autostart单元保持cgroup不被systemd顺手杀掉(tmp_path):
    """restart-bots.sh 拉起后台 dispatcher 后自己退出；oneshot + RemainAfterExit
    才能让 systemd 保持服务 active，否则默认 KillMode=control-group 会全杀。"""
    import platform_services as ps

    backend = ps.SystemdBackend(home=tmp_path, runner=lambda argv: ps._Completed(0),
                                system="Linux")
    autostart = backend.render_service(ps.service_spec("autostart", repo=REPO_ROOT))
    assert "Type=oneshot" in autostart
    assert "RemainAfterExit=yes" in autostart
    provider = backend.render_service(ps.service_spec("provider-web", repo=REPO_ROOT))
    assert "Type=simple" in provider
    assert "RemainAfterExit=yes" not in provider


# ---------------------------------------------------------------- 注册表 / 停用

def test_注册表空名单重启脚本非零退出(tmp_path):
    cfg = tmp_path / "configs"
    _write_bot(cfg, "beta", enabled="false", dispatcher_port=17802,
               namespace_uuid=NS_BETA)
    _write_channel(tmp_path, "beta")
    fake = _fake_bun(tmp_path)
    fake_log = tmp_path / "bun.log"
    env = _env(tmp_path, CLAUDEBOT_BUN=str(fake), FAKE_BUN_LOG=str(fake_log))

    r = subprocess.run(["bash", str(RESTART)], cwd=str(tmp_path), env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 0, "空名单却返回 0：%s" % r.stdout
    assert r.stderr.strip(), "失败时至少要有一行说明"
    assert not fake_log.exists(), "空名单却拉起了 bun"


def test_停用的bot不会被拉起且启用bot正常启动(tmp_path):
    cfg = tmp_path / "configs"
    _write_bot(cfg, "alpha", dispatcher_port=17801, namespace_uuid=NS_ALPHA)
    _write_bot(cfg, "beta", enabled="false", dispatcher_port=17802,
               namespace_uuid=NS_BETA)
    _write_channel(tmp_path, "alpha")
    _write_channel(tmp_path, "beta")
    fake = _fake_bun(tmp_path)
    fake_log = tmp_path / "bun.log"
    env = _env(tmp_path, CLAUDEBOT_BUN=str(fake), FAKE_BUN_LOG=str(fake_log))

    pid = None
    try:
        r = subprocess.run(["bash", str(RESTART)], cwd=str(tmp_path), env=env,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stdout + r.stderr
        assert _wait_for_file(fake_log), "假 bun 没被拉起"
        text = fake_log.read_text(encoding="utf-8")
        assert "BOT_NAME=alpha" in text
        assert "BOT_NAME=beta" not in text, "停用的 bot 被拉起来了"
        assert "DISPATCHER_PORT=17801" in text
        assert "BOT_NAMESPACE=%s" % NS_ALPHA in text

        pid_file = tmp_path / "run" / "alpha.pid"
        assert pid_file.is_file(), "没写 pid 文件"
        pid = int(pid_file.read_text().strip())

        r = subprocess.run(["bash", str(STOP)], cwd=str(tmp_path), env=env,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stdout + r.stderr
        time.sleep(0.3)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert not pid_file.exists()
        pid = None
    finally:
        if pid is not None:
            try:
                os.kill(pid, 9)
            except ProcessLookupError:
                pass


def test_注册表JSON与sh口径一致且停用可判定(tmp_path):
    cfg = tmp_path / "configs"
    _write_bot(cfg, "alpha", dispatcher_port=17801, namespace_uuid=NS_ALPHA)
    _write_bot(cfg, "beta", enabled="false", dispatcher_port=17802,
               namespace_uuid=NS_BETA)
    env = _env(tmp_path)
    cmd = (
        '. "$1"; registry_lines; echo "---"; '
        'if bot_stopped beta; then echo STOPPED; else echo ENABLED; fi; '
        'if bot_stopped alpha; then echo STOPPED; else echo ENABLED; fi'
    )
    r = subprocess.run(["bash", "-c", cmd, "_", str(LIB)], env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    head, tail = r.stdout.split("---", 1)
    assert [ln.split("\t")[0] for ln in head.strip().splitlines()] == ["alpha"]
    assert tail.strip().splitlines() == ["STOPPED", "ENABLED"]


def test_空名单registry_lines返回非零(tmp_path):
    cfg = tmp_path / "configs"
    _write_bot(cfg, "beta", enabled="false", namespace_uuid=NS_BETA)
    env = _env(tmp_path)
    r = subprocess.run(["bash", "-c", '. "$1"; registry_lines', "_", str(LIB)],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 1
    assert r.stdout.strip() == ""


# ---------------------------------------------------------------- worker 空闲探针

def _state(tmp_path, bot, busy, at, v=1):
    d = tmp_path / "channels" / bot
    d.mkdir(parents=True, exist_ok=True)
    (d / ".worker-state.json").write_text(
        json.dumps({"v": v, "busy": busy, "at": at, "chat": "1"}), encoding="utf-8")


def _run_idle(tmp_path, bot, max_sec="0"):
    env = _env(tmp_path, IDLE_WAIT_PY=sys.executable, IDLE_WAIT_POLL_MS="1")
    return subprocess.run(["bash", str(IDLE_WAIT), bot, max_sec], env=env,
                          capture_output=True, text=True, timeout=60)


def test_worker空闲立即放行(tmp_path):
    _state(tmp_path, "alpha", False, int(time.time() * 1000))
    r = _run_idle(tmp_path, "alpha")
    assert r.returncode == 0


def test_worker忙且新鲜等满上限返回1(tmp_path):
    _state(tmp_path, "alpha", True, int(time.time() * 1000))
    r = _run_idle(tmp_path, "alpha", "0")
    assert r.returncode == 1
    assert "idle_wait_timeout" in r.stderr


def test_worker状态陈旧按不忙处理(tmp_path):
    old = int(time.time() * 1000) - 16 * 60 * 1000
    _state(tmp_path, "alpha", True, old)
    assert _run_idle(tmp_path, "alpha").returncode == 0


@pytest.mark.parametrize("text", ["not-json", '{"v":2,"busy":true,"at":1}',
                                  '{"v":1,"busy":"yes","at":1}',
                                  '{"v":1,"busy":true,"at":"x"}'])
def test_worker状态坏掉按不忙处理(tmp_path, text):
    d = tmp_path / "channels" / "alpha"
    d.mkdir(parents=True)
    (d / ".worker-state.json").write_text(text, encoding="utf-8")
    assert _run_idle(tmp_path, "alpha").returncode == 0


def test_worker目录不存在按可杀处理(tmp_path):
    assert _run_idle(tmp_path, "ghost").returncode == 0


# ---------------------------------------------------------------- 孤儿 worker 清扫

def test_只清ppid为1的本仓库worker_plugin(tmp_path):
    ps = tmp_path / "fake-ps"
    ps.write_text(
        "#!/bin/sh\n"
        "cat <<'EOF'\n"
        "  101     1 bun %s/dispatcher/worker-plugin.ts\n"
        "  102   500 bun %s/dispatcher/worker-plugin.ts\n"
        "  103     1 bun /other/repo/dispatcher/worker-plugin.ts\n"
        "  104     1 bun %s/dispatcher/dispatcher.ts\n"
        "EOF\n"
        "exit 0\n" % (REPO_ROOT, REPO_ROOT, REPO_ROOT),
        encoding="utf-8",
    )
    ps.chmod(0o755)
    kill = tmp_path / "fake-kill"
    kill_log = tmp_path / "kill.log"
    kill.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$REAP_KILL_LOG\"\n", encoding="utf-8")
    kill.chmod(0o755)
    env = _env(tmp_path, REAP_PS_CMD=str(ps), REAP_KILL_CMD=str(kill),
               REAP_PATTERN=str(REPO_ROOT / "dispatcher" / "worker-plugin.ts"),
               REAP_KILL_LOG=str(kill_log))

    dry = subprocess.run(["bash", str(REAP), "--dry-run"], env=env,
                         capture_output=True, text=True, timeout=60)
    assert dry.returncode == 0
    assert "pid=101" in dry.stdout and "pid=102" not in dry.stdout
    assert not kill_log.exists() or kill_log.read_text() == ""

    real = subprocess.run(["bash", str(REAP)], env=env,
                          capture_output=True, text=True, timeout=60)
    assert real.returncode == 0
    assert kill_log.read_text(encoding="utf-8").strip() == "-9 101"
