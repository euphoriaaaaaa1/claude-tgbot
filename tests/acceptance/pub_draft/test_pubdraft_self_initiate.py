# -*- coding: utf-8 -*-
"""pub-draft 积温/主动消息验收：B20/B21/B22（B22 另见报告里的既有测试原样运行记录）。

self_initiate.py 用子进程跑；life-context 由 sitecustomize 里的 subprocess.run 替身返回
（与仓库既有 t23 验收同一手法），绝不启动真实 life-context、绝不连真实服务。
积温状态文件按接口「四之二」：<jiwen.state_dir>/{bot_id}-{chat_id}.json。
"""
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import pd_helpers as H
from conftest import BOT, CHAT

NOW = datetime.datetime(2026, 9, 14, 15, 0, 0).timestamp()
T0 = int(NOW)
SELF_INITIATE = H.REPO / "scripts" / "self_initiate.py"
TICK = H.REPO / "jiwen" / "tick.py"

SITECUSTOMIZE = """import os, subprocess, sys
sys.path.insert(0, os.environ['PD_REPO'])
import db
db.DB_PATH = os.environ['PD_DB']
db.init()
_real = subprocess.run
def _run(cmd, *a, **k):
    s = ' '.join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
    if 'life-context.py' in s:
        return subprocess.CompletedProcess(cmd, 0, os.environ['PD_LIFE_OUT'], '')
    return _real(cmd, *a, **k)
subprocess.run = _run
"""


def _harness(pd_env):
    h = pd_env["root"] / "harness"
    h.mkdir(exist_ok=True)
    (h / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    return h


def _base_env(pd_env, now=T0, extra=None):
    from conftest import STATE_REL
    state = pd_env["home"] / STATE_REL
    state.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    for k in list(env):
        if k.lower().endswith("_proxy"):
            del env[k]
    env.update({
        "HOME": str(pd_env["home"]), "USERPROFILE": str(pd_env["home"]),
        "CLAUDEBOT_CONFIG_DIR": str(pd_env["configs"]),
        "HUB_CONFIGS_DIR": str(pd_env["configs"]),
        "CLAUDEBOT_GLOBAL_CFG": str(pd_env["configs"] / "_global.yml"),
        "CLAUDEBOT_PROVIDER_PATH": str(pd_env["root"] / "providers.json"),
        "CLAUDEBOT_CHANNELS_ROOT": str(pd_env["channels"]),
        "CLAUDEBOTLIFE_TEST": "1", "CLAUDEBOTLIFE_TEST_ROOT": str(pd_env["root"]),
        "SELF_INITIATE_NOW": str(int(now)),
        "DISABLED_CHECK": "",
        "DISPATCHER_PORT_%s" % BOT.upper(): H.dead_url().rsplit(":", 1)[1],
        "PYTHONPATH": str(_harness(pd_env)) + os.pathsep + str(H.REPO),
        "PD_REPO": str(H.REPO), "PD_DB": str(pd_env["root"] / "test.db"),
        "PD_LIFE_OUT": '{"action": "FALLBACK"}',
    })
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def _markers(pd_env, interval="0", last=None):
    from conftest import STATE_REL
    state = pd_env["home"] / STATE_REL
    state.mkdir(parents=True, exist_ok=True)
    (state / ("%s-%s.interval" % (BOT, CHAT))).write_text(interval, encoding="utf-8")
    if last is not None:
        (state / ("%s-%s.last" % (BOT, CHAT))).write_text(str(last), encoding="utf-8")
    return state


def _run_self(pd_env, env):
    assert SELF_INITIATE.is_file(), "实现缺失：scripts/self_initiate.py"
    return subprocess.run([sys.executable, str(SELF_INITIATE), BOT, CHAT],
                          cwd=str(H.REPO), env=env, capture_output=True, text=True, timeout=60)


def _inbox(pd_env):
    p = pd_env["channels"] / BOT / "inbox"
    return sorted(x.name for x in p.glob("self-*.json")) if p.is_dir() else []


def test_B20a_生产成功_写收件箱与last_sent(pd_env):
    from conftest import STATE_REL
    from conftest import write_bot_cfg
    write_bot_cfg(pd_env)
    state = _markers(pd_env, interval="0", last=T0 - 1000)
    env = _base_env(pd_env)
    r = _run_self(pd_env, env)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert len(_inbox(pd_env)) == 1, _inbox(pd_env)
    sent = state / ("%s-%s.last-sent" % (BOT, CHAT))
    assert sent.is_file(), list(state.iterdir())
    content = sent.read_text(encoding="utf-8")
    assert content.strip() == str(T0)
    assert content.strip().isdigit() and content.strip().isascii()
    assert (state / ("%s-%s.last" % (BOT, CHAT))).read_text(encoding="utf-8").strip() == str(T0)


@pytest.mark.parametrize("case", ["life-context返回SKIP", "间隔闸拦住", "bot停用"])
def test_B20b_未送达_无收件箱无last_sent(pd_env, case):
    from conftest import STATE_REL, write_bot_cfg
    if case == "bot停用":
        write_bot_cfg(pd_env, enabled=False)
        state = _markers(pd_env, interval="0", last=T0 - 1000)
        env = _base_env(pd_env)
    elif case == "间隔闸拦住":
        write_bot_cfg(pd_env)
        state = _markers(pd_env, interval="999999999", last=T0 - 100)
        env = _base_env(pd_env)
    else:
        write_bot_cfg(pd_env)
        state = _markers(pd_env, interval="0", last=T0 - 1000)
        env = _base_env(pd_env, extra={"PD_LIFE_OUT": (
            '{"action": "SKIP", "reason": "asleep", "detail": "", "retry_after_s": null}')})
    r = _run_self(pd_env, env)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert _inbox(pd_env) == []
    assert not (state / ("%s-%s.last-sent" % (BOT, CHAT))).exists()
    if case == "bot停用":
        assert (state / ("%s-%s.last" % (BOT, CHAT))).read_text(encoding="utf-8").strip() == str(T0)


# ── B21：积温计分 ────────────────────────────────────────────────────────────

def _jiwen_setup(pd_env, last_user_msg_ts):
    from conftest import STATE_REL, write_bot_cfg
    channel = write_bot_cfg(pd_env)
    (channel / "access.json").write_text('{"allowFrom": ["%s"]}' % CHAT, encoding="utf-8")
    state_dir = pd_env["home"] / ".claude" / "dispatcher" / ".jiwen-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    H.dump_yaml({"jiwen": {
        "tick_interval_min": 5,
        "state_dir": str(state_dir),
        "rates": {"connection_per_min": 0},
        "delta_llm": {"base_url": H.dead_url(), "api_key": H.MODEL_KEY,
                      "model": "deepseek-test", "timeout": 1},
    }}, pd_env["configs"] / "_global.yml")
    state_file = state_dir / ("%s-%s.json" % (BOT, CHAT))
    state_file.write_text(json.dumps({
        "connection": 0.5, "valence": 0.0, "arousal": 0.0, "pride": 0.0, "energy": 1.0,
        "last_tick_ts": T0 - 300, "last_user_msg_ts": last_user_msg_ts,
    }, ensure_ascii=False), encoding="utf-8")
    markers = pd_env["home"] / STATE_REL
    markers.mkdir(parents=True, exist_ok=True)
    (markers / ("%s-%s.interval" % (BOT, CHAT))).write_text("0", encoding="utf-8")
    (markers / ("%s-%s.last" % (BOT, CHAT))).write_text(str(T0 - 1000), encoding="utf-8")
    return state_file


def _run_tick(pd_env, now):
    assert TICK.is_file(), "实现缺失：jiwen/tick.py"
    env = _base_env(pd_env, now=now)
    return subprocess.run([sys.executable, str(TICK)], cwd=str(H.REPO), env=env,
                          capture_output=True, text=True, timeout=60)


def _connection(pd_env, state_file):
    assert state_file.is_file(), "积温状态文件不存在：%s（目录：%s）" % (
        state_file, list(state_file.parent.iterdir()))
    return round(float(json.loads(state_file.read_text(encoding="utf-8"))["connection"]), 6)


SCENARIOS = {
    "a_送达扣一次": (str(T0), T0 - 100000, [(T0 + 300, 0.45), (T0 + 600, 0.45)]),
    "b_未回每5分钟积分": (str(T0), T0 - 100000, [(T0 + 14700, 0.44), (T0 + 15000, 0.43),
                                                (T0 + 15300, 0.42)]),
    "d_用户已回只扣送达": (str(T0), T0 + 100, [(T0 + 300, 0.45), (T0 + 600, 0.45)]),
    "c_无marker": (None, T0 - 100000, [(T0 + 300, 0.5), (T0 + 600, 0.5)]),
    "c_坏marker": ("12.3", T0 - 100000, [(T0 + 300, 0.5), (T0 + 600, 0.5)]),
    "c_未来超过60秒": (str(T0 + 3600), T0 - 100000, [(T0 + 300, 0.5), (T0 + 600, 0.5)]),
}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_B21a_b_c_d_送达与未回计分(pd_env, name):
    marker, last_user, steps = SCENARIOS[name]
    state_file = _jiwen_setup(pd_env, last_user)
    sent = pd_env["home"] / ".claude" / "dispatcher" / ".self-initiate-state" / \
        ("%s-%s.last-sent" % (BOT, CHAT))
    if marker is None:
        if sent.exists():
            sent.unlink()
    else:
        sent.write_text(marker, encoding="utf-8")
    for now, expected in steps:
        r = _run_tick(pd_env, now)
        assert r.returncode == 0, (name, now, r.returncode, r.stdout, r.stderr)
        assert _connection(pd_env, state_file) == expected, (name, now)
