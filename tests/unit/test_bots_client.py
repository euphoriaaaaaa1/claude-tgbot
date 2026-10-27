# -*- coding: utf-8 -*-
"""M7 单测：bots_client（INTERFACE §6.0-§6.2）。

**绝不真跑 restart-bots.sh**：每个碰重启的用例都设了 `HUB_RESTART_CMD`，
跑的是 true / exit 3 / 往 tmp 写标记这类假命令。
探活也不起真服务：单探活函数被替换成假的，只验并发封顶与状态映射。
"""
import json
import re
import time
import threading

import pytest

from moments import bots_client

TG_TOKEN = "123456789:AA-your-telegram-bot-token-here"   # §6.0 给的形状，假值


@pytest.fixture(autouse=True)
def _no_seams(monkeypatch):
    """任何用例都不许继承外部 env 的接缝值与真实端口。"""
    for k in ("HUB_BOTS_FILE", "HUB_RESTART_CMD", "DISPATCHER_PORT_CHENLULU"):
        monkeypatch.delenv(k, raising=False)


def bots_file(tmp_path, entries):
    p = tmp_path / "bots.json"
    p.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return str(p)


# ------------------------------------------------------------ 名单三态（§6.0）

def test_名单来自HUB_BOTS_FILE且display_name可省(monkeypatch, tmp_path):
    monkeypatch.setenv("HUB_BOTS_FILE",
                       bots_file(tmp_path, [{"id": "a", "display_name": "甲"}, {"id": "b"}]))
    assert bots_client.list_bots() == [{"id": "a", "display_name": "甲"},
                                       {"id": "b", "display_name": "b"}]


def test_空数组是正常态(monkeypatch, tmp_path):
    monkeypatch.setenv("HUB_BOTS_FILE", bots_file(tmp_path, []))
    assert bots_client.list_bots() == []


@pytest.mark.parametrize("raw", [None, "{ 坏掉的", '{"id":"x"}', '"just a string"',
                                 '[{"display_name":"没id"}]', '["不是对象"]'])
def test_名单读不出来一律抛异常(monkeypatch, tmp_path, raw):
    p = tmp_path / "bots.json"
    if raw is not None:
        p.write_text(raw, encoding="utf-8")
    monkeypatch.setenv("HUB_BOTS_FILE", str(p))
    with pytest.raises(Exception):
        bots_client.list_bots()


def test_未设接缝时走真实配置(monkeypatch):
    """未设 HUB_BOTS_FILE 就该回落 config_loader，与不存在该变量时行为一致。

    并锁住 `include_disabled=True`：管理台列表必须含被停用的 bot，
    否则停了以后它从页面消失，再也点不回来。
    """
    seen = {}

    def fake(include_disabled=False):
        seen["include_disabled"] = include_disabled
        return [{"_bot_id": "chenlulu", "display_name": "陈璐璐"}]

    monkeypatch.setattr("config_loader.list_enabled_bots", fake)
    assert bots_client.list_bots() == [{"id": "chenlulu", "display_name": "陈璐璐"}]
    assert seen["include_disabled"] is True


@pytest.mark.parametrize("env,expect", [("18000", 18000), ("", None), ("abc", None),
                                        ("0", None), ("70000", None)])
def test_端口优先取环境变量且拒绝垃圾值(monkeypatch, env, expect):
    monkeypatch.setenv("DISPATCHER_PORT_CHENLULU", env)
    assert bots_client.bot_port("chenlulu") == expect


def test_端口回落到注册表(monkeypatch, tmp_path):
    """原来回落的是模块里写死的 BOT_PORTS，现在回落注册表（configs/*.yml，加 bot 零改代码）。"""
    monkeypatch.delenv("DISPATCHER_PORT_XIAOYU", raising=False)
    d = tmp_path / "configs"
    d.mkdir()
    (d / "xiaoyu.yml").write_text("id: xiaoyu\ndispatcher_port: 17888\n", encoding="utf-8")
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(d))
    assert bots_client.bot_ports() == {"xiaoyu": 17888}
    assert bots_client.bot_port("xiaoyu") == 17888
    assert bots_client.bot_port("ghostbot") is None


# ------------------------------------------------------------ 探活（§6.1）

def fake_probe(monkeypatch, fn):
    monkeypatch.setattr(bots_client, "_probe_one", fn)


def entries(*ids):
    return [{"id": i, "display_name": i} for i in ids]


def test_phase决定online还是starting(monkeypatch):
    monkeypatch.setenv("DISPATCHER_PORT_A", "18001")
    monkeypatch.setenv("DISPATCHER_PORT_B", "18002")
    fake_probe(monkeypatch, lambda port: {"phase": "ready", "pid": 1, "queue_depth": 0,
                                          "in_flight": None}
               if port == 18001 else {"phase": "booting", "pid": 2, "queue_depth": 3})
    a, b = bots_client.probe_bots(entries("a", "b"))
    assert (a["state"], a["phase"], a["pid"]) == ("online", "ready", 1)
    assert (b["state"], b["phase"], b["queue_depth"]) == ("starting", "booting", 3)


def test_探活失败是offline且四字段为null(monkeypatch):
    monkeypatch.setenv("DISPATCHER_PORT_A", "18001")
    fake_probe(monkeypatch, lambda port: None)
    row = bots_client.probe_bots(entries("a"))[0]
    assert row["state"] == "offline" and row["port"] == 18001
    assert [row[k] for k in ("phase", "pid", "queue_depth", "in_flight")] == [None] * 4


def test_没端口是unknown且不发探活(monkeypatch):
    hits = []
    fake_probe(monkeypatch, lambda port: hits.append(port))
    row = bots_client.probe_bots(entries("ghostbot"))[0]
    assert row["state"] == "unknown" and hits == []
    assert set(row) == {"id", "display_name", "port", "state", "phase", "pid",
                        "queue_depth", "in_flight"}
    assert [row[k] for k in ("port", "phase", "pid", "queue_depth", "in_flight")] == [None] * 5


def test_非标量字段不出站(monkeypatch):
    """/status 里塞了对象（未来加字段/被污染）也只出标量，绝不整包透传。"""
    monkeypatch.setenv("DISPATCHER_PORT_A", "18001")
    fake_probe(monkeypatch, lambda port: {"phase": "ready", "session_uuid": "秘密",
                                          "in_flight": {"text": "用户原话"}})
    row = bots_client.probe_bots(entries("a"))[0]
    assert row["in_flight"] is None
    assert "秘密" not in json.dumps(row, ensure_ascii=False)


def test_并发探活不串行且封顶不超预算(monkeypatch):
    """[R4D] I7：6 个慢 bot 串行要 8.4 秒；并发 + 2 秒封顶必须在预算内返回。"""
    live = {"now": 0, "max": 0}
    lock = threading.Lock()

    def slow(_port):
        with lock:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        time.sleep(1.4)
        with lock:
            live["now"] -= 1
        return {"phase": "ready"}

    ids = ["b%d" % i for i in range(6)]
    for i in ids:
        monkeypatch.setenv("DISPATCHER_PORT_%s" % i.upper(), "18000")
    fake_probe(monkeypatch, slow)
    t0 = time.monotonic()
    rows = bots_client.probe_bots(entries(*ids))
    took = time.monotonic() - t0
    assert took < bots_client.PROBE_BUDGET + 0.6, "耗时 %.1fs，像是串行" % took
    assert live["max"] > 1, "根本没并发"
    assert all(r["state"] == "online" for r in rows)


def test_并发数不超过PROBE_WORKERS(monkeypatch):
    """bot 再多也不能一人一线程 —— 门户是单进程，线程炸了什么都干不了。"""
    live = {"now": 0, "max": 0}
    lock = threading.Lock()

    def slow(_port):
        with lock:
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
        time.sleep(0.3)
        with lock:
            live["now"] -= 1
        return {"phase": "ready"}

    ids = ["b%d" % i for i in range(bots_client.PROBE_WORKERS + 6)]
    for i in ids:
        monkeypatch.setenv("DISPATCHER_PORT_%s" % i.upper(), "18000")
    fake_probe(monkeypatch, slow)
    bots_client.probe_bots(entries(*ids))
    assert live["max"] <= bots_client.PROBE_WORKERS, "并发 %d 超过封顶" % live["max"]


def test_超过封顶的慢bot按offline返回(monkeypatch):
    monkeypatch.setenv("DISPATCHER_PORT_SLOWBOT", "18001")
    monkeypatch.setattr(bots_client, "PROBE_BUDGET", 0.3)
    fake_probe(monkeypatch, lambda port: time.sleep(2) or {"phase": "ready"})
    t0 = time.monotonic()
    row = bots_client.probe_bots(entries("slowbot"))[0]
    assert time.monotonic() - t0 < 1.0, "封顶没生效"
    assert row["state"] == "offline"


# ------------------------------------------------------------ 重启（§6.0/§6.2）

def test_设了接缝时命令被替换且恒可用(monkeypatch):
    monkeypatch.setenv("HUB_RESTART_CMD", "sh -c 'exit 0'")
    argv, available = bots_client.restart_plan()
    assert argv == ["sh", "-c", "exit 0"] and available is True


def test_未设接缝时构造真实脚本命令且不执行(monkeypatch):
    """只看命令串长什么样，绝不真跑。"""
    monkeypatch.delenv("HUB_RESTART_CMD", raising=False)
    argv, available = bots_client.restart_plan()
    assert argv[0] in ("bash", "powershell")
    assert argv[-1].endswith(("restart-bots.sh", "restart-bots.ps1"))
    assert isinstance(available, bool)


def wait_done(jobs, job_id, deadline=20.0):
    end = time.time() + deadline
    while time.time() < end:
        st = jobs.get(job_id)
        assert st is not None
        if st["state"] != "running":
            return st
        time.sleep(0.05)
    raise AssertionError("任务没进终态：%s" % jobs.get(job_id))


def test_重启全流程成功(tmp_path):
    marker = tmp_path / "ran.txt"
    jobs = bots_client.RestartJobs()
    acc = jobs.start(["sh", "-c", "echo ran > %s; echo 重启完成了" % marker])
    assert re.fullmatch(r"[0-9a-f]{8}", acc.job_id) and acc.busy is None
    assert isinstance(acc.started_at, int)
    st = wait_done(jobs, acc.job_id)
    assert (st["state"], st["exit_code"]) == ("done", 0)
    assert isinstance(st["took_ms"], int) and st["took_ms"] >= 0
    assert marker.read_text(encoding="utf-8").strip() == "ran"
    assert "重启完成了" in st["tail"]


def test_退出码非0是failed并如实带出(tmp_path):
    jobs = bots_client.RestartJobs()
    st = wait_done(jobs, jobs.start(["sh", "-c", "exit 3"]).job_id)
    assert (st["state"], st["exit_code"]) == ("failed", 3)


def test_tail里的Telegram令牌被脱敏():
    """§6.2：restart-bots.sh 会 source channel 的 .env，stderr 可能带出 token。"""
    jobs = bots_client.RestartJobs()
    st = wait_done(jobs, jobs.start(["sh", "-c", "echo %s >&2; exit 1" % TG_TOKEN]).job_id)
    assert st["state"] == "failed"
    assert TG_TOKEN not in json.dumps(st, ensure_ascii=False)
    assert "***" in st["tail"], "stderr 根本没被采集：%r" % st["tail"]


def test_超时后杀掉进程树并标记timeout(tmp_path):
    """真实超时是 180 秒，这里把超时压到秒级验状态机（§6.0 给的第三种典型）。"""
    marker = tmp_path / "child-alive.txt"
    jobs = bots_client.RestartJobs()
    acc = jobs.start(["sh", "-c", "sleep 30; echo late > %s" % marker], timeout=0.5)
    st = wait_done(jobs, acc.job_id, deadline=15.0)
    assert st["state"] == "timeout" and st["exit_code"] is None
    time.sleep(0.3)
    assert not marker.exists(), "子进程没被杀掉"


def test_并发重启被互斥挡住并回正在跑的job_id():
    jobs = bots_client.RestartJobs()
    first = jobs.start(["sh", "-c", "sleep 2"])
    second = jobs.start(["sh", "-c", "sleep 2"])
    assert second.job_id is None and second.busy == first.job_id
    wait_done(jobs, first.job_id)
    third = jobs.start(["true"])                 # 跑完就该放行，不是一辈子只能一次
    assert third.job_id and third.job_id != first.job_id
    wait_done(jobs, third.job_id)


def test_job只留最近5条():
    jobs = bots_client.RestartJobs()
    ids = []
    for _ in range(6):
        acc = jobs.start(["true"])
        ids.append(acc.job_id)
        wait_done(jobs, acc.job_id)
    assert jobs.get(ids[0]) is None, "第 1 条没被挤掉"
    assert all(jobs.get(j) is not None for j in ids[1:])


def test_未知job回None():
    assert bots_client.RestartJobs().get("deadbeef") is None


def test_命令起不来也不会卡死互斥():
    jobs = bots_client.RestartJobs()
    st = wait_done(jobs, jobs.start(["/does/not/exist"]).job_id)
    assert st["state"] == "failed"
    assert jobs.start(["true"]).job_id, "失败后互斥没释放"
