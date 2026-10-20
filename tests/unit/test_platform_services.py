# -*- coding: utf-8 -*-
"""platform_services 跨平台服务抽象单测。

覆盖：三平台命令/配置构造、未知平台、dry-run 零写入、前缀保护。
macOS 用例会用一个临时 label 真实注册后立即卸载（不碰任何生产服务）；
Windows 用例按 ``sys.platform`` 跳过并注明 T02 已在 VM 验证计划任务基本操作。
不启动 8767、不连真实服务。
"""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

import platform_services as ps

REPO_ROOT = Path(ps.__file__).resolve().parent

# 私有前缀只用于负向断言；拆开拼接，避免公开仓出现这些字面量。
PRIVATE_LABEL = "com." + "wsxwj" + ".claudebotlife-provider-web"
PRIVATE_HOME = "/Users/" + "wsxwj"


class FakeRunner:
    """记录 argv，按预设返回；默认全部成功。"""

    def __init__(self, responses=None):
        self.calls: list[list[str]] = []
        self.responses = list(responses or [])

    def __call__(self, argv):
        self.calls.append([str(a) for a in argv])
        if self.responses:
            return self.responses.pop(0)
        return ps._Completed(0)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- launchd

def test_launchd_渲染plist并先卸后装(tmp_path):
    runner = FakeRunner()
    backend = ps.LaunchdBackend(home=tmp_path, runner=runner, system="Darwin", uid=501)
    spec = ps.ServiceSpec(
        "provider-web",
        command=("/usr/bin/python3", "-m", "moments.web"),
        description="管理台",
        working_dir="/srv/repo",
        env=(("MOMENTS_WEB_MODE", "provider"), ("MOMENTS_WEB_PORT", "8767")),
        run_at_load=True,
        keep_alive=True,
        log_dir=str(tmp_path / "logs"),
    )
    result = backend.register(spec)

    assert result.ok, result.detail
    assert result.commands[0][:3] == [
        "launchctl",
        "bootout",
        "gui/501/com.example.claudebotlife-provider-web",
    ]
    assert result.commands[1][:3] == ["launchctl", "bootstrap", "gui/501"]

    plist = tmp_path / "Library" / "LaunchAgents" / (
        "com.example.claudebotlife-provider-web.plist"
    )
    assert plist.exists()
    data = plistlib.loads(plist.read_bytes())
    assert data["Label"] == "com.example.claudebotlife-provider-web"
    assert data["ProgramArguments"] == ["/usr/bin/python3", "-m", "moments.web"]
    assert data["EnvironmentVariables"]["MOMENTS_WEB_MODE"] == "provider"
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] == {"SuccessfulExit": False, "Crashed": True}
    assert data["WorkingDirectory"] == "/srv/repo"
    text = _read(plist)
    assert "com." + "wsxwj" not in text
    assert PRIVATE_HOME not in text


def test_launchd_定时字段与calendar(tmp_path):
    backend = ps.LaunchdBackend(
        home=tmp_path, runner=FakeRunner(), system="Darwin", uid=501
    )
    interval = ps.ServiceSpec(
        "relations-tick",
        command=("/usr/bin/python3", "relations_tick.py"),
        interval_seconds=900,
    )
    backend.register(interval)
    data = plistlib.loads(backend.plist_path(interval).read_bytes())
    assert data["StartInterval"] == 900
    assert "StartCalendarInterval" not in data

    calendar = ps.ServiceSpec(
        "cleanup",
        command=("/usr/bin/python3", "cleanup.py"),
        calendar=ps.CalendarSchedule(hour=3, minute=0, weekday=0),
    )
    backend.register(calendar)
    data = plistlib.loads(backend.plist_path(calendar).read_bytes())
    assert data["StartCalendarInterval"] == {"Hour": 3, "Minute": 0, "Weekday": 0}


def test_launchd_status未加载不是错误(tmp_path):
    runner = FakeRunner([ps._Completed(113, "", "Could not find service")])
    backend = ps.LaunchdBackend(home=tmp_path, runner=runner, system="Darwin", uid=501)
    result = backend.status(ps.ServiceSpec("autostart"))
    assert result.ok is True
    assert result.loaded is False
    assert result.state == "not-loaded"


def test_launchd_status解析运行状态(tmp_path):
    output = "gui/501/com.example.claudebotlife-provider-web = {\n\tstate = running\n\tpid = 4321\n}\n"
    runner = FakeRunner([ps._Completed(0, output, "")])
    backend = ps.LaunchdBackend(home=tmp_path, runner=runner, system="Darwin", uid=501)
    result = backend.status(ps.ServiceSpec("provider-web"))
    assert result.loaded is True
    assert result.running is True
    assert result.pid == 4321


# ---------------------------------------------------------------- systemd

def test_systemd_服务与timer构造(tmp_path):
    runner = FakeRunner()
    backend = ps.SystemdBackend(home=tmp_path, runner=runner, system="Linux")
    spec = ps.ServiceSpec(
        "relations-tick",
        command=("/usr/bin/python3", "relations_tick.py"),
        working_dir="/srv/repo",
        env=(("PATH", "/usr/bin:/bin"),),
        interval_seconds=900,
    )
    result = backend.register(spec)

    assert result.ok, result.detail
    unit = _read(tmp_path / ".config/systemd/user/claudebotlife-relations-tick.service")
    timer = _read(tmp_path / ".config/systemd/user/claudebotlife-relations-tick.timer")
    assert "ExecStart=/usr/bin/python3 relations_tick.py" in unit
    assert 'Environment=PATH="/usr/bin:/bin"' in unit
    assert "Type=oneshot" in unit
    assert "OnUnitActiveSec=900s" in timer
    assert "Unit=claudebotlife-relations-tick.service" in timer
    assert runner.calls[0] == ["systemctl", "--user", "daemon-reload"]
    assert runner.calls[1] == [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "claudebotlife-relations-tick.timer",
    ]


def test_systemd_长驻服务keepalive(tmp_path):
    backend = ps.SystemdBackend(home=tmp_path, runner=FakeRunner(), system="Linux")
    spec = ps.ServiceSpec(
        "provider-web",
        command=("/usr/bin/python3", "-m", "moments.web"),
        run_at_load=True,
        keep_alive=True,
    )
    backend.register(spec)
    unit = _read(tmp_path / ".config/systemd/user/claudebotlife-provider-web.service")
    assert "Type=simple" in unit
    assert "Restart=on-failure" in unit
    assert "[Install]" in unit


def test_systemd_引号与百分号转义(tmp_path):
    backend = ps.SystemdBackend(home=tmp_path, runner=FakeRunner(), system="Linux")
    spec = ps.ServiceSpec(
        "provider-web",
        command=("/opt/My Python/bin/python3", "-m", "moments.web"),
        env=(("MOMENTS_WEB_MODE", "provider%test"),),
    )
    backend.register(spec)
    unit = _read(tmp_path / ".config/systemd/user/claudebotlife-provider-web.service")
    assert 'ExecStart="/opt/My Python/bin/python3" -m moments.web' in unit
    assert 'Environment=MOMENTS_WEB_MODE="provider%%test"' in unit


def test_systemd_status定时任务检查timer(tmp_path):
    runner = FakeRunner(
        [ps._Completed(0, "active\n", ""), ps._Completed(0, "enabled\n", "")]
    )
    backend = ps.SystemdBackend(home=tmp_path, runner=runner, system="Linux")
    spec = ps.ServiceSpec(
        "relations-tick",
        command=("/usr/bin/python3", "relations_tick.py"),
        interval_seconds=900,
    )
    result = backend.status(spec)
    assert result.loaded is True
    assert result.running is True
    assert runner.calls[0][-1] == "claudebotlife-relations-tick.timer"
    assert runner.calls[1][-1] == "claudebotlife-relations-tick.timer"


def test_cli_raw_label卸载不需要服务名(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ps._platform, "system", lambda: "Darwin")
    rc = ps.main(
        [
            "unregister",
            "--raw-label",
            "com.example.claudebotlife-selftest-old",
            "--home",
            str(tmp_path),
            "--dry-run",
        ]
    )
    assert rc == 0
    assert "unregister" in capsys.readouterr().out


# ---------------------------------------------------------------- Windows

def test_windows_命令构造与日历触发(tmp_path):
    runner = FakeRunner()
    backend = ps.WindowsBackend(home=tmp_path, runner=runner, system="Windows")
    spec = ps.ServiceSpec(
        "memory-compactor",
        command=("C:\\Python310\\python.exe", "memory\\memory_compactor.py"),
        working_dir="C:\\repo",
        env=(("PYTHONUTF8", "1"),),
        calendar=ps.CalendarSchedule(hour=4, minute=0, weekday=0),
    )
    result = backend.register(spec)

    assert result.ok, result.detail
    argv = runner.calls[0]
    assert argv[:3] == ["schtasks", "/Create", "/TN"]
    assert argv[3] == "claude-tgbot-memory-compactor"
    assert "/SC" in argv and "WEEKLY" in argv and "SUN" in argv
    tr = argv[argv.index("/TR") + 1]
    assert tr.startswith("cmd.exe /d /c cd /d \"C:\\repo\"")
    assert 'set "PYTHONUTF8=1"' in tr
    assert "C:\\Python310\\python.exe" in tr
    assert argv[-1] == "/F"


def test_windows_间隔任务按分钟取整(tmp_path):
    runner = FakeRunner()
    backend = ps.WindowsBackend(home=tmp_path, runner=runner, system="Windows")
    spec = ps.ServiceSpec(
        "relations-tick",
        command=("python.exe", "relations_tick.py"),
        interval_seconds=901,
    )
    backend.register(spec)
    argv = runner.calls[0]
    assert argv[argv.index("/SC") + 1] == "MINUTE"
    # 901 秒向上取整为 16 分钟
    assert argv[argv.index("/MO") + 1] == "16"


def test_windows_登录自启与raw_task操作(tmp_path):
    runner = FakeRunner()
    backend = ps.WindowsBackend(home=tmp_path, runner=runner, system="Windows")
    spec = ps.ServiceSpec(
        "dispatchers",
        command=("powershell", "-File", "start-bots.ps1"),
        run_at_load=True,
    )
    backend.register(spec)
    assert runner.calls[0][3] == "claude-tgbot-dispatchers"
    assert runner.calls[0][runner.calls[0].index("/SC") + 1] == "ONLOGON"

    raw = ps.ServiceSpec("", raw_label="claude-tgbot-self-initiate-old")
    result = backend.unregister(raw)
    assert result.ok
    assert runner.calls[1] == [
        "schtasks",
        "/Delete",
        "/TN",
        "claude-tgbot-self-initiate-old",
        "/F",
    ]


# ---------------------------------------------------------------- 平台与安全边界

def test_未知平台明确报错():
    with pytest.raises(ps.UnsupportedPlatform, match="Plan9"):
        ps.resolve_backend(system="Plan9")


def test_服务不适用于当前平台(tmp_path):
    backend = ps.resolve_backend(system="Darwin", home=tmp_path, runner=FakeRunner())
    spec = ps.service_spec("dispatchers", repo=tmp_path)
    with pytest.raises(ps.UnsupportedPlatform, match="Windows"):
        backend.register(spec)


def test_cli未知平台退出码2(monkeypatch, capsys):
    monkeypatch.setattr(ps._platform, "system", lambda: "Plan9")
    rc = ps.main(["status", "autostart"])
    assert rc == 2
    assert "不支持的服务管理平台" in capsys.readouterr().err


@pytest.mark.parametrize(
    "raw_label",
    [
        PRIVATE_LABEL,
        "com.example.other-provider-web",
        "com.claudebotlife.provider-web",
    ],
)
def test_launchd拒绝非公开版前缀(tmp_path, raw_label):
    backend = ps.LaunchdBackend(home=tmp_path, runner=FakeRunner(), system="Darwin", uid=501)
    spec = ps.ServiceSpec("", raw_label=raw_label)
    with pytest.raises(ps.SpecError, match="拒绝操作"):
        backend.unregister(spec)
    assert not (tmp_path / "Library").exists()


def test_launchd允许精确清理历史自启标签(tmp_path):
    backend = ps.LaunchdBackend(home=tmp_path, runner=FakeRunner(), system="Darwin", uid=501)
    for label in (
        "com.claudebotlife.autostart",
        "com.example.claudebotlife.autostart",
    ):
        spec = ps.ServiceSpec("", raw_label=label)
        assert backend.label(spec) == label
        assert backend.unregister(spec).ok


def test_systemd与windows也拒绝非公开版前缀(tmp_path):
    systemd = ps.SystemdBackend(home=tmp_path, runner=FakeRunner(), system="Linux")
    with pytest.raises(ps.SpecError):
        systemd.unregister(ps.ServiceSpec("", raw_label="com." + "wsxwj.claudebotlife-x"))
    windows = ps.WindowsBackend(home=tmp_path, runner=FakeRunner(), system="Windows")
    with pytest.raises(ps.SpecError):
        windows.unregister(ps.ServiceSpec("", raw_label="com.claudebotlife-x"))


# ---------------------------------------------------------------- dry-run

def test_launchd_dry_run零写入零执行(tmp_path):
    def forbidden(_argv):
        raise AssertionError("dry-run 不得执行任何子进程")

    backend = ps.LaunchdBackend(
        home=tmp_path, runner=forbidden, system="Darwin", uid=501, dry_run=True
    )
    spec = ps.ServiceSpec(
        "provider-web",
        command=("/usr/bin/python3", "-m", "moments.web"),
        run_at_load=True,
        log_dir=str(tmp_path / "logs"),
    )
    result = backend.register(spec)

    assert result.ok
    assert "dry-run" in result.detail
    assert any("bootstrap" in c for c in result.commands)
    assert not (tmp_path / "Library").exists()
    assert not (tmp_path / "logs").exists()


def test_systemd_dry_run零写入(tmp_path):
    backend = ps.SystemdBackend(
        home=tmp_path, runner=FakeRunner(), system="Linux", dry_run=True
    )
    spec = ps.ServiceSpec(
        "relations-tick",
        command=("/usr/bin/python3", "relations_tick.py"),
        interval_seconds=900,
    )
    result = backend.register(spec)
    assert result.ok
    assert not (tmp_path / ".config").exists()


def test_cli_dry_run零写入(tmp_path, capsys):
    rc = ps.main(
        [
            "register",
            "autostart",
            "--repo",
            str(tmp_path),
            "--home",
            str(tmp_path),
            "--dry-run",
            "--json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["commands"]
    assert not (tmp_path / "Library").exists()
    assert not (tmp_path / ".config").exists()


def test_self_initiate接受中文bot_id(tmp_path):
    """T04 的注册表允许中文 bot id；服务名不能把它挡掉。"""
    spec = ps.service_spec(
        "self-initiate-小助手", repo=tmp_path, bot="小助手", chat_id="123456"
    )
    assert spec.name == "self-initiate-小助手"
    backend = ps.LaunchdBackend(
        home=tmp_path, runner=FakeRunner(), system="Darwin", uid=501
    )
    assert backend.label(spec) == "com.example.claudebotlife-self-initiate-小助手"


def test_未知服务名明确报错(tmp_path):
    with pytest.raises(ps.SpecError, match="未知服务"):
        ps.service_spec("no-such-service", repo=tmp_path)


# ---------------------------------------------------------------- 实际注册（条件跑）

@pytest.mark.skipif(sys.platform != "darwin", reason="macOS launchd 实跑")
def test_macos_临时label真实注册与立即卸载(tmp_path):
    """临时 label + 立即卸载；绝不改名/停掉任何生产服务。"""
    domain = "gui/%d" % os.getuid()
    probe = subprocess.run(
        ["launchctl", "print", domain], capture_output=True, text=True, timeout=10
    )
    if probe.returncode != 0:
        pytest.skip("当前会话没有 GUI launchd domain（例如 SSH/CI）")

    label = "%sselftest-%d-%s" % (
        ps.LAUNCHD_PREFIX,
        os.getpid(),
        uuid.uuid4().hex[:8],
    )
    spec = ps.ServiceSpec(
        "selftest",
        command=("/bin/sleep", "30"),
        run_at_load=True,
        log_dir=str(tmp_path),
        raw_label=label,
    )
    backend = ps.LaunchdBackend(home=tmp_path, system="Darwin")
    try:
        registered = backend.register(spec)
        assert registered.ok, registered.detail
        status = backend.status(spec)
        assert status.loaded is True
    finally:
        removed = backend.unregister(spec)

    assert removed.ok, removed.detail
    assert backend.status(spec).loaded is False
    assert not backend.plist_path(spec).exists()


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows 计划任务实跑；T02 已在 Windows 11 VM 验证 schtasks 创建/运行/删除",
)
def test_windows_临时任务真实注册与立即卸载():
    label = "%sselftest-%d" % (ps.WINDOWS_PREFIX, os.getpid())
    spec = ps.ServiceSpec(
        "selftest",
        command=("cmd.exe", "/d", "/c", "exit", "0"),
        run_at_load=True,
        raw_label=label,
    )
    backend = ps.WindowsBackend(system="Windows")
    try:
        registered = backend.register(spec)
        assert registered.ok, registered.detail
        assert backend.status(spec).loaded is True
    finally:
        removed = backend.unregister(spec)
    assert removed.ok, removed.detail
    assert backend.status(spec).loaded is False


# ---------------------------------------------------------------- 安装器接线与静态卫生

def test_install_sh已改走platform_services():
    text = _read(REPO_ROOT / "install.sh")
    assert "platform_services.py register autostart" in text
    assert "load_agent()" not in text
    # 安装器本身不再直接拼任何平台命令。
    for command in ("launchctl", "systemctl", "schtasks"):
        assert command not in text, command


def test_静态检查无私人前缀与硬编码家目录():
    files = [
        REPO_ROOT / "platform_services.py",
        REPO_ROOT / "install.sh",
        *sorted((REPO_ROOT / "windows").glob("*.ps1")),
        *sorted((REPO_ROOT / "plist-templates").glob("*.tmpl")),
    ]
    for path in files:
        text = _read(path)
        assert "com." + "wsxwj" not in text, path
        assert PRIVATE_HOME not in text, path
