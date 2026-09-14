"""黑盒验收（desync）公共夹具：只依赖 INTERFACE-desync.md 的 env / 模块属性注入点。

运行器职责（INTERFACE §11.4）：
- CLAUDEBOTLIFE_TEST=1 + CLAUDEBOTLIFE_TEST_ROOT=<tmp_path>；HOME 与所有路径 env 都在其下
- 纵深隔离（当前代码还没有 §11 注入点也必须成立）：DIRECTOR_NO_SPAWN=1；
  PATH 前置拒绝桩（tmux/launchctl/pkill/claude/schtasks → stderr "test_mode: real <名> called"，exit 97）；
  假服务只用 port 0；临时注册表每个 bot 显式写一个非生产端口（绝不落到默认 17801-17804）；
  TELEGRAM_DISPATCHER_URL/DISPATCHER_URL 指向本机无人监听的随机端口。
- 绝不使用 ~、$USER、/Users/wsxwj、~/.claude/dispatcher；绝不 launchctl；绝不连 17801-17804。
"""
import datetime
import importlib
import json
import os
import socket
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

# 2026-09-14 15:00 本地时间：白天，避开 NIGHT_SKIP=(1,8)
NOW = datetime.datetime(2026, 9, 14, 15, 0, 0).timestamp()
FORBIDDEN_PORTS = {17801, 17802, 17803, 17804, 7897, 7788}
REFUSE_CMDS = ("tmux", "launchctl", "pkill", "claude", "schtasks")


def free_port():
    """本机随机空闲端口（永不返回生产/代理端口）。"""
    while True:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        p = s.getsockname()[1]
        s.close()
        if p not in FORBIDDEN_PORTS:
            return p


def human(ts, text="嗯"):
    return {"ts": ts, "speaker": "主人", "text": text, "is_bot": False}


def bot_msg(ts, who="bot1", text="哈哈"):
    return {"ts": ts, "speaker": who, "text": text, "is_bot": True}


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """隔离环境；返回各目录路径的 dict（root/home/cfg/ch/st/md/gt/mk/stub/calls）。"""
    root = str(tmp_path)
    dirs = {"root": root}
    for k in ("home", "cfg", "ch", "st", "md", "gt", "mk", "stub"):
        p = tmp_path / k
        p.mkdir()
        dirs[k] = str(p)
    monkeypatch.setenv("CLAUDEBOTLIFE_TEST", "1")
    monkeypatch.setenv("CLAUDEBOTLIFE_TEST_ROOT", root)
    monkeypatch.setenv("HOME", dirs["home"])
    monkeypatch.setenv("HUB_CONFIGS_DIR", dirs["cfg"])
    monkeypatch.setenv("CLAUDEBOT_CONFIG_DIR", dirs["cfg"])
    monkeypatch.setenv("DIRECTOR_CHANNELS_ROOT", dirs["ch"])
    monkeypatch.setenv("DIRECTOR_STATE_DIR", dirs["st"])
    monkeypatch.setenv("DIRECTOR_MODE_DIR", dirs["md"])
    monkeypatch.setenv("DIRECTOR_GT_DIR", dirs["gt"])
    monkeypatch.setenv("DIRECTOR_MARKER_DIR", dirs["mk"])
    monkeypatch.setenv("DIRECTOR_NO_SPAWN", "1")
    dead = f"http://127.0.0.1:{free_port()}"  # 无人监听：任何漏网请求只会连接失败
    monkeypatch.setenv("TELEGRAM_DISPATCHER_URL", dead)
    monkeypatch.setenv("DISPATCHER_URL", dead)
    for name in REFUSE_CMDS:
        stub = tmp_path / "stub" / name
        stub.write_text('#!/bin/sh\necho "$0 $@" >> "$CLAUDEBOTLIFE_TEST_ROOT/calls.log"\n'
                        f'echo "test_mode: real {name} called" >&2\nexit 97\n')
        stub.chmod(0o755)
    monkeypatch.setenv("PATH", dirs["stub"] + os.pathsep + os.environ.get("PATH", ""))
    dirs["calls"] = os.path.join(root, "calls.log")
    return dirs


def write_cfg(cfg_dir, bot_id, enabled=None, extra="", port=None):
    """写 configs/<bot>.yml；没显式给 dispatcher_port 时补一个随机非生产端口。返回端口。"""
    line = "" if enabled is None else f"enabled: {'true' if enabled else 'false'}\n"
    if "dispatcher_port" not in extra:
        port = port or free_port()
        extra += f"dispatcher_port: {port}\n"
    with open(os.path.join(cfg_dir, f"{bot_id}.yml"), "w", encoding="utf-8") as f:
        f.write(f"id: {bot_id}\n{line}{extra}")
    return port


@pytest.fixture
def registry(iso):
    import bots_registry
    return importlib.reload(bots_registry)


@pytest.fixture
def director(iso, registry):
    """在隔离 env 下重新加载 director，并清空限流字典 / 启用宽限状态。"""
    import director as d
    d = importlib.reload(d)
    # r7 §2.5 正式注入点：配额闸只经 d._quota_ok（() -> bool）注入，不再替换 _quota/check_quota；默认放行以观察其它闸
    d._quota_ok = lambda: True
    if isinstance(getattr(d, "_busy_log_at", None), dict):
        d._busy_log_at.clear()
    if hasattr(d, "_prev_stopped"):
        d._prev_stopped = None
    if isinstance(getattr(d, "_resumed_at", None), dict):
        d._resumed_at.clear()
    return d


def marker(iso, bot_dir_name, chat, ts):
    """写 <MARKER_DIR>/<bot>-<chat>.last-user，内容 int 秒（无换行也合法）。"""
    p = os.path.join(iso["mk"], f"{bot_dir_name}-{chat}.last-user")
    with open(p, "w") as f:
        f.write(str(ts))
    return p


def calls(iso):
    p = iso["calls"]
    return open(p).read() if os.path.exists(p) else ""


def switch_on(iso, chat_id):
    open(os.path.join(iso["md"], str(chat_id)), "w").close()


def gt_line(iso, chat_id, ts, text="hi", is_bot=False, username="u", mid=1):
    """追加一行群 transcript（§10.2 最小形状）；ts 为 epoch 秒。"""
    iso_ts = datetime.datetime.fromtimestamp(ts, datetime.timezone(datetime.timedelta(hours=8))).isoformat()
    row = {"ts": iso_ts, "chat_id": str(chat_id), "message_id": mid, "from_id": "5331715732",
           "from_username": username, "is_bot": is_bot, "text": text, "observed_by": "bot1"}
    with open(os.path.join(iso["gt"], f"{chat_id}.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def inbox(iso, bot):
    p = os.path.join(iso["ch"], bot, "inbox")
    return sorted(os.listdir(p)) if os.path.isdir(p) else []
