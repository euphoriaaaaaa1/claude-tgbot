# -*- coding: utf-8 -*-
"""bots_registry 纯函数层单测：派生规则 / 兜底 / 最小空闲号 / CLI 两格式。

只碰 tmp_path 下的假 configs（``HUB_CONFIGS_DIR`` 接缝），绝不读仓库真 configs/。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import bots_registry

REPO_ROOT = Path(bots_registry.__file__).resolve().parent
CHENLULU_NS = "550e8400-e29b-41d4-a716-446655440001"


@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
    d = tmp_path / "configs"
    d.mkdir()
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(d))
    for k in list(os.environ):
        if k.startswith("DISPATCHER_PORT_"):
            monkeypatch.delenv(k, raising=False)
    return d


def write_bot(d, bot_id, **fields):
    lines = [f"id: {bot_id}", "display_name: 某个 bot"]
    lines += [f"{k}: {v}" for k, v in fields.items()]
    (d / f"{bot_id}.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_cli(d, *args):
    env = dict(os.environ, HUB_CONFIGS_DIR=str(d))
    return subprocess.run([sys.executable, "-m", "bots_registry", *args],
                          cwd=str(REPO_ROOT), env=env, capture_output=True,
                          text=True, timeout=60)


# ------------------------------------------------------------ 端口解析（F3）

def test_端口四档优先级(cfgdir, monkeypatch):
    write_bot(cfgdir, "alpha", dispatcher_port=17900)
    write_bot(cfgdir, "chenlulu")                  # legacy 兜底
    write_bot(cfgdir, "zeta")                      # 派生
    monkeypatch.setenv("DISPATCHER_PORT_ALPHA", "19999")
    got = {b["id"]: (b["port"], b["port_derived"]) for b in bots_registry.bots()}
    assert got["alpha"] == (19999, False)          # env 盖过 yml
    assert got["chenlulu"] == (17801, False)       # legacy 命中不算派生
    assert got["zeta"][1] is True


def test_派生取最小空闲号且不抢显式端口(cfgdir):
    write_bot(cfgdir, "bbb", dispatcher_port=17802)
    write_bot(cfgdir, "ccc")
    write_bot(cfgdir, "ddd")
    got = bots_registry.ports()
    assert got == {"bbb": 17802, "ccc": 17801, "ddd": 17803}


def test_既有bot端口不因新bot漂移(cfgdir):
    write_bot(cfgdir, "chenlulu")
    before = bots_registry.ports()["chenlulu"]
    write_bot(cfgdir, "aaa")                       # 排序更靠前
    after = bots_registry.ports()
    assert before == after["chenlulu"] == 17801
    assert after["aaa"] != 17801


@pytest.mark.parametrize("raw", ["", "abc", "0", "70000", "None"])
def test_垃圾端口值当没给转而派生(cfgdir, raw):
    write_bot(cfgdir, "alpha", dispatcher_port=raw)
    item = bots_registry.bots()[0]
    assert item["port"] == 17801 and item["port_derived"] is True


# ------------------------------------------------------------ namespace

def test_namespace来自yml而legacy只是兜底(cfgdir):
    write_bot(cfgdir, "chenlulu")
    assert bots_registry.namespace_for("chenlulu") == CHENLULU_NS
    write_bot(cfgdir, "alpha", namespace_uuid="22222222-2222-2222-2222-222222222222")
    assert bots_registry.namespace_for("alpha").endswith("222222222222")
    assert bots_registry.namespace_for("查无此bot") is None


def test_没写namespace的新bot回None而不是派生一个(cfgdir):
    """派生 uuid5 会给既有 bot 算出另一个值 → worker 丢整段会话历史，所以绝不派生。"""
    write_bot(cfgdir, "alpha")
    assert bots_registry.bots()[0]["namespace_uuid"] is None


def test_env_BOT_NAMESPACE不在本表内生效(cfgdir, monkeypatch):
    """它是单值 env（逐 bot 注入），在多 bot 表里读它会把一个值套到所有 bot 头上。"""
    monkeypatch.setenv("BOT_NAMESPACE", "99999999-9999-9999-9999-999999999999")
    write_bot(cfgdir, "chenlulu")
    assert bots_registry.namespace_for("chenlulu") == CHENLULU_NS


# ------------------------------------------------------------ 扫描与 CLI

def test_下划线开头与坏文件都不进名单(cfgdir):
    (cfgdir / "_global.yml").write_text("moments:\n  enabled: true\n", encoding="utf-8")
    (cfgdir / "broken.yml").write_text("id: [oops\n  x: : :\n", encoding="utf-8")
    (cfgdir / "notmap.yml").write_text("- 只是个列表\n", encoding="utf-8")
    write_bot(cfgdir, "alpha")
    msgs = []
    assert [b["id"] for b in bots_registry.bots(warn=msgs.append)] == ["alpha"]
    assert sorted(m.split(":")[0] for m in msgs) == ["skip broken.yml", "skip notmap.yml"]


def test_CLI两格式同源(cfgdir):
    write_bot(cfgdir, "alpha", dispatcher_port=17804,
              namespace_uuid="22222222-2222-2222-2222-222222222222")
    sh = run_cli(cfgdir, "--format=sh")
    js = run_cli(cfgdir, "--format=json")
    assert (sh.returncode, js.returncode) == (0, 0), sh.stderr + js.stderr
    assert sh.stdout.strip().split("\t") == \
        ["alpha", "17804", "22222222-2222-2222-2222-222222222222"]
    item = json.loads(js.stdout)[0]
    assert set(item) == {"id", "display_name", "port", "namespace_uuid", "port_derived"}
    assert (item["port"], item["port_derived"]) == (17804, False)


def test_CLI没有bot时空输出退出码1(cfgdir):
    """退出码 1 = 「没有可用 bot」：启动脚本据此报错，而不是静默起 0 个 bot。"""
    sh = run_cli(cfgdir, "--format=sh")
    assert (sh.returncode, sh.stdout.strip()) == (1, "")
    js = run_cli(cfgdir / "根本不存在", "--format=json")
    assert (js.returncode, js.stdout.strip()) == (1, "[]")


def test_CLI坏文件只记一行stderr且其余照出(cfgdir):
    write_bot(cfgdir, "alpha")
    (cfgdir / "broken.yml").write_text("id: [oops\n  x: : :\n", encoding="utf-8")
    p = run_cli(cfgdir, "--format=json")
    assert p.returncode == 0
    assert [b["id"] for b in json.loads(p.stdout)] == ["alpha"]
    lines = [ln for ln in p.stderr.strip().split("\n") if ln.strip()]
    assert len(lines) == 1 and lines[0].startswith("skip broken.yml:")


@pytest.mark.parametrize("args", [("--format=xml",), ("--oops",)])
def test_CLI非法参数退出码2(cfgdir, args):
    p = run_cli(cfgdir, *args)
    assert p.returncode == 2 and p.stdout.strip() == ""
