# -*- coding: utf-8 -*-
"""pub-memo-restart · R 判活：管理台总览与 bot 管理卡片的「主程序没响应」状态（行为清单 15-21）。

只经公开面：ClaudeLocalAdapter.bots()、Flask 测试客户端 GET /api/console/overview、console.html 文本。
主程序一律用本机回环假服务；端口无服务 = 先占一个空闲端口再释放。
"""
import re
import time

import pytest

import pmr_helpers as H
from pmr_helpers import BOT, FakeDispatcher, HangServer, MISSING, free_port, status_body, write_bot, write_link_state

STATE_TEXT_DOWN = "主程序没响应，来消息也不会处理。点「重启」会只重启这一只 bot 的主程序"


def _row(adapter):
    rows = adapter.bots()
    assert len(rows) == 1, rows
    return rows[0]


def _fresh(offset):
    """假 dispatcher 在答 /status 的那一刻取 now+offset，避免用例自身耗时拖偏边界。"""
    return lambda: (200, status_body(time.time() + offset))


# ───────────────── #15 ─────────────────
@pytest.mark.parametrize("surface", ["适配器bots", "总览接口"])
def test_R15_启用的bot端口没有服务_显示主程序没响应(r_env, adapter, web_client, surface):
    write_bot(r_env, port=free_port(), enabled=True)
    if surface == "适配器bots":
        row = _row(adapter)
    else:
        client = web_client[0]
        r = client.get("/api/console/overview")
        assert r.status_code == 200
        rows = [b for b in r.get_json()["bots"] if b["bot_id"] == BOT]
        assert len(rows) == 1
        row = rows[0]
    assert row["state"] == "dispatcher_down"
    assert row["state_text"] == STATE_TEXT_DOWN
    assert row["polling"] is False
    assert row["enabled"] is True


# ───────────────── #16 ─────────────────
@pytest.mark.parametrize("offset,expect_state", [(-10, "running"), (-299, "running"), (-301, "dispatcher_down"),
                                                 (-400, "dispatcher_down")],
                         ids=["10秒前", "299秒前_临界内", "301秒前_陈旧", "400秒前_陈旧"])
def test_R16_status带last_poll_ok_超过300秒没拉到更新才判没响应(r_env, adapter, offset, expect_state):
    with FakeDispatcher(status=_fresh(offset)) as fake:
        write_bot(r_env, port=fake.port, enabled=True)
        row = _row(adapter)
    assert row["state"] == expect_state
    assert row["polling"] is True                   # 主程序在答 /status，polling 仍是 alive


# ───────────────── #17 ─────────────────
@pytest.mark.parametrize("link_kind,expect_state", [
    ("新鲜_checked_at为现在", "running"),
    ("过期_checked_at为200秒前", "dispatcher_down"),
    ("乱码", "dispatcher_down"),
])
def test_R17_线路状态文件报断线时不判没响应_过期或乱码按线路通处理(r_env, adapter, link_kind, expect_state):
    with FakeDispatcher(status=_fresh(-400)) as fake:
        write_bot(r_env, port=fake.port, enabled=True)
        now = int(time.time())
        text = {"新鲜_checked_at为现在": "down_since=%d checked_at=%d\n" % (now - 30, now),
                "过期_checked_at为200秒前": "down_since=%d checked_at=%d\n" % (now - 400, now - 200),
                "乱码": "this is not a link state file\n"}[link_kind]
        write_link_state(r_env, BOT, text)
        row = _row(adapter)
    assert row["state"] == expect_state


# ───────────────── #18 ─────────────────
@pytest.mark.parametrize("variant", ["缺失", "为0", "为负数", "为布尔true", "为字符串", "在未来600秒"])
def test_R18_last_poll_ok缺失或不合法_按改前判空闲或运行_不发health(r_env, adapter, variant):
    def st():
        v = {"缺失": MISSING, "为0": 0, "为负数": -5, "为布尔true": True, "为字符串": "abc",
             "在未来600秒": time.time() + 600}[variant]
        return 200, status_body(v)
    with FakeDispatcher(status=st) as fake:
        write_bot(r_env, port=fake.port, enabled=True)
        row = _row(adapter)
        paths = fake.paths()
    assert row["state"] == "running"
    assert ("GET", "/health") not in fake.hits and "/health" not in paths


# ───────────────── #19 ─────────────────
def test_R19_status和health都不回_判主程序没响应(r_env, adapter):
    with HangServer() as hang:
        write_bot(r_env, port=hang.port, enabled=True)
        row = _row(adapter)
    assert row["state"] == "dispatcher_down"


def test_R19_status不回但health回200且新鲜_按空闲(r_env, adapter):
    with FakeDispatcher(status=lambda: "hang") as fake:
        write_bot(r_env, port=fake.port, enabled=True)
        row = _row(adapter)
        paths = fake.paths()
    assert row["state"] == "idle"
    assert "/health" in paths                                # 确实去问了 /health


def test_R19_status回401_按空闲且不发health(r_env, adapter):
    with FakeDispatcher(status=lambda: (401, None)) as fake:
        write_bot(r_env, port=fake.port, enabled=True)
        row = _row(adapter)
        paths = fake.paths()
    assert row["state"] == "idle"
    assert "/health" not in paths


# ───────────────── #20 ─────────────────
def test_R20_已停用的bot_显示stopped且不去问health(r_env, adapter):
    with FakeDispatcher() as fake:
        write_bot(r_env, port=fake.port, enabled=False)
        row = _row(adapter)
        paths = fake.paths()
    assert row["state"] == "stopped" and row["enabled"] is False
    assert "/health" not in paths                           # 已停用的不判活


def test_R20_端口未配_显示idle(r_env, adapter):
    write_bot(r_env, port=None, enabled=True)
    assert _row(adapter)["state"] == "idle"


def test_R20_开关文件存在且端口无服务_回到改前显示idle(r_env, adapter):
    write_bot(r_env, port=free_port(), enabled=True)
    H.set_switch(r_env)
    row = _row(adapter)
    assert row["state"] == "idle"
    assert row["polling"] is False


# ───────────────── #21 ─────────────────
def test_R21_console页面_状态表和重启按钮的脚本都在():
    html = (H.REPO / "moments" / "templates" / "console.html").read_text(encoding="utf-8")
    assert re.search(r"STATE_TEXT\s*=\s*\{[^}]*dispatcher_down: '主程序没响应'", html), "STATE_TEXT 缺 dispatcher_down"
    assert re.search(r"STATE_KIND\s*=\s*\{[^}]*dispatcher_down: 'bad'", html), "STATE_KIND 缺 dispatcher_down: 'bad'"
    found = False
    for m in re.finditer(r"/restart", html):
        window = html[max(0, m.start() - 300): m.end() + 300]
        if "/api/console/bots/" in window and "POST" in window:
            found = True
    assert found, "页面脚本里没有对 /api/console/bots/.../restart 的 POST"
