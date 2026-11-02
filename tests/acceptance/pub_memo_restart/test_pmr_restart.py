# -*- coding: utf-8 -*-
"""pub-memo-restart · R 整只重启：ClaudeLocalAdapter.restart() 与 POST .../restart 路由（行为清单 22-25）。

整只重启一律经接口文件给的测试接缝（CLAUDEBOT_TEST=1 + CLAUDEBOT_RESTART_ONE_CMD）换成替身脚本，
替身在被调后才在该端口起一个假 dispatcher；绝不真的停起任何进程，结束时清掉替身与它起的假服务。
"""
import subprocess
import sys
import threading
import time

import pytest

import pmr_helpers as H
from pmr_helpers import BOT, FakeDispatcher, LEAK, free_port, pid_alive, write_bot

OK_MSG = "主程序没响应，已连主程序一起重启"
FAIL_MSG = "主程序没起来，请稍后再试或看日志"
BUSY_MSG = "这只 bot 正在重启，请稍候"


# ───────────────── #22 ─────────────────
def test_R22_主程序没响应_整只重启_替身恰被调一次_不走ensure_worker(r_env, adapter, make_stub, monkeypatch):
    port = free_port()
    write_bot(r_env, port=port, enabled=True)
    stub = make_stub(BOT, port, "up")
    stub.apply(monkeypatch)
    result = adapter.restart(BOT, False)
    assert result == {"status": "ok", "message": OK_MSG}
    calls = stub.calls()
    assert len(calls) == 1
    assert calls[0][-1] == BOT
    assert "POST /ensure_worker" not in stub.server_hits() and "GET /ensure_worker" not in stub.server_hits()
    assert any(h.endswith("/status") for h in stub.server_hits())      # 确实等到了 /status 成功


# ───────────────── #23 ─────────────────
def test_R23_替身退出1_failed_不重试_输出不泄漏(r_env, adapter, make_stub, monkeypatch, capfd, caplog):
    port = free_port()
    write_bot(r_env, port=port, enabled=True)
    stub = make_stub(BOT, port, "exit1")
    stub.apply(monkeypatch)
    t0 = time.monotonic()
    result = adapter.restart(BOT, False)
    elapsed = time.monotonic() - t0
    assert result == {"status": "failed", "message": FAIL_MSG}
    assert len(stub.calls()) == 1                                       # 不重试
    assert elapsed < 20
    cap = capfd.readouterr()
    assert LEAK not in cap.out + cap.err + caplog.text                  # 脚本 stdout/stderr 一律丢弃


def test_R23_替身退出0但从不起服务_到60秒总时限failed(r_env, adapter, make_stub, monkeypatch):
    port = free_port()
    write_bot(r_env, port=port, enabled=True)
    stub = make_stub(BOT, port, "noserve")
    stub.apply(monkeypatch)
    t0 = time.monotonic()
    result = adapter.restart(BOT, False)
    elapsed = time.monotonic() - t0
    assert result == {"status": "failed", "message": FAIL_MSG}
    assert elapsed <= 65
    assert len(stub.calls()) == 1


def test_R23_替身25秒不退出_不杀它_20秒后放锁failed(r_env, adapter, make_stub, monkeypatch):
    port = free_port()
    write_bot(r_env, port=port, enabled=True)
    stub = make_stub(BOT, port, "hang", hang_s=25)
    stub.apply(monkeypatch)
    t0 = time.monotonic()
    result = adapter.restart(BOT, False)
    elapsed = time.monotonic() - t0
    assert result == {"status": "failed", "message": FAIL_MSG}
    assert elapsed <= 65
    assert len(stub.calls()) == 1
    assert pid_alive(stub.stub_pid())                                   # 返回时脚本还在睡：没被杀


@H.skip_on_windows
def test_R23_没设CLAUDEBOT_TEST_只设替身命令_走仓内脚本路径_替身不被调(r_env, adapter, make_stub, monkeypatch):
    """走仓内脚本路径时若真去跑 restart-one.sh 会真停起进程，所以经接缝 _popen_restart 截住启动：
    只记下 argv 与重定向参数，并让它抛 OSError（= 单只入口起不来）。"""
    from adapters import claude_proc
    port = free_port()
    write_bot(r_env, port=port, enabled=True)
    stub = make_stub(BOT, port, "up")
    monkeypatch.setenv("CLAUDEBOT_RESTART_ONE_CMD", stub.env()["CLAUDEBOT_RESTART_ONE_CMD"])   # 只设命令，不设 TEST=1
    seen = []

    def fake_popen(argv, *a, **kw):
        seen.append((list(argv), kw))
        raise OSError("blocked by test")
    monkeypatch.setattr(claude_proc, "_popen_restart", fake_popen)
    result = adapter.restart(BOT, False)
    assert result == {"status": "failed", "message": FAIL_MSG}
    assert stub.calls() == []
    assert len(seen) == 1
    argv, kw = seen[0]
    assert argv[0] == "bash" and argv[1].replace("\\", "/").endswith("dispatcher/restart-one.sh") and argv[2:] == [BOT]
    assert kw.get("stdin") == subprocess.DEVNULL and kw.get("stdout") == subprocess.DEVNULL \
        and kw.get("stderr") == subprocess.DEVNULL
    assert kw.get("start_new_session") is True


# ───────────────── #24 ─────────────────
def _restart_normal(r_env, adapter):
    with FakeDispatcher(workers=True) as fake:
        write_bot(r_env, port=fake.port, enabled=True)
        result = adapter.restart(BOT, False)
        return result, list(fake.paths())


def test_R24_主程序正常时_与改前逐字相同_替身没被调(r_env, adapter, make_stub, monkeypatch):
    stub = make_stub(BOT, free_port(), "up")
    stub.apply(monkeypatch)
    new_result, new_paths = _restart_normal(r_env, adapter)
    H.set_switch(r_env)                                                  # 开关文件存在 = 回到改前的判活
    old_result, old_paths = _restart_normal(r_env, adapter)
    assert new_result == old_result
    assert new_result["status"] == "ok"
    assert "/ensure_worker" in new_paths and "/ensure_worker" in old_paths   # 走的是原来的只重启 worker 路径
    assert stub.calls() == []


def test_R24_已停用的bot_409_stopped_替身没被调(r_env, adapter, make_stub, monkeypatch):
    from adapters.base import AdapterError
    port = free_port()
    write_bot(r_env, port=port, enabled=False)
    stub = make_stub(BOT, port, "up")
    stub.apply(monkeypatch)
    with pytest.raises(AdapterError) as ei:
        adapter.restart(BOT, False)
    assert ei.value.code == "stopped" and ei.value.http_status == 409
    assert stub.calls() == []


def test_R24_开关文件存在且主程序挂了_如实报主程序没在运行_替身没被调(r_env, adapter, make_stub, monkeypatch):
    """接口文件写的是 AdapterUnavailable("down")，改前代码实际是返回 failed + 同一句文案；
    两种形态都认（语义同一句「dispatcher 没在运行」），但绝不能是整只重启的文案或成功。"""
    from adapters.base import AdapterUnavailable
    port = free_port()
    write_bot(r_env, port=port, enabled=True)
    H.set_switch(r_env)
    stub = make_stub(BOT, port, "up")
    stub.apply(monkeypatch)
    try:
        result = adapter.restart(BOT, False)
    except AdapterUnavailable as e:
        assert e.status == "down" and e.message == "dispatcher 没在运行"
    else:
        assert result == {"status": "failed", "message": "dispatcher 没在运行"}
    assert stub.calls() == []


def test_R24_整只重启进行中_同一只bot再点_409_busy_替身只被调一次(r_env, adapter, make_stub, monkeypatch):
    from adapters.base import AdapterError
    port = free_port()
    write_bot(r_env, port=port, enabled=True)
    stub = make_stub(BOT, port, "block")
    stub.apply(monkeypatch)
    box = {}

    def first():
        try:
            box["result"] = adapter.restart(BOT, False)
        except BaseException as e:           # noqa: BLE001 —— 记下来在主线程断言
            box["error"] = e
    th = threading.Thread(target=first, daemon=True)
    th.start()
    deadline = time.monotonic() + 15
    while not stub.calls() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert len(stub.calls()) == 1, "第一次整只重启应已把替身拉起（并卡在里面）"
    t0 = time.monotonic()
    with pytest.raises(AdapterError) as ei:
        adapter.restart(BOT, False)
    waited = time.monotonic() - t0
    assert ei.value.code == "busy" and ei.value.http_status == 409 and ei.value.message == BUSY_MSG
    assert waited < 5                                                    # 当场拒绝，不是等锁超时
    stub.release.write_text("", encoding="utf-8")                       # 放行第一次
    th.join(timeout=70)
    assert not th.is_alive()
    assert "error" not in box, box
    assert box["result"] == {"status": "ok", "message": OK_MSG}
    assert len(stub.calls()) == 1


# ───────────────── #25 ─────────────────
def _url(backend="claude", bot=BOT):
    return "/api/console/bots/%s/%s/restart" % (backend, bot)


def test_R25_force不是布尔_422_invalid(r_env, web_client):
    client = web_client[0]
    write_bot(r_env, port=free_port(), enabled=True)
    r = client.post(_url(), json={"force": "yes"})
    assert r.status_code == 422
    assert r.get_json()["code"] == "invalid"


def test_R25_请求体不是对象或超过1KB_422_invalid(r_env, web_client):
    client = web_client[0]
    write_bot(r_env, port=free_port(), enabled=True)
    assert client.post(_url(), json=["force"]).status_code == 422
    big = client.post(_url(), json={"force": False, "pad": "x" * 2000})
    assert big.status_code == 422 and big.get_json()["code"] == "invalid"


def test_R25_主程序没响应_空对象请求体_200_整只重启成功(r_env, web_client, make_stub, monkeypatch):
    client = web_client[0]
    port = free_port()
    write_bot(r_env, port=port, enabled=True)
    stub = make_stub(BOT, port, "up")
    stub.apply(monkeypatch)
    r = client.post(_url(), json={})
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "status": "ok", "message": OK_MSG}
    assert len(stub.calls()) == 1


def test_R25_整只重启失败_200_ok为false(r_env, web_client, make_stub, monkeypatch):
    client = web_client[0]
    port = free_port()
    write_bot(r_env, port=port, enabled=True)
    stub = make_stub(BOT, port, "exit1")
    stub.apply(monkeypatch)
    r = client.post(_url(), json={})
    assert r.status_code == 200
    assert r.get_json() == {"ok": False, "status": "failed", "message": FAIL_MSG}


def test_R25_跨站来源_403_且不重启(r_env, web_client, make_stub, monkeypatch):
    client = web_client[0]
    port = free_port()
    write_bot(r_env, port=port, enabled=True)
    stub = make_stub(BOT, port, "up")
    stub.apply(monkeypatch)
    r = client.post(_url(), json={}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert stub.calls() == []


def test_R25_设了口令但没登录_401_且不重启(r_env, web_client, make_stub, monkeypatch):
    _, web, _ = web_client
    port = free_port()
    write_bot(r_env, port=port, enabled=True)
    stub = make_stub(BOT, port, "up")
    stub.apply(monkeypatch)
    anon = web.app.test_client()
    r = anon.post(_url(), json={})
    assert r.status_code == 401
    assert stub.calls() == []


@pytest.mark.parametrize("case", ["底层没有graceful能力", "force为真但没有force能力"])
def test_R25_底层不支持_501_unsupported_不调用restart(r_env, web_client, monkeypatch, case):
    from adapters import registry
    from adapters.base import CAPABILITY_KEYS
    client = web_client[0]
    calls = []

    class CapAdapter:
        backend = "dsh"

        def meta(self):
            caps = {k: True for k in CAPABILITY_KEYS}
            if case == "底层没有graceful能力":
                caps["bot.restart.graceful"] = False
            else:
                caps["bot.restart.graceful"] = True
            caps["bot.restart.force"] = False
            return {"backend": "dsh", "backend_version": "fake", "adapter": "fake", "capabilities": caps,
                    "effort_levels": ["low", "medium", "high", "xhigh", "max"]}

        def bots(self):
            return [{"bot_id": BOT, "person": BOT, "display_name": BOT, "enabled": True, "state": "running",
                     "state_text": "", "polling": True, "username": "x_bot", "provider": None,
                     "provider_source": "default", "pending": None, "detail": {}, "moved_to": None,
                     "backend": "dsh"}]

        def providers(self):
            return {"providers": [], "default": None}

        def restart(self, bot_id, force):
            calls.append((bot_id, force))
            return {"status": "ok", "message": "should not run"}
    fake = CapAdapter()
    monkeypatch.setattr(registry, "enabled_backends", lambda: ["dsh"])
    monkeypatch.setattr(registry, "adapter", lambda name: fake)
    monkeypatch.setattr(registry, "label", lambda name: "dsh")
    body = {"force": True} if case == "force为真但没有force能力" else {}
    r = client.post(_url("dsh"), json=body)
    assert r.status_code == 501
    assert r.get_json()["code"] == "unsupported"
    assert calls == []
