# -*- coding: utf-8 -*-
"""§4.1 `POST /hub/api/bots/test_token` —— getMe 失败分型表逐行 + 外发面三条硬约束。

本端点 HTTP 恒 200（业务失败放 body），只有"我们这边坏了"才用 4xx。
"""
import pytest

import hub2util as u
from stub_telegram import LEAK_TOKEN

TOKEN = "123456789:test-AAaaBBbbCCccDDddEEeeFFggHHhh11"
EP = "/hub/api/bots/test_token"


def _call(apif, telegram, token=TOKEN, **env):
    api = apif(HUB_TELEGRAM_API_BASE=telegram.base, **env)
    return api.post(EP, {"telegram_token": token})


def test_测token_成功_回username与打码token(apif, telegram):
    telegram.mode = "ok"
    code, body = _call(apif, telegram)
    assert code == 200, body
    assert body["ok"] is True
    assert body["username"] == "my_bot"
    assert body["bot_id"] == 123456789
    assert body["token_masked"].endswith(TOKEN[-4:])
    assert TOKEN not in u.flat_text(body), "响应里出现了完整 token"


@pytest.mark.parametrize("mode,code_", [("401", 401), ("404", 404)])
def test_测token_Telegram回401或404_stage是invalid_token(apif, telegram, mode, code_):
    telegram.mode = mode
    code, body = _call(apif, telegram)
    assert code == 200, body
    assert body["ok"] is False
    assert body["stage"] == "invalid_token"
    assert body["error_code"] == code_


def test_测token_Telegram回其它4xx_stage是rejected并带description(apif, telegram):
    telegram.mode = "429"
    code, body = _call(apif, telegram)
    assert code == 200, body
    assert body["stage"] == "rejected"
    assert body["error_code"] == 429
    assert "Too Many Requests" in body["description"]


def test_测token_Telegram回5xx_stage是upstream(apif, telegram):
    telegram.mode = "500"
    code, body = _call(apif, telegram)
    assert code == 200, body
    assert body["stage"] == "upstream"
    assert body["error_code"] == 500


def test_测token_响应不是JSON_stage是bad_response(apif, telegram):
    telegram.mode = "badjson"
    code, body = _call(apif, telegram)
    assert code == 200, body
    assert body["stage"] == "bad_response"


def test_测token_连不上_stage是network且detail只给异常类名(apif, telegram):
    """base 指向一个没人监听的回环端口（autouse 默认就是这个）。"""
    telegram.stop()
    code, body = _call(apif, telegram)
    assert code == 200, body
    assert body["stage"] == "network"
    assert isinstance(body["detail"], str) and " " not in body["detail"].strip()
    assert TOKEN not in u.flat_text(body)


@pytest.mark.slow
def test_测token_10秒超时_stage是timeout(apif, telegram):
    telegram.mode = "hang"
    telegram.delay = 12
    code, body = _call(apif, telegram)
    assert code == 200, body
    assert body["stage"] == "timeout"


def test_测token_HTTP200但body是ok_false_stage是rejected(apif, telegram):
    """R9-1：Telegram 限流的常见形态，落进"成功"分支会 KeyError → 500。"""
    telegram.mode = "ok_false_200"
    code, body = _call(apif, telegram)
    assert code == 200, body
    assert body["ok"] is False
    assert body["stage"] == "rejected"
    assert body["error_code"] == 429


def test_测token_ok为true但result缺username_stage是bad_response(apif, telegram):
    """R9-2：base 指向任意 HTTP 服务时必然遇到。"""
    telegram.mode = "no_username"
    code, body = _call(apif, telegram)
    assert code == 200, body
    assert body["stage"] == "bad_response"


def test_测token_遇3xx重定向_stage是bad_response且不跟随(apif, telegram):
    """R9-3：跟随重定向会把带 token 的 URL 送到 Location 主机。"""
    telegram.mode = "redirect"
    code, body = _call(apif, telegram)
    assert code == 200, body
    assert body["stage"] == "bad_response"
    assert not any(p == "/redirected" for _, p in telegram.hits), \
        "跟随了重定向（必须写死 allow_redirects=False）"


def test_测token_响应体超过64KiB_stage是bad_response(apif, telegram):
    """R2-2：base 误指到大文件/慢速流，10 秒足够灌进几百 MB 把门户 OOM。"""
    telegram.mode = "huge"
    telegram.huge_bytes = 300 * 1024
    code, body = _call(apif, telegram)
    assert code == 200, body
    assert body["stage"] == "bad_response"


@pytest.mark.parametrize("bad", [
    pytest.param("12345:short", id="密钥段太短"),
    pytest.param("123456789", id="没有冒号"),
    pytest.param("abcdefghi:AAaaBBbbCCccDDddEEeeFFggHHhh11", id="前段不是数字"),
    pytest.param("123:AAaaBBbbCCccDDddEEeeFFggHHhh1122", id="前段位数不够"),
    pytest.param("123456789:AAaa BBbb CCcc DDdd EEee FFgg 11", id="含空格"),
])
def test_测token_格式预检不通过_stage是malformed且一个请求都不发(apif, telegram, bad):
    telegram.mode = "ok"
    code, body = _call(apif, telegram, token=bad)
    assert code == 200, body
    assert body["ok"] is False
    assert body["stage"] == "malformed"
    assert telegram.hits == [], "格式不对就不该发出网络请求"


@pytest.mark.parametrize("payload", [
    pytest.param({}, id="缺字段"),
    pytest.param({"telegram_token": ""}, id="空串"),
    pytest.param({"telegram_token": 123}, id="非字符串"),
    pytest.param({"telegram_token": None}, id="null"),
])
def test_测token_请求体不对_400_bad_body(apif, telegram, payload):
    api = apif(HUB_TELEGRAM_API_BASE=telegram.base)
    code, body = api.post(EP, payload)
    assert code == 400, body
    assert body["error"] == "bad_body"


def test_测token_上游把token回显在描述里_响应与日志都不许带出来(apif, telegram, capfd):
    """§4.1 🔴 脱敏：description 与 detail 绝不含原始请求 URL 或响应体。"""
    telegram.mode = "401"
    telegram.echo_token_in_body = True
    code, body = _call(apif, telegram)
    out, err = capfd.readouterr()
    assert code == 200, body
    blob = u.flat_text(body)
    assert LEAK_TOKEN not in blob, "上游回显的 token 被原样带进响应"
    assert "/bot" not in blob, "响应里出现了带 token 的请求 URL"
    assert LEAK_TOKEN not in out and LEAK_TOKEN not in err, "token 进了 stdout/stderr"
    assert TOKEN not in out and TOKEN not in err


def test_测token_上游描述里的控制字符被剔除且截到200字(apif, telegram):
    """R2-3：description 是上游可控字符串，页面要 textContent 渲染，服务端先消毒。"""
    telegram.mode = "429"
    telegram.description = "\x00\x01\x1b[31m危险\x07" + "A" * 500
    code, body = _call(apif, telegram)
    assert code == 200, body
    desc = body["description"]
    assert len(desc) <= 200, f"没截断：{len(desc)}"
    assert not any(ord(ch) < 0x20 for ch in desc), "控制字符没剔干净"


@pytest.mark.parametrize("base", [
    pytest.param("not-a-url", id="根本不是URL"),
    pytest.param("https://api.telegram.org/?x=1", id="带query"),
    pytest.param("https://api.telegram.org/bot", id="带路径"),
])
def test_getMe基址形状非法_进程启动即失败(make_client, base):
    """§7 R2-1：base 是环境变量不是请求参数，配错必须在启动期炸，不是运行时 4xx。"""
    with pytest.raises(Exception):
        make_client(HUB_TELEGRAM_API_BASE=base)


def test_getMe基址是https的第三方域名_形状合法允许启动(make_client):
    """§7：https://evil.example 形状合法（是用户自己的配置），只断言能起来，不发请求。"""
    c, _ = make_client(HUB_TELEGRAM_API_BASE="https://evil.example")
    assert c.get("/hub/addbot").status_code == 200


def test_测token_基址是明文http非回环_200_blocked_base且一个请求都不发(apif, telegram):
    """§13 A1 裁决：拒发的形状定死为 `200 {"ok":false,"stage":"blocked_base"}`，
    与 `malformed` 同为本地预检 —— 外部据此能区分「拒发」与「发了但连不上（network）」。"""
    telegram.mode = "ok"
    api = apif(HUB_TELEGRAM_API_BASE="http://evil.example")
    code, body = api.post(EP, {"telegram_token": TOKEN})
    assert code == 200, body
    assert body["ok"] is False
    assert body["stage"] == "blocked_base"
    assert telegram.hits == [], "拒发却发出了网络请求"


def test_测token_拒发时不回显base原文也不带出token(apif, telegram):
    """§13 A1：`blocked_base` 的响应**不回显 base 原文、不含 token**。"""
    api = apif(HUB_TELEGRAM_API_BASE="http://evil.example")
    code, body = api.post(EP, {"telegram_token": TOKEN})
    blob = u.flat_text(body)
    assert code == 200, body
    assert TOKEN not in blob
    assert "evil.example" not in blob, "把配错的 base 原文回显给了页面"
