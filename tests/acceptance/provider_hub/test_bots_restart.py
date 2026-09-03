# -*- coding: utf-8 -*-
"""§6.2 重启 bot 的完整流程 —— 经 INTERFACE §6.0 的 `HUB_RESTART_CMD` 接缝驱动。

**绝不会重启用户真实的 bot**：每个用例都设了 `HUB_RESTART_CMD`，
执行的是 `true` / `exit 3` / 往 tmp 写标记文件这类假命令。
未设该变量的真实路径不在本文件内触发（那才是会动真 bot 的那条）。

⚠️ 已知风险（接缝类测试固有）：若实现**没兑现** `HUB_RESTART_CMD`，
第一条用例会以"标记文件不存在"失败，但那一次会真的跑到 restart-bots.sh。
INTERFACE §6.0 明确要求该接缝，风险已知并接受；实现方请先把这条接缝接上再跑本文件。
"""
import json
import os
import re
import time

import pytest

from conftest import _Api

TG_TOKEN = "123456789:AAxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"   # §6.0 给的形状，假值


def _api(make_client, cmd, **env):
    return _Api(make_client(HUB_RESTART_CMD=cmd, **env)[0])


def _wait_done(a, job_id, deadline=20.0):
    """轮询到终态；超时就把最后一次响应带进断言消息，便于定位。"""
    end = time.time() + deadline
    last = None
    while time.time() < end:
        code, last = a.get("/hub/api/bots/restart/%s" % job_id)
        assert code == 200, "查任务状态实得 %s：%s" % (code, last)
        if last.get("state") != "running":
            return last
        time.sleep(0.15)
    raise AssertionError("任务 %s 在 %.0f 秒内没进终态，最后一次：%s" % (job_id, deadline, last))


def test_受理即返回202与job_id(make_client):
    """§6.2：立即 202，不做 180 秒同步等待（浏览器和中间代理都会先超时）。"""
    a = _api(make_client, "true")
    t0 = int(time.time())
    code, body = a.post("/hub/api/bots/restart")
    assert code == 202, body
    assert re.fullmatch(r"[0-9a-f]{8}", body["job_id"]), "job_id 应是 8 位 hex：%r" % body["job_id"]
    assert isinstance(body["started_at"], int)
    assert abs(body["started_at"] - t0) < 10


def test_命令真的被执行了(make_client, tmp_path):
    """反向断言的正面版：不看状态机，直接看文件系统有没有留下痕迹。"""
    marker = tmp_path / "restart-ran.txt"
    a = _api(make_client, "sh -c 'echo ran > %s'" % marker)
    job = a.post("/hub/api/bots/restart")[1]["job_id"]
    st = _wait_done(a, job)
    assert st["state"] == "done", st
    assert marker.exists(), "重启命令没被执行（标记文件不存在）"
    assert marker.read_text(encoding="utf-8").strip() == "ran"


def test_命令成功时状态done退出码0(make_client):
    a = _api(make_client, "true")
    job = a.post("/hub/api/bots/restart")[1]["job_id"]
    st = _wait_done(a, job)
    assert st["state"] == "done"
    assert st["exit_code"] == 0
    assert isinstance(st["took_ms"], int) and st["took_ms"] >= 0


def test_命令失败时状态failed并如实带出退出码(make_client):
    a = _api(make_client, "sh -c 'exit 3'")
    job = a.post("/hub/api/bots/restart")[1]["job_id"]
    st = _wait_done(a, job)
    assert st["state"] == "failed", st
    assert st["exit_code"] == 3, "退出码没如实带出：%s" % st


def test_输出被采集进tail(make_client):
    a = _api(make_client, "sh -c 'echo 重启完成了'")
    st = _wait_done(a, a.post("/hub/api/bots/restart")[1]["job_id"])
    assert "重启完成了" in st["tail"], "脚本输出没被采集：%r" % st.get("tail")


def test_tail里的Telegram令牌必须被脱敏(make_client):
    """§6.2：restart-bots.sh 会 source channel 的 .env，stderr 可能带出 token。"""
    a = _api(make_client, "sh -c 'echo %s >&2; exit 1'" % TG_TOKEN)
    st = _wait_done(a, a.post("/hub/api/bots/restart")[1]["job_id"])
    assert st["state"] == "failed"
    assert TG_TOKEN not in json.dumps(st, ensure_ascii=False), \
        "Telegram token 明文出现在 tail 里：%r" % st.get("tail")


def test_上一次还在跑时再点返回409并给出正在跑的job_id(make_client):
    """§6.2 进程内互斥：不许两次重启并发压在一起。"""
    a = _api(make_client, "sh -c 'sleep 3'")
    first = a.post("/hub/api/bots/restart")[1]["job_id"]
    code, body = a.post("/hub/api/bots/restart")
    assert code == 409, "并发重启没被挡住：%s %s" % (code, body)
    assert body["error"] == "restart_in_progress"
    assert body["job_id"] == first, "409 里应带上正在跑的那个 job_id"


def test_上一次跑完后可以再次重启(make_client):
    """互斥是"同时只能一个"，不是"一辈子只能一次"。"""
    a = _api(make_client, "true")
    first = a.post("/hub/api/bots/restart")[1]["job_id"]
    _wait_done(a, first)
    code, body = a.post("/hub/api/bots/restart")
    assert code == 202, body
    assert body["job_id"] != first, "两次任务的 job_id 不该相同"


def test_设了替身命令时restart_available恒为真(make_client):
    """§6.0：不再去 stat 脚本文件，因此 409 restart_script_missing 不会在这条路径出现。"""
    a = _api(make_client, "true")
    code, body = a.get("/hub/api/bots")
    assert code == 200, body
    assert body["restart_available"] is True


def test_job记录只留最近5条(make_client):
    """§6.2：内存里只保留最近 5 条 —— 第 6 次跑完后，第 1 条应查不到。"""
    a = _api(make_client, "true")
    jobs = []
    for _ in range(6):
        code, body = a.post("/hub/api/bots/restart")
        assert code == 202, body
        jobs.append(body["job_id"])
        _wait_done(a, jobs[-1])
    assert a.get("/hub/api/bots/restart/%s" % jobs[0])[0] == 404, "第 1 条没被挤掉"
    for j in jobs[1:]:
        assert a.get("/hub/api/bots/restart/%s" % j)[0] == 200, "最近 5 条应还在：%s" % j


def test_查不存在的job返回404(make_client):
    a = _api(make_client, "true")
    code, body = a.get("/hub/api/bots/restart/deadbeef")
    assert code == 404
    assert body["error"] == "not_found"


def test_重启接口不回显命令串(make_client, tmp_path):
    """反向用例：命令串可能含路径与参数，属配置值，不该出现在响应里（§7 末行同理）。"""
    marker = tmp_path / "secret-path-marker"
    a = _api(make_client, "sh -c 'touch %s'" % marker)
    code, body = a.post("/hub/api/bots/restart")
    st = _wait_done(a, body["job_id"])
    assert "secret-path-marker" not in json.dumps(st, ensure_ascii=False), \
        "命令串被回显：%s" % st


@pytest.mark.slow
@pytest.mark.skipif(not os.environ.get("HUB_TEST_SLOW"),
                    reason="timeout 态要等满 180 秒——INTERFACE 未给超时时长的注入接缝，"
                           "设 HUB_TEST_SLOW=1 才跑")
def test_命令挂死超过180秒后状态timeout(make_client):
    a = _api(make_client, "sh -c 'sleep 999'")
    job = a.post("/hub/api/bots/restart")[1]["job_id"]
    st = _wait_done(a, job, deadline=200.0)
    assert st["state"] == "timeout", st
