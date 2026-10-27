# -*- coding: utf-8 -*-
"""`configs/<bot>.yml` 顶层 `enabled` 的停用开关 —— 判定层。

判定层只碰 tmp_path 下的假 configs（`HUB_CONFIGS_DIR` / `config_loader.CONFIGS_DIR`
两个接缝），绝不读仓库真 configs/。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import bots_registry
import config_loader

REPO_ROOT = Path(bots_registry.__file__).resolve().parent


@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
    d = tmp_path / "configs"
    d.mkdir()
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(d))
    # 旧 .disabled 标记也走临时目录，否则会读到机主 ~/.claude/dispatcher/.disabled
    monkeypatch.setenv("DIRECTOR_DISABLED_DIR", str(tmp_path / ".disabled"))
    monkeypatch.setattr(config_loader, "CONFIGS_DIR", str(d))
    for k in list(os.environ):
        if k.startswith("DISPATCHER_PORT_"):
            monkeypatch.delenv(k, raising=False)
    return d


def write_bot(d, bot_id, **fields):
    lines = [f"id: {bot_id}", "display_name: 某个 bot"]
    lines += [f"{k}: {v}" for k, v in fields.items()]
    (d / f"{bot_id}.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")


# ------------------------------------------------------------ 判定（缺省 / 显式）

def test_没写enabled字段的存量bot照常启用(cfgdir):
    """默认值是"启用"，不然升级到这版的机器上全部 bot 一夜之间集体停摆。"""
    write_bot(cfgdir, "alpha")
    assert [b["id"] for b in bots_registry.bots()] == ["alpha"]
    assert bots_registry.disabled_ids() == set()


def test_显式false的bot不进注册表(cfgdir):
    write_bot(cfgdir, "alpha")
    write_bot(cfgdir, "beta", enabled="false")
    assert [b["id"] for b in bots_registry.bots()] == ["alpha"]
    assert bots_registry.disabled_ids() == {"beta"}


@pytest.mark.parametrize("raw", ["false", "no", "0", "off", '""'])
def test_各种写法的假值都算停用(cfgdir, raw):
    """手写 `enabled: 0` 的人要的就是停 —— 按 `is False` 判会把它当启用。"""
    write_bot(cfgdir, "beta", enabled=raw)
    assert bots_registry.bots() == []


@pytest.mark.parametrize("raw", ["true", "yes", "1"])
def test_真值照常启用(cfgdir, raw):
    write_bot(cfgdir, "beta", enabled=raw)
    assert [b["id"] for b in bots_registry.bots()] == ["beta"]


def test_include_disabled能拿回停用的bot(cfgdir):
    write_bot(cfgdir, "beta", enabled="false")
    got = bots_registry.bots(include_disabled=True)
    assert [b["id"] for b in got] == ["beta"]
    # 行的字段集是 §6 契约，绝不因为这个开关多长一个键出来
    assert set(got[0]) == {"id", "display_name", "port", "namespace_uuid", "port_derived"}


def test_停用一个bot不会让别人的派生端口漂移(cfgdir):
    """端口按全表解析：停用的 bot 仍占着号，否则重新启用时端口就换人了。"""
    write_bot(cfgdir, "aaa")
    write_bot(cfgdir, "bbb")
    before = bots_registry.ports()
    write_bot(cfgdir, "aaa", enabled="false")
    after = bots_registry.ports()
    assert after == before, "停用后端口表变了：%s → %s" % (before, after)


def test_停用的bot仍查得到端口与命名空间(cfgdir):
    """通讯录语义：查不到端口 = 管理台把它显示成 unknown 而不是 offline。"""
    write_bot(cfgdir, "beta", enabled="false", dispatcher_port=17888,
              namespace_uuid="22222222-2222-2222-2222-222222222222")
    assert bots_registry.ports()["beta"] == 17888
    assert bots_registry.namespace_for("beta").endswith("222222222222")


def test_CLI不吐停用的bot(cfgdir):
    """重启脚本 / Windows 启动脚本全按 CLI 拉起 —— 这一条就是"停了不被拉回来"。"""
    write_bot(cfgdir, "alpha")
    write_bot(cfgdir, "beta", enabled="false")
    env = dict(os.environ, HUB_CONFIGS_DIR=str(cfgdir))
    p = subprocess.run([sys.executable, "-m", "bots_registry", "--format=sh"],
                       cwd=str(REPO_ROOT), env=env, capture_output=True,
                       text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    assert [ln.split("\t")[0] for ln in p.stdout.strip().split("\n")] == ["alpha"]
    js = subprocess.run([sys.executable, "-m", "bots_registry", "--format=json"],
                        cwd=str(REPO_ROOT), env=env, capture_output=True,
                        text=True, timeout=60)
    assert [b["id"] for b in json.loads(js.stdout)] == ["alpha"]


def test_全部bot被停用时CLI退出码1(cfgdir):
    """空名单 = 1：启动脚本据此报错，而不是静默起 0 个 bot。"""
    write_bot(cfgdir, "beta", enabled="false")
    p = subprocess.run([sys.executable, "-m", "bots_registry", "--format=sh"],
                       cwd=str(REPO_ROOT), env=dict(os.environ, HUB_CONFIGS_DIR=str(cfgdir)),
                       capture_output=True, text=True, timeout=60)
    assert (p.returncode, p.stdout.strip()) == (1, "")


# ------------------------------------------------------------ config_loader 同口径

def test_list_enabled_bots跳过停用的(cfgdir):
    write_bot(cfgdir, "alpha")
    write_bot(cfgdir, "beta", enabled="false")
    assert [c["_bot_id"] for c in config_loader.list_enabled_bots()] == ["alpha"]


def test_list_enabled_bots的include_disabled留住停用的(cfgdir):
    """管理台是唯一能把它启用回来的地方，从列表里消失就再也点不回来了。"""
    write_bot(cfgdir, "alpha")
    write_bot(cfgdir, "beta", enabled="false")
    got = [c["_bot_id"] for c in config_loader.list_enabled_bots(include_disabled=True)]
    assert got == ["alpha", "beta"]


# ------------------------------------------------------------ 重启脚本得杀得掉停用的 bot

def test_重启脚本先停全部dispatcher再按注册表拉起():
    """停用的 bot 已经不在注册表里，循环轮不到它 —— 只在循环内逐个 kill 等于没停。

    源码级断言（同 tests/acceptance/hub2/test_source_contracts.py 的口径）：
    真跑一遍会把机主正在用的 bot 全停掉。
    """
    txt = (REPO_ROOT / "restart-bots.example.sh").read_text(encoding="utf-8")
    sweep = txt.find("stop_all_dispatchers")
    loop = txt.find("while IFS=")
    read_registry = txt.find("bots_registry --format=sh")
    assert sweep != -1, "重启脚本不停旧 dispatcher，停用的 bot 会一直活着"
    assert "tmux kill-session" not in txt and "launchctl " not in txt, \
        "重启脚本不应再依赖 tmux/launchd（跨平台进程模型）"
    assert read_registry < sweep < loop, \
        "停旧进程必须排在『读注册表成功之后、拉起之前』（读不出名单时不该白停一遍）"
