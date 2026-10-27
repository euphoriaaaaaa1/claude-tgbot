# -*- coding: utf-8 -*-
"""唯一注册表：旧 ``.disabled/<bot>`` 只读兼容、CBL 兼容面、停用后脚本不拉起。

只碰 tmp_path：``HUB_CONFIGS_DIR`` 与 ``DIRECTOR_DISABLED_DIR`` 两个接缝都指向临时目录，
绝不读仓库真 configs/ 或机主 ``~/.claude/dispatcher/.disabled``。
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import bots_registry
import config_loader

REPO_ROOT = Path(bots_registry.__file__).resolve().parent


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "configs"
    cfg.mkdir()
    dis = tmp_path / ".disabled"
    dis.mkdir()
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(cfg))
    monkeypatch.setenv("DIRECTOR_DISABLED_DIR", str(dis))
    monkeypatch.setattr(config_loader, "CONFIGS_DIR", str(cfg))
    for k in list(os.environ):
        if k.startswith("DISPATCHER_PORT_"):
            monkeypatch.delenv(k, raising=False)
    return cfg, dis


def write_bot(d, bot_id, **fields):
    lines = [f"id: {bot_id}", "display_name: 某个 bot"]
    lines += [f"{k}: {v}" for k, v in fields.items()]
    (d / f"{bot_id}.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_cli(env, *args):
    cfg, dis = env
    e = dict(os.environ, HUB_CONFIGS_DIR=str(cfg), DIRECTOR_DISABLED_DIR=str(dis))
    return subprocess.run([sys.executable, "-m", "bots_registry", *args],
                          cwd=str(REPO_ROOT), env=e, capture_output=True,
                          text=True, timeout=60)


# ------------------------------------------------------------ 旧标记：只读兼容

def test_旧标记命中即停用且include_disabled能拿回(env):
    cfg, dis = env
    write_bot(cfg, "alpha")
    write_bot(cfg, "beta", dispatcher_port=17888)
    (dis / "beta").write_text("stopped\n", encoding="utf-8")

    assert [b["id"] for b in bots_registry.bots()] == ["alpha"]
    assert bots_registry.disabled_ids() == {"beta"}
    got = bots_registry.bots(include_disabled=True)
    assert [b["id"] for b in got] == ["alpha", "beta"]
    # 行字段集仍是契约，不因旧标记变形
    assert set(got[1]) == {"id", "display_name", "port", "namespace_uuid", "port_derived"}


def test_yml与旧标记任一成立即停用(env):
    cfg, dis = env
    write_bot(cfg, "alpha", enabled="false")
    write_bot(cfg, "beta")
    (dis / "beta").write_text("", encoding="utf-8")
    assert bots_registry.bots() == []
    assert bots_registry.disabled_ids() == {"alpha", "beta"}


def test_读取不创建也不改写旧标记(env):
    cfg, dis = env
    write_bot(cfg, "beta")
    marker = dis / "beta"
    marker.write_text("stopped\n", encoding="utf-8")
    before = (marker.read_text(encoding="utf-8"), marker.stat().st_mtime_ns)

    bots_registry.bots()
    bots_registry.disabled_ids()
    bots_registry.ports()
    bots_registry.reload()
    p = run_cli(env, "--format=json")
    assert p.returncode == 1

    assert marker.read_text(encoding="utf-8") == before[0]
    assert marker.stat().st_mtime_ns == before[1]
    assert [x.name for x in dis.iterdir()] == ["beta"]


def test_enabled_false不写旧标记(env):
    """写侧只有 yml：注册表读取/CLI 都不许落第二张开关表。"""
    cfg, dis = env
    write_bot(cfg, "beta", enabled="false")
    bots_registry.bots()
    run_cli(env, "--format=sh")
    assert list(dis.iterdir()) == []


def test_CLI旧标记停用后不吐且退出码1(env):
    """启动脚本按 CLI 拉起 —— 旧标记必须在这里就被挡住。"""
    cfg, dis = env
    write_bot(cfg, "alpha")
    write_bot(cfg, "beta")
    (dis / "beta").write_text("", encoding="utf-8")
    p = run_cli(env, "--format=sh")
    assert p.returncode == 0, p.stderr
    assert [ln.split("\t")[0] for ln in p.stdout.strip().split("\n")] == ["alpha"]

    (dis / "alpha").write_text("", encoding="utf-8")
    p = run_cli(env, "--format=sh")
    assert (p.returncode, p.stdout.strip()) == (1, "")


def test_config_loader与注册表同口径认旧标记(env):
    cfg, dis = env
    write_bot(cfg, "alpha")
    write_bot(cfg, "beta")
    (dis / "beta").write_text("", encoding="utf-8")
    assert [c["_bot_id"] for c in config_loader.list_enabled_bots()] == ["alpha"]
    got = [c["_bot_id"] for c in config_loader.list_enabled_bots(include_disabled=True)]
    assert got == ["alpha", "beta"]


# ------------------------------------------------------------ 路径越界

@pytest.mark.parametrize("bad", ["", ".", "..", "../victim", "a/b", "a\\b", "\x00x"])
def test_越界bot_id不读旧标记(env, bad):
    cfg, dis = env
    # 诱饵：若实现把 ../victim 直接拼进目录就会命中它
    victim = dis.parent / "victim"
    victim.write_text("", encoding="utf-8")
    assert bots_registry.legacy_disabled(bad) is False
    assert bots_registry.safe_bot_id(bad) is False


def test_中文bot_id仍可读旧标记(env):
    """旧标记目录里出现过中文 bot 名；只拒绝路径语义，不拒绝字符集。"""
    cfg, dis = env
    write_bot(cfg, "测试角色甲")
    (dis / "测试角色甲").write_text("", encoding="utf-8")
    assert bots_registry.legacy_disabled("测试角色甲") is True
    assert bots_registry.bots() == []


# ------------------------------------------------------------ CBL 兼容面

def test_entries字段与CBL口径一致(env):
    cfg, _ = env
    write_bot(cfg, "alpha", dispatcher_port=17804,
              namespace_uuid="22222222-2222-2222-2222-222222222222",
              telegram_username="alpha_bot", bot_channel_path="/tmp/channels/alpha")
    (cfg / "alpha.yml").write_text(
        "\n".join([
            "id: alpha",
            "display_name: 某个 bot",
            "dispatcher_port: 17804",
            "namespace_uuid: 22222222-2222-2222-2222-222222222222",
            "telegram_username: alpha_bot",
            "bot_channel_path: /tmp/channels/alpha",
            "launcher: registry",
        ]) + "\n", encoding="utf-8")
    got = bots_registry.entries()[0]
    assert set(got) == {"bot_id", "id", "dispatcher_port", "bot_channel_path",
                        "namespace_uuid", "telegram_username", "launcher"}
    assert got["bot_id"] == "alpha"
    assert got["dispatcher_port"] == "17804"          # 字符串是 CBL 原口径
    assert got["namespace_uuid"].endswith("222222222222")
    assert got["telegram_username"] == "alpha_bot"
    assert got["bot_channel_path"] == "/tmp/channels/alpha"
    assert got["launcher"] == "registry"


def test_停用的bot不出现在entries_但端口和命名空间照常(env):
    cfg, _ = env
    write_bot(cfg, "beta", enabled="false", dispatcher_port=17888,
              namespace_uuid="22222222-2222-2222-2222-222222222222")
    assert bots_registry.entries() == []
    assert bots_registry.ports()["beta"] == 17888
    assert bots_registry.namespace_for("beta").endswith("222222222222")
    assert bots_registry.namespace("beta") == bots_registry.namespace_for("beta")


def test_reload返回当前端口表(env):
    cfg, _ = env
    write_bot(cfg, "alpha", dispatcher_port=17804)
    assert bots_registry.reload() == bots_registry.ports() == {"alpha": 17804}


def test_username只认合规值(env):
    cfg, _ = env
    write_bot(cfg, "alpha", telegram_username="alpha_bot")
    write_bot(cfg, "beta", telegram_username="bad name")
    assert bots_registry.username("alpha") == "alpha_bot"
    assert bots_registry.username("beta") is None
    assert bots_registry.username("nosuch") is None
    assert bots_registry.username("../victim") is None


# ------------------------------------------------------------ 唯一模块

def test_旧注册表模块与import都不存在():
    """T04 的唯一名单：``bot_registry.py`` 不进入 v2。"""
    assert not (REPO_ROOT / "bot_registry.py").exists()
    hits = []
    for p in REPO_ROOT.rglob("*.py"):
        if any(part in (".git", "__pycache__") for part in p.parts):
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if re.search(r"^\s*(?:from|import)\s+bot_registry\b", text, re.M):
            hits.append(str(p.relative_to(REPO_ROOT)))
    assert hits == []
