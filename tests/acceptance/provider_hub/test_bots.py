# -*- coding: utf-8 -*-
"""§6.1 bot 状态（BRIEF S6 的"如实显示在线/离线"那一半）。

名单经 INTERFACE §6.0 的 `HUB_BOTS_FILE` 注入，端口仍走 `DISPATCHER_PORT_<BOT>`，
所以三态（online/starting/offline/unknown）都能确定地构造出来，不依赖本机装了哪些 bot。
探活打的是桩 dispatcher，绝不碰真实 17801。重启流程见 test_bots_restart.py。
"""
import json
import time

import pytest

from conftest import _Api


def _bots_file(tmp_path, entries):
    p = tmp_path / "bots.json"
    p.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return str(p)


def _api(make_client, bots_file, **env):
    return _Api(make_client(HUB_BOTS_FILE=bots_file, **env)[0])


def _get(a):
    code, body = a.get("/hub/api/bots")
    assert code == 200, body
    return body


def _row(body, bot_id):
    hit = [b for b in body["bots"] if b["id"] == bot_id]
    assert hit, "名单里没有 %s：%s" % (bot_id, body["bots"])
    return hit[0]


def test_列表基本形状(make_client, tmp_path, dispatcher_stub):
    f = _bots_file(tmp_path, [{"id": "chenlulu", "display_name": "陈璐璐"}])
    body = _get(_api(make_client, f, DISPATCHER_PORT_CHENLULU=dispatcher_stub.port))
    assert isinstance(body["restart_available"], bool)
    row = _row(body, "chenlulu")
    assert set(row) == {"id", "display_name", "port", "state", "phase", "pid",
                        "queue_depth", "in_flight"}
    assert row["display_name"] == "陈璐璐"
    assert row["port"] == dispatcher_stub.port


def test_探活必须用POST(make_client, tmp_path, dispatcher_stub):
    """§6.1：dispatcher 对非 POST 一律 405，用错方法会把在线的 bot 误报成离线。"""
    f = _bots_file(tmp_path, [{"id": "chenlulu"}])
    _get(_api(make_client, f, DISPATCHER_PORT_CHENLULU=dispatcher_stub.port))
    assert dispatcher_stub.hits, "门户根本没探活"
    assert all(m == "POST" for m, _ in dispatcher_stub.hits), \
        "用了非 POST 探活：%s" % dispatcher_stub.hits
    assert all(p.startswith("/status") for _, p in dispatcher_stub.hits)


def test_phase为ready时state是online(make_client, tmp_path, dispatcher_stub):
    f = _bots_file(tmp_path, [{"id": "chenlulu"}])
    row = _row(_get(_api(make_client, f, DISPATCHER_PORT_CHENLULU=dispatcher_stub.port)),
               "chenlulu")
    assert row["state"] == "online"
    assert row["phase"] == "ready"
    assert row["pid"] == 12345
    assert row["queue_depth"] == 0


def test_phase不是ready时state是starting(make_client, tmp_path, dispatcher_stub):
    """§6.1：state 反映 dispatcher 进程，phase 反映它托管的 worker，页面须分开显示。"""
    dispatcher_stub.payload = {"phase": "booting", "pid": 4321, "queue_depth": 3,
                               "in_flight": None}
    f = _bots_file(tmp_path, [{"id": "chenlulu"}])
    row = _row(_get(_api(make_client, f, DISPATCHER_PORT_CHENLULU=dispatcher_stub.port)),
               "chenlulu")
    assert row["state"] == "starting"
    assert row["phase"] == "booting"
    assert row["queue_depth"] == 3


def test_display_name省略时回落用id(make_client, tmp_path, dispatcher_stub):
    f = _bots_file(tmp_path, [{"id": "chenlulu"}])
    row = _row(_get(_api(make_client, f, DISPATCHER_PORT_CHENLULU=dispatcher_stub.port)),
               "chenlulu")
    assert row["display_name"] == "chenlulu"


def test_连接被拒时state是offline且四个字段为null(make_client, tmp_path, dispatcher_stub):
    port = dispatcher_stub.port
    dispatcher_stub.stop()
    f = _bots_file(tmp_path, [{"id": "chenlulu"}])
    row = _row(_get(_api(make_client, f, DISPATCHER_PORT_CHENLULU=port)), "chenlulu")
    assert row["state"] == "offline"
    for k in ("phase", "pid", "queue_depth", "in_flight"):
        assert row[k] is None, "%s 在 offline 时应为 null，实得 %r" % (k, row[k])


def test_非2xx响应也算offline(make_client, tmp_path, dispatcher_stub):
    dispatcher_stub.status_code = 500
    f = _bots_file(tmp_path, [{"id": "chenlulu"}])
    row = _row(_get(_api(make_client, f, DISPATCHER_PORT_CHENLULU=dispatcher_stub.port)),
               "chenlulu")
    assert row["state"] == "offline"


def test_查不到端口的bot是unknown态且五个字段全null(make_client, tmp_path):
    """§6.0 三态表第三行：名单里有、但两处端口来源都查不到。"""
    f = _bots_file(tmp_path, [{"id": "ghostbot", "display_name": "没端口的"}])
    row = _row(_get(_api(make_client, f)), "ghostbot")
    assert row["state"] == "unknown"
    assert row["display_name"] == "没端口的"
    for k in ("port", "phase", "pid", "queue_depth", "in_flight"):
        assert row[k] is None, "%s 应为 null，实得 %r" % (k, row[k])


def test_unknown态不发任何探活(make_client, tmp_path, dispatcher_stub):
    """§6.0：没端口可打，因此不占 2 秒封顶预算。"""
    f = _bots_file(tmp_path, [{"id": "ghostbot"}])
    _get(_api(make_client, f))
    assert dispatcher_stub.hits == [], "给没有端口的 bot 发了探活：%s" % dispatcher_stub.hits


def test_空名单是正常态不是错误(make_client, tmp_path):
    """§6.0 三态表第二行：bots 是空数组，不是缺省字段。"""
    a = _api(make_client, _bots_file(tmp_path, []))
    code, body = a.get("/hub/api/bots")
    assert code == 200, body
    assert body["bots"] == []
    assert "restart_available" in body


@pytest.mark.parametrize("raw,why", [(None, "文件不存在"), ("{ 坏掉的", "不是合法 JSON"),
                                     ('{"id":"x"}', "顶层不是数组"),
                                     ('"just a string"', "顶层是字符串")])
def test_名单读不出来时返回500_config_unreadable(make_client, tmp_path, raw, why):
    """§6.0 三态表第一行 + §6.1 边界：detail 只含异常类名，不含路径与配置值。"""
    p = tmp_path / "bots.json"
    if raw is not None:
        p.write_text(raw, encoding="utf-8")
    a = _Api(make_client(HUB_BOTS_FILE=str(p))[0])
    code, body = a.get("/hub/api/bots")
    assert code == 500, "%s：期望 500 config_unreadable，实得 %s" % (why, code)
    assert body["error"] == "config_unreadable"
    detail = str(body.get("detail", ""))
    assert str(tmp_path) not in detail, "detail 里带出了文件路径：%r" % detail
    assert "Traceback" not in detail


def test_三态可以混在同一个名单里各自独立(make_client, tmp_path, dispatcher_stub):
    f = _bots_file(tmp_path, [{"id": "onbot"}, {"id": "offbot"}, {"id": "ghostbot"}])
    body = _get(_api(make_client, f,
                     DISPATCHER_PORT_ONBOT=dispatcher_stub.port,
                     DISPATCHER_PORT_OFFBOT=1))     # 1 号端口必然连不上
    assert _row(body, "onbot")["state"] == "online"
    assert _row(body, "offbot")["state"] == "offline"
    assert _row(body, "ghostbot")["state"] == "unknown"


def test_多个慢bot不会把接口拖过2秒封顶(make_client, tmp_path, dispatcher_stub):
    """§6.1 [R4D] I7：必须并发探活。单桩延迟 1.4 秒，3 个串行要 4.2 秒。"""
    dispatcher_stub.delay = 1.4
    f = _bots_file(tmp_path, [{"id": "a1"}, {"id": "a2"}, {"id": "a3"}])
    env = {"DISPATCHER_PORT_A1": dispatcher_stub.port,
           "DISPATCHER_PORT_A2": dispatcher_stub.port,
           "DISPATCHER_PORT_A3": dispatcher_stub.port}
    a = _api(make_client, f, **env)
    t0 = time.time()
    _get(a)
    took = time.time() - t0
    assert took < 2.6, "探活耗时 %.1fs（3 个 bot），像是串行" % took


def test_列表不泄露任何令牌(make_client, tmp_path, dispatcher_stub):
    """S8：bot 行里只该有 id/名字/端口/状态，不该带出 channel 的 .env 内容。"""
    f = _bots_file(tmp_path, [{"id": "chenlulu"}])
    body = _get(_api(make_client, f, DISPATCHER_PORT_CHENLULU=dispatcher_stub.port))
    text = json.dumps(body, ensure_ascii=False)
    assert "TELEGRAM_BOT_TOKEN" not in text
    assert "sk-" not in text


def test_重启端点不接受GET(make_client, tmp_path):
    """反向用例：确认不会因为一次误 GET 就把用户的 bot 全重启了。

    这条**故意不设** HUB_RESTART_CMD —— 即便真实脚本存在，GET 也不该触发它。
    """
    a = _Api(make_client(HUB_BOTS_FILE=_bots_file(tmp_path, []))[0])
    code, body = a.get("/hub/api/bots/restart")
    assert code in (404, 405), "GET /hub/api/bots/restart 返回了 %s" % code
    assert body.get("error") in ("not_found", "method_not_allowed")
