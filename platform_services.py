# -*- coding: utf-8 -*-
"""跨平台服务注册抽象（PLAN T05 / §6）。

**唯一允许直接调用 launchctl / systemctl / schtasks 的模块**。安装器（``install.sh``）
与 Windows 脚本一律经本模块注册、卸载、启停服务；其余代码不要自己拼平台命令。

三个后端：

- macOS：``launchd``，用户级 ``~/Library/LaunchAgents/<label>.plist``。
- Linux：``systemd --user``，``~/.config/systemd/user/<unit>.service``（定时任务再加
  ``.timer``）。
- Windows：计划任务，``schtasks``，任务名固定 ``claude-tgbot-*``。

约定：

- 固定前缀：launchd ``com.example.claudebotlife-*``、systemd
  ``claudebotlife-*``、Windows ``claude-tgbot-*``。``--raw-label`` 只接受带对应
  前缀的标签，防止误碰别家的生产服务。
- 幂等：注册先卸后装；卸载不存在的服务按成功处理。
- ``dry_run=True`` 只把计划执行的命令写进结果，**不建目录、不写文件、不执行任何
  子进程**。
- 未知平台抛 :class:`UnsupportedPlatform`，CLI 退出码 2；绝不静默成功。
- 只写用户级服务，不碰系统级 launchd/systemd，不要求管理员权限。

CLI 用法（安装器与 PowerShell 调的就是它）::

    python3 platform_services.py register autostart --repo /path/to/repo
    python3 platform_services.py unregister autostart
    python3 platform_services.py status provider-web --json
    python3 platform_services.py register self-initiate-<bot> --bot <bot> --chat-id 123
    python3 platform_services.py unregister --raw-label claude-tgbot-self-initiate-<bot>

退出码：0 成功；1 操作失败/后端不可用；2 参数错或平台不支持。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import plistlib
import platform as _platform
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

LAUNCHD_PREFIX = "com.example.claudebotlife-"
SYSTEMD_PREFIX = "claudebotlife-"
WINDOWS_PREFIX = "claude-tgbot-"

# 历史安装器用过的标签：只允许精确清理这些，绝不放开任意 com.* 标签。
LEGACY_LAUNCHD_LABELS = frozenset(
    {
        "com.claudebotlife.autostart",  # 旧版 install.sh
        "com.example.claudebotlife.autostart",  # 更早的手动 README
    }
)

# 服务名会拼进 unit/plist/任务名：允许 Unicode（bot id 可以是中文），但拒绝空白、
# 路径分隔符、NUL 与 systemd 的 % 说明符。
_NAME_RE = re.compile(r"^[^\s/\\\x00%]{1,128}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

ACTION_REGISTER = "register"
ACTION_UNREGISTER = "unregister"
ACTION_START = "start"
ACTION_STOP = "stop"
ACTION_STATUS = "status"
ACTION_KICKSTART = "kickstart"
ACTIONS = (
    ACTION_REGISTER,
    ACTION_UNREGISTER,
    ACTION_START,
    ACTION_STOP,
    ACTION_STATUS,
    ACTION_KICKSTART,
)


class ServiceError(RuntimeError):
    """服务抽象层错误的基类。"""


class UnsupportedPlatform(ServiceError):
    """当前平台不在支持范围内（或请求的服务不适用于该平台）。"""


class SpecError(ServiceError):
    """服务描述非法 / 参数错。"""


class BackendUnavailable(ServiceError):
    """平台命令缺失或无法执行（例如 Linux 上没有 systemctl）。"""


# ---------------------------------------------------------------- 数据模型

@dataclass(frozen=True)
class CalendarSchedule:
    """固定日历触发（launchd 语义：weekday 0=周日）。"""

    hour: int
    minute: int
    weekday: int | None = None

    def __post_init__(self):
        if not (0 <= int(self.hour) <= 23):
            raise SpecError("hour 必须在 0-23")
        if not (0 <= int(self.minute) <= 59):
            raise SpecError("minute 必须在 0-59")
        if self.weekday is not None and not (0 <= int(self.weekday) <= 6):
            raise SpecError("weekday 必须在 0-6（0=周日）")

    def launchd_dict(self) -> dict:
        data = {"Hour": int(self.hour), "Minute": int(self.minute)}
        if self.weekday is not None:
            data["Weekday"] = int(self.weekday)
        return data

    def systemd_oncalendar(self) -> str:
        names = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
        clock = "%02d:%02d:00" % (self.hour, self.minute)
        if self.weekday is None:
            return "*-*-* " + clock
        return "%s *-*-* %s" % (names[int(self.weekday)], clock)

    def schtasks_args(self) -> list[str]:
        clock = "%02d:%02d" % (self.hour, self.minute)
        if self.weekday is None:
            return ["/SC", "DAILY", "/ST", clock]
        days = ("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT")
        return ["/SC", "WEEKLY", "/D", days[int(self.weekday)], "/ST", clock]


@dataclass(frozen=True)
class ServiceSpec:
    """一个平台无关的服务描述。

    ``command`` 必须已经拆成 argv（不要传一整条 shell 字符串）。``env`` 用
    元组保存，保证可哈希、顺序稳定。
    """

    name: str
    command: tuple[str, ...] = ()
    description: str = ""
    working_dir: str | None = None
    env: tuple[tuple[str, str], ...] = ()
    run_at_load: bool = False
    keep_alive: bool = False
    interval_seconds: int | None = None
    calendar: CalendarSchedule | None = None
    log_dir: str | None = None
    platforms: tuple[str, ...] = ()
    raw_label: str | None = None

    def validate(self, *, need_command: bool = True) -> None:
        if not self.raw_label:
            if not isinstance(self.name, str) or not _NAME_RE.match(self.name):
                raise SpecError(
                    "服务名只允许字母、数字、点、下划线、连字符（1-64 字符）：%r"
                    % (self.name,)
                )
        else:
            if not _LABEL_RE.match(self.raw_label):
                raise SpecError("标签含非法字符：%r" % (self.raw_label,))
        if need_command:
            if not self.command or not all(isinstance(a, str) for a in self.command):
                raise SpecError("command 必须是非空的 argv 列表")
            if any("\x00" in a for a in self.command):
                raise SpecError("command 不能包含 NUL")
        if self.interval_seconds is not None:
            if not isinstance(self.interval_seconds, int) or self.interval_seconds <= 0:
                raise SpecError("interval_seconds 必须是正整数")
            if self.interval_seconds > 365 * 24 * 3600:
                raise SpecError("interval_seconds 过大")
        if self.interval_seconds and self.calendar:
            raise SpecError("interval_seconds 与 calendar 只能二选一")
        for key, value in self.env:
            if not _ENV_KEY_RE.match(key):
                raise SpecError("环境变量名非法：%r" % (key,))
            if "\x00" in value:
                raise SpecError("环境变量 %s 的值不能包含 NUL" % key)
        if self.working_dir is not None and "\x00" in self.working_dir:
            raise SpecError("working_dir 不能包含 NUL")
        if "\n" in self.description or "\r" in self.description:
            raise SpecError("description 不能包含换行")

    def env_dict(self) -> dict[str, str]:
        return {k: v for k, v in self.env}

    def has_schedule(self) -> bool:
        return self.interval_seconds is not None or self.calendar is not None


@dataclass
class OperationResult:
    ok: bool
    action: str
    service: str
    platform: str
    detail: str = ""
    commands: list[list[str]] = field(default_factory=list)
    state: str | None = None
    loaded: bool | None = None
    running: bool | None = None
    pid: int | None = None

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "action": self.action,
            "service": self.service,
            "platform": self.platform,
            "detail": self.detail,
            "commands": self.commands,
            "state": self.state,
            "loaded": self.loaded,
            "running": self.running,
            "pid": self.pid,
        }


class _Completed:
    """runner 的最小返回契约（与 subprocess.CompletedProcess 同形）。"""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


Runner = Callable[[Sequence[str]], _Completed]


def _default_runner(argv: Sequence[str]) -> _Completed:
    try:
        proc = subprocess.run(
            list(argv), capture_output=True, text=True, timeout=30, check=False
        )
    except FileNotFoundError as exc:
        raise BackendUnavailable(
            "找不到平台命令 %s（当前环境未提供该服务管理器）" % (argv[0],)
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise BackendUnavailable("平台命令超时：%s" % (argv[0],)) from exc
    return _Completed(proc.returncode, proc.stdout or "", proc.stderr or "")


def _require_prefix(label: str, prefix: str, what: str) -> str:
    if not label.startswith(prefix):
        raise SpecError(
            "拒绝操作非公开版%s %r：必须以 %r 开头" % (what, label, prefix)
        )
    return label


class ServiceBackend:
    """后端基类：统一 runner、dry-run 与结果收集。"""

    platform_name = ""

    def __init__(
        self,
        *,
        home: str | os.PathLike[str] | None = None,
        runner: Runner | None = None,
        dry_run: bool = False,
        system: str | None = None,
        uid: int | None = None,
    ):
        self.home = Path(home).expanduser() if home else Path.home()
        self.runner = runner or _default_runner
        self.dry_run = dry_run
        self.system = system or _platform.system()
        self.uid = uid
        if self.uid is None and hasattr(os, "getuid"):
            self.uid = os.getuid()
        self._commands: list[list[str]] = []

    # -- 子类实现 ---------------------------------------------------------
    def label(self, spec: ServiceSpec) -> str:
        raise NotImplementedError

    def register(self, spec: ServiceSpec) -> OperationResult:
        raise NotImplementedError

    def unregister(self, spec: ServiceSpec) -> OperationResult:
        raise NotImplementedError

    def start(self, spec: ServiceSpec) -> OperationResult:
        raise NotImplementedError

    def stop(self, spec: ServiceSpec) -> OperationResult:
        raise NotImplementedError

    def status(self, spec: ServiceSpec) -> OperationResult:
        raise NotImplementedError

    def kickstart(self, spec: ServiceSpec) -> OperationResult:
        raise NotImplementedError

    # -- 公共工具 ---------------------------------------------------------
    def _begin(self) -> None:
        self._commands = []

    def _run(self, argv: Sequence[str]) -> _Completed:
        argv = [str(a) for a in argv]
        self._commands.append(argv)
        if self.dry_run:
            return _Completed(0, "", "")
        return self.runner(argv)

    def _result(
        self,
        action: str,
        spec: ServiceSpec,
        *,
        ok: bool = True,
        detail: str = "",
        state: str | None = None,
        loaded: bool | None = None,
        running: bool | None = None,
        pid: int | None = None,
    ) -> OperationResult:
        return OperationResult(
            ok=ok,
            action=action,
            service=spec.name or spec.raw_label or "",
            platform=self.platform_name,
            detail=detail,
            commands=[list(c) for c in self._commands],
            state=state,
            loaded=loaded,
            running=running,
            pid=pid,
        )

    def _check_platform(self, spec: ServiceSpec) -> None:
        if spec.platforms and self.system not in spec.platforms:
            raise UnsupportedPlatform(
                "服务 %s 不支持当前平台 %s（支持：%s）"
                % (spec.name, self.system, ", ".join(spec.platforms))
            )


# ---------------------------------------------------------------- launchd

class LaunchdBackend(ServiceBackend):
    platform_name = "launchd"

    @property
    def launch_agents_dir(self) -> Path:
        return self.home / "Library" / "LaunchAgents"

    def label(self, spec: ServiceSpec) -> str:
        if spec.raw_label:
            if spec.raw_label in LEGACY_LAUNCHD_LABELS:
                return spec.raw_label
            return _require_prefix(spec.raw_label, LAUNCHD_PREFIX, "launchd 标签")
        return LAUNCHD_PREFIX + spec.name

    def plist_path(self, spec: ServiceSpec) -> Path:
        return self.launch_agents_dir / (self.label(spec) + ".plist")

    def _domain(self) -> str:
        if self.uid is None:
            raise BackendUnavailable("无法确定当前用户 UID，不能操作 launchd")
        return "gui/%d" % self.uid

    def _target(self, spec: ServiceSpec) -> str:
        return "%s/%s" % (self._domain(), self.label(spec))

    def render(self, spec: ServiceSpec) -> bytes:
        spec.validate()
        payload: dict = {
            "Label": self.label(spec),
            "ProgramArguments": list(spec.command),
            "RunAtLoad": bool(spec.run_at_load),
            "AbandonProcessGroup": True,
        }
        if spec.working_dir:
            payload["WorkingDirectory"] = spec.working_dir
        if spec.env:
            payload["EnvironmentVariables"] = spec.env_dict()
        if spec.interval_seconds:
            payload["StartInterval"] = int(spec.interval_seconds)
        if spec.calendar:
            payload["StartCalendarInterval"] = spec.calendar.launchd_dict()
        if spec.keep_alive:
            payload["KeepAlive"] = {"SuccessfulExit": False, "Crashed": True}
            payload["ThrottleInterval"] = 30
        if spec.log_dir:
            log_dir = Path(spec.log_dir)
            payload["StandardOutPath"] = str(log_dir / (spec.name + ".stdout"))
            payload["StandardErrorPath"] = str(log_dir / (spec.name + ".stderr"))
        return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)

    def _write_plist(self, spec: ServiceSpec) -> Path:
        path = self.plist_path(spec)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(path.name + ".tmp-%d" % os.getpid())
        tmp.write_bytes(self.render(spec))
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
        return path

    def register(self, spec: ServiceSpec) -> OperationResult:
        spec.validate()
        self._check_platform(spec)
        self._begin()
        path = self.plist_path(spec)
        detail = ""
        if not self.dry_run:
            path = self._write_plist(spec)
        # 先卸后装：已加载时 bootstrap 会报 EALREADY，先 bootout 保证幂等。
        self._run(["launchctl", "bootout", self._target(spec)])
        proc = self._run(["launchctl", "bootstrap", self._domain(), str(path)])
        ok = proc.returncode == 0
        if not ok:
            detail = (
                "bootstrap 失败（退出码 %d）：%s\n"
                "plist 已写入 %s；图形登录后可手动执行："
                "launchctl bootstrap %s %s"
                % (
                    proc.returncode,
                    (proc.stderr or proc.stdout).strip(),
                    path,
                    self._domain(),
                    path,
                )
            )
        elif self.dry_run:
            detail = "dry-run：未写 plist、未执行 launchctl"
        return self._result(
            ACTION_REGISTER, spec, ok=ok, detail=detail, loaded=ok or self.dry_run
        )

    def unregister(self, spec: ServiceSpec) -> OperationResult:
        spec.validate(need_command=False)
        self._check_platform(spec)
        self._begin()
        path = self.plist_path(spec)
        proc = self._run(["launchctl", "bootout", self._target(spec)])
        removed = False
        if not self.dry_run and path.exists():
            path.unlink()
            removed = True
        detail = "dry-run：未执行 launchctl、未删除 plist" if self.dry_run else ""
        # bootout 对未加载的服务返回非零；卸载仍算成功（幂等）。
        if proc.returncode != 0 and not self.dry_run:
            detail = "服务原本未加载（launchctl bootout 退出码 %d）；已清理 plist=%s" % (
                proc.returncode,
                removed,
            )
        return self._result(
            ACTION_UNREGISTER, spec, ok=True, detail=detail, loaded=False
        )

    def _print(self, spec: ServiceSpec) -> _Completed:
        return self._run(["launchctl", "print", self._target(spec)])

    def start(self, spec: ServiceSpec) -> OperationResult:
        spec.validate(need_command=False)
        self._check_platform(spec)
        self._begin()
        if self.dry_run:
            self._run(["launchctl", "bootstrap", self._domain(), str(self.plist_path(spec))])
            return self._result(
                ACTION_START, spec, detail="dry-run：未执行 launchctl", loaded=None
            )
        current = self._print(spec)
        if current.returncode == 0:
            proc = self._run(["launchctl", "kickstart", "-k", self._target(spec)])
            how = "kickstart"
        else:
            proc = self._run(
                ["launchctl", "bootstrap", self._domain(), str(self.plist_path(spec))]
            )
            how = "bootstrap"
        ok = proc.returncode == 0
        return self._result(
            ACTION_START,
            spec,
            ok=ok,
            detail="" if ok else "%s 失败：%s" % (how, (proc.stderr or proc.stdout).strip()),
            loaded=ok,
        )

    def stop(self, spec: ServiceSpec) -> OperationResult:
        spec.validate(need_command=False)
        self._check_platform(spec)
        self._begin()
        proc = self._run(["launchctl", "bootout", self._target(spec)])
        ok = proc.returncode == 0 or self.dry_run
        return self._result(
            ACTION_STOP,
            spec,
            ok=ok,
            detail="" if ok else "bootout 失败：%s" % (proc.stderr or proc.stdout).strip(),
            loaded=False if ok else None,
        )

    def kickstart(self, spec: ServiceSpec) -> OperationResult:
        spec.validate(need_command=False)
        self._check_platform(spec)
        self._begin()
        proc = self._run(["launchctl", "kickstart", "-k", self._target(spec)])
        ok = proc.returncode == 0 or self.dry_run
        return self._result(
            ACTION_KICKSTART,
            spec,
            ok=ok,
            detail="" if ok else "kickstart 失败（服务未加载？先 register）：%s"
            % (proc.stderr or proc.stdout).strip(),
            running=True if ok else None,
        )

    def status(self, spec: ServiceSpec) -> OperationResult:
        spec.validate(need_command=False)
        self._check_platform(spec)
        self._begin()
        if self.dry_run:
            self._run(["launchctl", "print", self._target(spec)])
            return self._result(
                ACTION_STATUS, spec, detail="dry-run：未执行 launchctl", state="unknown"
            )
        proc = self._print(spec)
        if proc.returncode != 0:
            return self._result(
                ACTION_STATUS, spec, state="not-loaded", loaded=False, running=False
            )
        text = proc.stdout or ""
        running = bool(re.search(r"(?m)^\s*state\s*=\s*running\s*$", text))
        pid_match = re.search(r"(?m)^\s*pid\s*=\s*(\d+)\s*$", text)
        return self._result(
            ACTION_STATUS,
            spec,
            state="running" if running else "loaded",
            loaded=True,
            running=running,
            pid=int(pid_match.group(1)) if pid_match else None,
        )


# ---------------------------------------------------------------- systemd

def _systemd_quote(word: str) -> str:
    """按 systemd.syntax 的规则给单个参数加引号。"""
    if word and re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", word):
        return word
    escaped = word.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return '"' + escaped + '"'


def _systemd_env(key: str, value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return '%s="%s"' % (key, escaped)


class SystemdBackend(ServiceBackend):
    platform_name = "systemd"

    @property
    def unit_dir(self) -> Path:
        return self.home / ".config" / "systemd" / "user"

    def _base(self, spec: ServiceSpec) -> str:
        if spec.raw_label:
            base = _require_prefix(spec.raw_label, SYSTEMD_PREFIX, "systemd 单元")
            for suffix in (".service", ".timer"):
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
            return base
        return SYSTEMD_PREFIX + spec.name

    def service_name(self, spec: ServiceSpec) -> str:
        return self._base(spec) + ".service"

    def timer_name(self, spec: ServiceSpec) -> str:
        return self._base(spec) + ".timer"

    def service_path(self, spec: ServiceSpec) -> Path:
        return self.unit_dir / self.service_name(spec)

    def timer_path(self, spec: ServiceSpec) -> Path:
        return self.unit_dir / self.timer_name(spec)

    def render_service(self, spec: ServiceSpec) -> str:
        spec.validate()
        lines = ["[Unit]", "Description=%s" % (spec.description or spec.name), ""]
        lines += ["[Service]", "Type=%s" % ("oneshot" if spec.has_schedule() else "simple")]
        lines.append(
            "ExecStart=" + " ".join(_systemd_quote(a) for a in spec.command)
        )
        if spec.working_dir:
            lines.append("WorkingDirectory=" + spec.working_dir)
        for key, value in spec.env:
            lines.append("Environment=" + _systemd_env(key, value))
        if spec.keep_alive and not spec.has_schedule():
            lines.append("Restart=on-failure")
            lines.append("RestartSec=30")
        if not spec.has_schedule():
            lines += ["", "[Install]", "WantedBy=default.target"]
        return "\n".join(lines) + "\n"

    def render_timer(self, spec: ServiceSpec) -> str:
        if not spec.has_schedule():
            raise SpecError("没有定时配置，不能生成 timer")
        lines = ["[Unit]", "Description=%s timer" % (spec.description or spec.name), ""]
        lines.append("[Timer]")
        if spec.calendar:
            lines.append("OnCalendar=" + spec.calendar.systemd_oncalendar())
            lines.append("Persistent=true")
        else:
            lines.append("OnBootSec=%ds" % int(spec.interval_seconds))
            lines.append("OnUnitActiveSec=%ds" % int(spec.interval_seconds))
        lines.append("Unit=" + self.service_name(spec))
        lines += ["", "[Install]", "WantedBy=timers.target"]
        return "\n".join(lines) + "\n"

    def _write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_name(path.name + ".tmp-%d" % os.getpid())
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)

    def _systemctl(self, *args: str) -> _Completed:
        return self._run(["systemctl", "--user", *args])

    def register(self, spec: ServiceSpec) -> OperationResult:
        spec.validate()
        self._check_platform(spec)
        self._begin()
        if not self.dry_run:
            self._write(self.service_path(spec), self.render_service(spec))
            if spec.has_schedule():
                self._write(self.timer_path(spec), self.render_timer(spec))
        self._systemctl("daemon-reload")
        target = self.timer_name(spec) if spec.has_schedule() else self.service_name(spec)
        if spec.has_schedule() or spec.run_at_load:
            enable = self._systemctl("enable", "--now", target)
        else:
            enable = self._systemctl("enable", target)
        ok = enable.returncode == 0
        detail = "" if ok else "systemctl enable 失败：%s" % (
            enable.stderr or enable.stdout
        ).strip()
        if self.dry_run:
            detail = "dry-run：未写 unit、未执行 systemctl"
        return self._result(ACTION_REGISTER, spec, ok=ok, detail=detail, loaded=ok)

    def unregister(self, spec: ServiceSpec) -> OperationResult:
        spec.validate(need_command=False)
        self._check_platform(spec)
        self._begin()
        target = self.timer_name(spec) if spec.has_schedule() else self.service_name(spec)
        self._systemctl("disable", "--now", target)
        self._systemctl("stop", target)
        if spec.has_schedule():
            # timer 触发的 oneshot service 可能正好在跑，一并停掉。
            self._systemctl("stop", self.service_name(spec))
        if not self.dry_run:
            for path in (self.timer_path(spec), self.service_path(spec)):
                if path.exists():
                    path.unlink()
        self._systemctl("daemon-reload")
        detail = "dry-run：未执行 systemctl、未删除 unit" if self.dry_run else ""
        return self._result(
            ACTION_UNREGISTER, spec, ok=True, detail=detail, loaded=False
        )

    def start(self, spec: ServiceSpec) -> OperationResult:
        spec.validate(need_command=False)
        self._check_platform(spec)
        self._begin()
        target = self.timer_name(spec) if spec.has_schedule() else self.service_name(spec)
        proc = self._systemctl("start", target)
        ok = proc.returncode == 0 or self.dry_run
        return self._result(
            ACTION_START,
            spec,
            ok=ok,
            detail="" if ok else "systemctl start 失败：%s"
            % (proc.stderr or proc.stdout).strip(),
            loaded=ok,
        )

    def stop(self, spec: ServiceSpec) -> OperationResult:
        spec.validate(need_command=False)
        self._check_platform(spec)
        self._begin()
        target = self.timer_name(spec) if spec.has_schedule() else self.service_name(spec)
        proc = self._systemctl("stop", target)
        ok = proc.returncode == 0 or self.dry_run
        return self._result(
            ACTION_STOP,
            spec,
            ok=ok,
            detail="" if ok else "systemctl stop 失败：%s"
            % (proc.stderr or proc.stdout).strip(),
            loaded=False if ok else None,
        )

    def kickstart(self, spec: ServiceSpec) -> OperationResult:
        spec.validate(need_command=False)
        self._check_platform(spec)
        self._begin()
        target = self.timer_name(spec) if spec.has_schedule() else self.service_name(spec)
        proc = self._systemctl("restart", target)
        ok = proc.returncode == 0 or self.dry_run
        return self._result(
            ACTION_KICKSTART,
            spec,
            ok=ok,
            detail="" if ok else "systemctl restart 失败：%s"
            % (proc.stderr or proc.stdout).strip(),
            running=True if ok else None,
        )

    def status(self, spec: ServiceSpec) -> OperationResult:
        spec.validate(need_command=False)
        self._check_platform(spec)
        self._begin()
        if self.dry_run:
            self._run(["systemctl", "--user", "is-active", self.service_name(spec)])
            return self._result(
                ACTION_STATUS, spec, detail="dry-run：未执行 systemctl", state="unknown"
            )
        target = self.timer_name(spec) if spec.has_schedule() else self.service_name(spec)
        active = self._systemctl("is-active", target)
        enabled = self._systemctl("is-enabled", target)
        state = (active.stdout or "").strip() or "unknown"
        running = active.returncode == 0 and state == "active"
        loaded = enabled.returncode == 0
        return self._result(
            ACTION_STATUS,
            spec,
            state=state,
            loaded=loaded,
            running=running,
        )


# ---------------------------------------------------------------- Windows

class WindowsBackend(ServiceBackend):
    platform_name = "windows"

    def task_name(self, spec: ServiceSpec) -> str:
        if spec.raw_label:
            return _require_prefix(spec.raw_label, WINDOWS_PREFIX, "计划任务名")
        return WINDOWS_PREFIX + spec.name

    def _trigger_args(self, spec: ServiceSpec) -> list[str]:
        if spec.calendar:
            return spec.calendar.schtasks_args()
        if spec.interval_seconds:
            minutes = max(1, int(math.ceil(spec.interval_seconds / 60.0)))
            return ["/SC", "MINUTE", "/MO", str(minutes)]
        if spec.run_at_load:
            return ["/SC", "ONLOGON"]
        return ["/SC", "ONCE", "/ST", "00:00"]

    def command_line(self, spec: ServiceSpec) -> str:
        base = subprocess.list2cmdline(list(spec.command))
        if not spec.working_dir and not spec.env:
            return base
        parts = []
        if spec.working_dir:
            parts.append('cd /d "%s"' % spec.working_dir)
        for key, value in spec.env:
            parts.append('set "%s=%s"' % (key, value.replace('"', '""')))
        parts.append(base)
        return "cmd.exe /d /c " + " && ".join(parts)

    def register(self, spec: ServiceSpec) -> OperationResult:
        spec.validate()
        self._check_platform(spec)
        self._begin()
        argv = [
            "schtasks",
            "/Create",
            "/TN",
            self.task_name(spec),
            "/TR",
            self.command_line(spec),
            *self._trigger_args(spec),
            "/F",
        ]
        proc = self._run(argv)
        ok = proc.returncode == 0
        detail = "" if ok else "schtasks /Create 失败：%s%s" % (
            (proc.stderr or "").strip(),
            ("；手动命令：" + subprocess.list2cmdline(argv)) if not self.dry_run else "",
        )
        if self.dry_run:
            detail = "dry-run：未执行 schtasks"
        return self._result(ACTION_REGISTER, spec, ok=ok, detail=detail, loaded=ok)

    def unregister(self, spec: ServiceSpec) -> OperationResult:
        spec.validate(need_command=False)
        self._check_platform(spec)
        self._begin()
        proc = self._run(
            ["schtasks", "/Delete", "/TN", self.task_name(spec), "/F"]
        )
        ok = proc.returncode == 0 or self.dry_run
        detail = "" if ok else "schtasks /Delete 失败：%s" % (
            proc.stderr or proc.stdout
        ).strip()
        return self._result(
            ACTION_UNREGISTER, spec, ok=ok, detail=detail, loaded=False
        )

    def start(self, spec: ServiceSpec) -> OperationResult:
        spec.validate(need_command=False)
        self._check_platform(spec)
        self._begin()
        proc = self._run(["schtasks", "/Run", "/TN", self.task_name(spec)])
        ok = proc.returncode == 0 or self.dry_run
        return self._result(
            ACTION_START,
            spec,
            ok=ok,
            detail="" if ok else "schtasks /Run 失败：%s"
            % (proc.stderr or proc.stdout).strip(),
            running=ok,
        )

    def stop(self, spec: ServiceSpec) -> OperationResult:
        spec.validate(need_command=False)
        self._check_platform(spec)
        self._begin()
        proc = self._run(["schtasks", "/End", "/TN", self.task_name(spec)])
        ok = proc.returncode == 0 or self.dry_run
        return self._result(
            ACTION_STOP,
            spec,
            ok=ok,
            detail="" if ok else "schtasks /End 失败：%s"
            % (proc.stderr or proc.stdout).strip(),
            running=False if ok else None,
        )

    def kickstart(self, spec: ServiceSpec) -> OperationResult:
        spec.validate(need_command=False)
        self._check_platform(spec)
        self._begin()
        self._run(["schtasks", "/End", "/TN", self.task_name(spec)])
        proc = self._run(["schtasks", "/Run", "/TN", self.task_name(spec)])
        ok = proc.returncode == 0 or self.dry_run
        return self._result(
            ACTION_KICKSTART,
            spec,
            ok=ok,
            detail="" if ok else "schtasks /Run 失败：%s"
            % (proc.stderr or proc.stdout).strip(),
            running=ok,
        )

    def status(self, spec: ServiceSpec) -> OperationResult:
        spec.validate(need_command=False)
        self._check_platform(spec)
        self._begin()
        if self.dry_run:
            self._run(
                ["schtasks", "/Query", "/TN", self.task_name(spec), "/FO", "LIST", "/V"]
            )
            return self._result(
                ACTION_STATUS, spec, detail="dry-run：未执行 schtasks", state="unknown"
            )
        proc = self._run(
            ["schtasks", "/Query", "/TN", self.task_name(spec), "/FO", "LIST", "/V"]
        )
        if proc.returncode != 0:
            return self._result(
                ACTION_STATUS, spec, state="not-found", loaded=False, running=False
            )
        text = proc.stdout or ""
        status_match = re.search(r"(?mi)^\s*(?:Status|状态)\s*:\s*(.+?)\s*$", text)
        state = status_match.group(1) if status_match else "unknown"
        running = state.lower() in ("running", "正在运行")
        return self._result(
            ACTION_STATUS,
            spec,
            state=state,
            loaded=True,
            running=running,
        )


# ---------------------------------------------------------------- 平台选择

def resolve_backend(
    system: str | None = None,
    *,
    home: str | os.PathLike[str] | None = None,
    runner: Runner | None = None,
    dry_run: bool = False,
    uid: int | None = None,
) -> ServiceBackend:
    """按平台返回后端；未知平台抛 :class:`UnsupportedPlatform`。"""
    system = system or _platform.system()
    if system == "Darwin":
        return LaunchdBackend(
            home=home, runner=runner, dry_run=dry_run, system=system, uid=uid
        )
    if system == "Linux":
        return SystemdBackend(
            home=home, runner=runner, dry_run=dry_run, system=system, uid=uid
        )
    if system == "Windows":
        return WindowsBackend(
            home=home, runner=runner, dry_run=dry_run, system=system, uid=uid
        )
    raise UnsupportedPlatform(
        "不支持的服务管理平台：%s（支持 macOS launchd / Linux systemd --user / "
        "Windows 计划任务）" % (system,)
    )


# ---------------------------------------------------------------- 服务目录

def _path_env(home: Path) -> str:
    return ":".join(
        [
            "/opt/homebrew/bin",
            "/usr/local/bin",
            str(home / ".bun" / "bin"),
            "/usr/bin",
            "/bin",
        ]
    )


def _require_chat_id(chat_id: str | None) -> str:
    if chat_id is None or not re.fullmatch(r"-?\d+", str(chat_id)):
        raise SpecError("self-initiate 需要数字 --chat-id")
    return str(chat_id)


def service_spec(
    name: str,
    *,
    repo: str | os.PathLike[str],
    python_bin: str | None = None,
    bot: str | None = None,
    chat_id: str | None = None,
    home: str | os.PathLike[str] | None = None,
) -> ServiceSpec:
    """内置服务目录：安装器与 PowerShell 只报服务名，命令在这里统一构造。"""
    repo_path = Path(repo).expanduser().resolve()
    python_bin = python_bin or sys.executable
    home_path = Path(home).expanduser() if home else Path.home()
    log_dir = tempfile.gettempdir()
    base_env = (
        ("PATH", _path_env(home_path)),
        # 中文 Windows 的计划任务控制台默认 cp936；定时脚本写 emoji/中文会 UnicodeEncodeError。
        ("PYTHONUTF8", "1"),
    )

    if name == "autostart":
        return ServiceSpec(
            name=name,
            command=("/bin/bash", str(repo_path / "restart-bots.sh")),
            description="claude-tgbot 登录自启（restart-bots.sh）",
            working_dir=str(repo_path),
            env=base_env + (("TMUX_TMPDIR", "/tmp"),),
            run_at_load=True,
            log_dir=log_dir,
            platforms=("Darwin", "Linux"),
        )

    if name == "dispatchers":
        return ServiceSpec(
            name=name,
            command=(
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-WindowStyle",
                "Hidden",
                "-File",
                str(repo_path / "windows" / "start-bots.ps1"),
            ),
            description="claude-tgbot dispatcher 登录自启",
            working_dir=str(repo_path),
            run_at_load=True,
            platforms=("Windows",),
        )

    if name == "provider-web":
        return ServiceSpec(
            name=name,
            command=(python_bin, "-m", "moments.web"),
            description="claude-tgbot 管理台 8767",
            working_dir=str(repo_path),
            env=base_env
            + (
                ("MOMENTS_WEB_MODE", "provider"),
                ("MOMENTS_WEB_PORT", "8767"),
            ),
            run_at_load=True,
            keep_alive=True,
            log_dir=log_dir,
        )

    if name == "provider-proxy":
        return ServiceSpec(
            name=name,
            command=(python_bin, "provider_proxy.py"),
            description="claude-tgbot 供应商代理 8770",
            working_dir=str(repo_path),
            env=base_env,
            run_at_load=True,
            keep_alive=True,
            log_dir=log_dir,
        )

    if name == "relations-tick":
        return ServiceSpec(
            name=name,
            command=(python_bin, "relations_tick.py"),
            description="claude-tgbot 关系数值 tick",
            working_dir=str(repo_path),
            env=base_env,
            interval_seconds=900,
            log_dir=log_dir,
        )

    if name == "memory-compactor":
        return ServiceSpec(
            name=name,
            command=(python_bin, "memory/memory_compactor.py"),
            description="claude-tgbot 长期记忆压缩",
            working_dir=str(repo_path),
            env=base_env,
            calendar=CalendarSchedule(hour=4, minute=0, weekday=0),
            log_dir=log_dir,
        )

    if name == "cleanup":
        return ServiceSpec(
            name=name,
            command=(python_bin, "cleanup.py"),
            description="claude-tgbot 每周清理",
            working_dir=str(repo_path),
            env=base_env,
            calendar=CalendarSchedule(hour=3, minute=0, weekday=0),
            log_dir=log_dir,
        )

    if name == "daily-wildcard":
        return ServiceSpec(
            name=name,
            command=(python_bin, "daily-wildcard.py"),
            description="claude-tgbot 每日签文",
            working_dir=str(repo_path),
            env=base_env,
            calendar=CalendarSchedule(hour=0, minute=15),
            log_dir=log_dir,
        )

    if name == "jiwen-tick":
        return ServiceSpec(
            name=name,
            command=(python_bin, "jiwen/tick.py"),
            description="claude-tgbot 情绪/关系 tick",
            working_dir=str(repo_path),
            env=base_env,
            interval_seconds=300,
            log_dir=log_dir,
            platforms=("Darwin", "Linux", "Windows"),
        )

    if name.startswith("self-initiate-") or name == "self-initiate":
        bot = bot or (
            name[len("self-initiate-"):]
            if name.startswith("self-initiate-")
            else None
        )
        if not bot or not _NAME_RE.match(bot):
            raise SpecError("self-initiate 需要合法 bot id（不能含空白/路径分隔符/NUL）")
        chat = _require_chat_id(chat_id)
        return ServiceSpec(
            name="self-initiate-" + bot,
            command=(python_bin, "scripts/self_initiate.py", bot, chat),
            description="claude-tgbot 主动消息 %s" % bot,
            working_dir=str(repo_path),
            env=base_env,
            interval_seconds=600,
            log_dir=log_dir,
        )

    raise SpecError(
        "未知服务 %r（可用：autostart、dispatchers、provider-web、provider-proxy、"
        "relations-tick、memory-compactor、cleanup、daily-wildcard、jiwen-tick、"
        "self-initiate-<bot>）" % (name,)
    )


def run_action(action: str, spec: ServiceSpec, backend: ServiceBackend) -> OperationResult:
    if action not in ACTIONS:
        raise SpecError("未知操作：%s" % (action,))
    return getattr(backend, action)(spec)


# ---------------------------------------------------------------- CLI

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="platform_services.py",
        description="跨平台用户级服务注册/卸载/启停（launchd / systemd --user / schtasks）",
    )
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument("service", nargs="?", help="内置服务名（如 autostart、provider-web）")
    parser.add_argument("--repo", default=".", help="仓库根目录（默认当前目录）")
    parser.add_argument(
        "--python-bin", default=sys.executable, help="服务里使用的 Python 解释器"
    )
    parser.add_argument("--bot", help="bot id（self-initiate 用）")
    parser.add_argument("--chat-id", help="self-initiate 的数字 chat id")
    parser.add_argument("--home", help="覆盖用户主目录（测试/运维接缝）")
    parser.add_argument(
        "--raw-label",
        help="按完整平台标签操作（只接受公开版前缀；用于清理历史任务）",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="只打印计划命令，不写文件、不执行命令"
    )
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        backend = resolve_backend(home=args.home, dry_run=args.dry_run)
        if args.raw_label:
            if not args.service:
                # 清理历史任务时没有内置服务名，给一个占位名。
                spec = ServiceSpec(name="", raw_label=args.raw_label)
            else:
                spec = ServiceSpec(
                    name=args.service, raw_label=args.raw_label
                )
        else:
            if not args.service:
                parser.error("需要服务名，或使用 --raw-label")
            spec = service_spec(
                args.service,
                repo=args.repo,
                python_bin=args.python_bin,
                bot=args.bot,
                chat_id=args.chat_id,
                home=args.home,
            )
        result = run_action(args.action, spec, backend)
    except UnsupportedPlatform as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 2
    except SpecError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 2
    except BackendUnavailable as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 1
    except OSError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False))
    else:
        print(
            "%s %s [%s] %s"
            % (
                result.action,
                result.service or "raw",
                result.platform,
                "ok" if result.ok else "failed",
            )
        )
        if result.state is not None:
            print("state: %s" % result.state)
        if result.pid is not None:
            print("pid: %d" % result.pid)
        if result.detail:
            print(result.detail, file=sys.stdout if result.ok else sys.stderr)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
