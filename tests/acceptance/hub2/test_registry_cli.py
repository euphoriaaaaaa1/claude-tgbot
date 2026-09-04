# -*- coding: utf-8 -*-
"""§6 注册表 CLI 契约（`python3 -m bots_registry`）——「契约即测试点」。"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def run_cli(configs_dir, *args, **extra_env):
    env = dict(os.environ)
    env["HUB_CONFIGS_DIR"] = str(configs_dir)
    env.update({k: str(v) for k, v in extra_env.items()})
    p = subprocess.run([sys.executable, "-m", "bots_registry", *args],
                       cwd=str(REPO_ROOT), env=env, capture_output=True,
                       text=True, timeout=60)
    assert "No module named" not in p.stderr, "bots_registry 模块不存在（未实现）"
    return p


def write_bot(configs_dir, bot_id, **fields):
    lines = [f"id: {bot_id}", "display_name: 某个 bot",
             f"bot_channel_path: ~/.claude/channels/{bot_id}", "chat_id: 111"]
    lines += [f"{k}: {v}" for k, v in fields.items()]
    (configs_dir / f"{bot_id}.yml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_CLI_sh格式_每行三列制表符分隔且按id排序(configs_dir, global_yml):
    write_bot(configs_dir, "zeta", dispatcher_port=17805,
              namespace_uuid="11111111-1111-1111-1111-111111111111")
    write_bot(configs_dir, "alpha", dispatcher_port=17804,
              namespace_uuid="22222222-2222-2222-2222-222222222222")
    p = run_cli(configs_dir, "--format=sh")
    assert p.returncode == 0, p.stderr
    rows = [ln.split("\t") for ln in p.stdout.strip().split("\n")]
    assert [r[0] for r in rows] == ["alpha", "zeta"], "没按 id 排序或多了表头"
    assert rows[0] == ["alpha", "17804", "22222222-2222-2222-2222-222222222222"]
    assert all(len(r) == 3 for r in rows)


def test_CLI_json格式_字段齐全(configs_dir, global_yml):
    write_bot(configs_dir, "alpha", dispatcher_port=17804,
              namespace_uuid="22222222-2222-2222-2222-222222222222")
    p = run_cli(configs_dir, "--format=json")
    assert p.returncode == 0, p.stderr
    items = json.loads(p.stdout)
    assert len(items) == 1
    assert set(items[0]) == {"id", "display_name", "port", "namespace_uuid",
                             "port_derived"}
    assert items[0]["port"] == 17804
    assert items[0]["port_derived"] is False


def test_CLI_下划线开头的yml不算bot(configs_dir, global_yml):
    """`_global.yml` 就在同一个目录里，绝不能被当成一个 bot。"""
    p = run_cli(configs_dir, "--format=json")
    assert json.loads(p.stdout or "[]") == []


def test_CLI_没有任何bot_空输出且退出码1(configs_dir, global_yml):
    """PLAN R3-A：退出码 1 表示"没有可用 bot"是刻意的。"""
    p = run_cli(configs_dir, "--format=sh")
    assert p.returncode == 1
    assert p.stdout.strip() == ""


def test_CLI_配置目录不存在_退出码1(configs_dir):
    p = run_cli(configs_dir / "nope", "--format=json")
    assert p.returncode == 1
    assert p.stdout.strip() in ("", "[]")


def test_CLI_某个yml坏掉_跳过它其余照出并在stderr记一行(configs_dir, global_yml):
    write_bot(configs_dir, "alpha", dispatcher_port=17804,
              namespace_uuid="22222222-2222-2222-2222-222222222222")
    (configs_dir / "broken.yml").write_text("id: [oops\n  x: : :\n", encoding="utf-8")
    p = run_cli(configs_dir, "--format=json")
    assert p.returncode == 0, p.stderr
    assert [i["id"] for i in json.loads(p.stdout)] == ["alpha"]
    lines = [ln for ln in p.stderr.strip().split("\n") if ln.strip()]
    assert len(lines) == 1, f"stderr 应恰好一行：{lines}"
    assert lines[0].startswith("skip ") and "broken.yml" in lines[0]


def test_CLI_所有yml都坏掉_退出码1(configs_dir, global_yml):
    (configs_dir / "broken.yml").write_text("id: [oops\n  x: : :\n", encoding="utf-8")
    p = run_cli(configs_dir, "--format=json")
    assert p.returncode == 1


def test_CLI_端口优先级_环境变量盖过yml(configs_dir, global_yml):
    """F3 解析顺序第一档：env DISPATCHER_PORT_<BOT>。"""
    write_bot(configs_dir, "xiaoyu", dispatcher_port=17804,
              namespace_uuid="33333333-3333-3333-3333-333333333333")
    p = run_cli(configs_dir, "--format=json", DISPATCHER_PORT_XIAOYU=19999)
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)[0]["port"] == 19999


def test_CLI_端口优先级_legacy兜底且不算派生(configs_dir, global_yml):
    """§8 承诺：chenlulu 不写字段也必须是 17801，且 port_derived 为 false。"""
    write_bot(configs_dir, "chenlulu")
    item = json.loads(run_cli(configs_dir, "--format=json").stdout)[0]
    assert item["port"] == 17801
    assert item["port_derived"] is False


def test_CLI_新bot缺端口_派生最小空闲号(configs_dir, global_yml):
    """F3：派生 = 取 ≥17801 且未被任何显式/legacy 端口占用的最小空闲号。"""
    write_bot(configs_dir, "chenlulu")
    write_bot(configs_dir, "xiaoyu")
    items = {i["id"]: i for i in json.loads(run_cli(configs_dir, "--format=json").stdout)}
    assert items["chenlulu"]["port"] == 17801
    assert items["xiaoyu"]["port"] == 17802
    assert items["xiaoyu"]["port_derived"] is True


def test_CLI_加一个排序更靠前的bot_既有bot端口不漂移(configs_dir, global_yml):
    """§8 F3 的核心承诺 —— 「17801 + index」那种写法会让 chenlulu 静默失联。"""
    write_bot(configs_dir, "chenlulu")
    before = json.loads(run_cli(configs_dir, "--format=json").stdout)
    assert [i for i in before if i["id"] == "chenlulu"][0]["port"] == 17801
    write_bot(configs_dir, "aaa")
    after = {i["id"]: i for i in json.loads(run_cli(configs_dir, "--format=json").stdout)}
    assert after["chenlulu"]["port"] == 17801, "既有 bot 的端口被挤走了"
    assert after["aaa"]["port"] != 17801


def test_CLI_显式端口占住时派生跳过它(configs_dir, global_yml):
    write_bot(configs_dir, "bbb", dispatcher_port=17802,
              namespace_uuid="44444444-4444-4444-4444-444444444444")
    write_bot(configs_dir, "ccc")
    items = {i["id"]: i for i in json.loads(run_cli(configs_dir, "--format=json").stdout)}
    assert items["bbb"]["port"] == 17802
    assert items["ccc"]["port"] not in (17802,)
    assert items["ccc"]["port"] >= 17801


CHENLULU_NS = "550e8400-e29b-41d4-a716-446655440001"   # §8 / §13 A5 定死的常量


def test_CLI_chenlulu的namespace不写字段时等于定死的legacy值(configs_dir, global_yml):
    """§8 + §13 A5：值定死为 `550e8400-...0001`，测试硬编码这个字符串。
    弱断言（非空且多次一致）在整表被换掉时照样绿 —— 那正是会话历史静默丢失的场景。"""
    write_bot(configs_dir, "chenlulu")
    item = json.loads(run_cli(configs_dir, "--format=json").stdout)[0]
    assert item["namespace_uuid"] == CHENLULU_NS


def test_CLI_sh格式第三列就是chenlulu定死的namespace(configs_dir, global_yml):
    """§6 `--format=sh` 第三列 = `namespace_uuid`；启动脚本据此注入 `BOT_NAMESPACE`（§13 A6）。"""
    write_bot(configs_dir, "chenlulu")
    p = run_cli(configs_dir, "--format=sh")
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip().split("\t") == ["chenlulu", "17801", CHENLULU_NS]


def test_CLI_stdout里不出现任何token形状的串(configs_dir, global_yml, channels_dir):
    """反向：注册表读的是不含 token 的 yml，输出里绝不该冒出 token。"""
    import re
    write_bot(configs_dir, "alpha", dispatcher_port=17804,
              namespace_uuid="55555555-5555-5555-5555-555555555555")
    (channels_dir / "alpha").mkdir(parents=True, exist_ok=True)
    (channels_dir / "alpha" / ".env").write_text(
        "TELEGRAM_BOT_TOKEN=123456789:test-AAaaBBbbCCccDDddEEeeFFggHHhh11\n",
        encoding="utf-8")
    p = run_cli(configs_dir, "--format=json")
    assert not re.search(r"\d{8,10}:[A-Za-z0-9_-]{30,}", p.stdout + p.stderr)
