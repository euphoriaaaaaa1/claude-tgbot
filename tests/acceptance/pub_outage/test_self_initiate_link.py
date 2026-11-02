# -*- coding: utf-8 -*-
"""pub-outage 主动开口脚本在线路不通时的行为（行为清单 #9 #10）。

只依据 pub-outage-接口.md 6.4 / 6.4a / 6.5：self_initiate.py 只经命令行黑盒调用；
life-context 由 sitecustomize 里的 subprocess.run 替身返回（同仓库既有验收的手法），并记一个"被调用过"标记文件。
HOME、线路状态目录全在 tmp_path 里，绝不碰真实 ~/.claude，绝不连任何真实端口。
"""
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SELF_INITIATE = REPO / "scripts" / "self_initiate.py"
BOT = "outage_bot"
CHAT = "10001"
T0 = 1_800_000_000
STATE_REL = Path(".claude") / "dispatcher" / ".self-initiate-state"
DEFAULT_LINK_REL = Path(".claude") / "dispatcher" / ".link-state"
SKIP_OUT = '{"action": "SKIP", "reason": "asleep", "detail": "", "retry_after_s": null}'

SITECUSTOMIZE = """import os, subprocess, sys
sys.path.insert(0, os.environ['PO_REPO'])
import db
db.DB_PATH = os.environ['PO_DB']
db.init()
_real = subprocess.run
def _run(cmd, *a, **k):
    s = ' '.join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
    if 'life-context.py' in s:
        with open(os.environ['PO_LC_MARK'], 'a') as f:
            f.write('called\\n')
        return subprocess.CompletedProcess(cmd, 0, os.environ['PO_LIFE_OUT'], '')
    return _real(cmd, *a, **k)
subprocess.run = _run
"""


def _dead_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Env:
    def __init__(self, root):
        self.root = root
        self.home = root / "home"
        self.configs = root / "configs"
        self.channels = root / "channels"
        self.link_dir = root / "linkstate"
        self.lc_mark = root / "life-context-called"
        self.state = self.home / STATE_REL
        for p in (self.home, self.configs, self.channels / BOT / "inbox", self.state):
            p.mkdir(parents=True, exist_ok=True)
        (self.configs / "_global.yml").write_text("jiwen:\n  tick_interval_min: 5\n", encoding="utf-8")
        (self.configs / ("%s.yml" % BOT)).write_text(
            "id: %s\nenabled: true\nchat_id: \"%s\"\nbot_channel_path: %s\n" % (BOT, CHAT, self.channels / BOT),
            encoding="utf-8")
        harness = root / "harness"
        harness.mkdir()
        (harness / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
        self.harness = harness
        # 冷静期放行：间隔 0、上次很久以前
        (self.state / ("%s-%s.interval" % (BOT, CHAT))).write_text("0", encoding="utf-8")
        (self.state / ("%s-%s.last" % (BOT, CHAT))).write_text(str(T0 - 1000), encoding="utf-8")

    def markers(self):
        out = {}
        for ext in ("last", "interval", "last-sent"):
            p = self.state / ("%s-%s.%s" % (BOT, CHAT, ext))
            out[ext] = p.read_bytes() if p.exists() else None
        return out

    def inbox(self):
        p = self.channels / BOT / "inbox"
        return sorted(x.name for x in p.iterdir()) if p.is_dir() else []

    def run(self, link_dir_env=True, life_out='{"action": "FALLBACK"}'):
        env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
        env.update({
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "CLAUDEBOT_CONFIG_DIR": str(self.configs), "HUB_CONFIGS_DIR": str(self.configs),
            "CLAUDEBOT_GLOBAL_CFG": str(self.configs / "_global.yml"),
            "CLAUDEBOT_SECRETS_FILE": str(self.configs / "_secrets.yml"),
            "CLAUDEBOT_PROVIDER_PATH": str(self.root / "providers.json"),
            "CLAUDEBOT_CHANNELS_ROOT": str(self.channels),
            "CLAUDEBOTLIFE_TEST": "1", "CLAUDEBOTLIFE_TEST_ROOT": str(self.root),
            "SELF_INITIATE_NOW": str(T0), "DISABLED_CHECK": "",
            "DISPATCHER_PORT_%s" % BOT.upper(): str(_dead_port()),
            "PYTHONPATH": str(self.harness) + os.pathsep + str(REPO),
            "PO_REPO": str(REPO), "PO_DB": str(self.root / "test.db"),
            "PO_LIFE_OUT": life_out, "PO_LC_MARK": str(self.lc_mark),
        })
        env.pop("LINK_STATE_DIR", None)
        if link_dir_env:
            env["LINK_STATE_DIR"] = str(self.link_dir)
        return subprocess.run([sys.executable, str(SELF_INITIATE), BOT, CHAT],
                              cwd=str(REPO), env=env, capture_output=True, text=True, timeout=60)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def _write_link(env, link_dir_env, content):
    d = env.link_dir if link_dir_env else env.home / DEFAULT_LINK_REL
    d.mkdir(parents=True, exist_ok=True)
    (d / BOT).write_text(content, encoding="utf-8")


@pytest.mark.parametrize("link_dir_env", [True, False], ids=["设LINK_STATE_DIR", "不设_走HOME缺省位置"])
def test_9_线路不通_跳过主动开口_退出码0_标记文件不变_收件箱无新件_不调life_context(env, link_dir_env):
    assert SELF_INITIATE.is_file(), "实现缺失：scripts/self_initiate.py"
    _write_link(env, link_dir_env, "down_since=%d checked_at=%d\n" % (T0 - 100, T0 - 30))
    before = env.markers()
    assert before["last"] == str(T0 - 1000).encode() and before["last-sent"] is None
    r = env.run(link_dir_env=link_dir_env)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "skip: reason=link_down" in r.stderr, (r.stdout, r.stderr)
    assert env.markers() == before
    assert env.inbox() == []
    assert not env.lc_mark.exists(), "线路不通时不应调用 life-context"


@pytest.mark.parametrize("case", ["过期121秒", "超前121秒", "文件不存在", "内容乱码"])
def test_10_线路状态无效或过期_视为通_照原逻辑走(env, case):
    assert SELF_INITIATE.is_file(), "实现缺失：scripts/self_initiate.py"
    if case == "过期121秒":
        _write_link(env, True, "down_since=%d checked_at=%d\n" % (T0 - 200, T0 - 121))
    elif case == "超前121秒":
        _write_link(env, True, "down_since=%d checked_at=%d\n" % (T0, T0 + 121))
    elif case == "内容乱码":
        _write_link(env, True, "\x00\x01 乱码 not a state file �")
    r = env.run(link_dir_env=True, life_out=SKIP_OUT)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "reason=link_down" not in r.stderr, r.stderr
    assert "skip per life-context:" in (r.stdout + r.stderr), (r.stdout, r.stderr)
    assert env.lc_mark.exists(), "视为通时应照原逻辑调用 life-context"
    assert env.inbox() == []
