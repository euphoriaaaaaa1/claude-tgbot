# -*- coding: utf-8 -*-
"""§4.2 A 段「只读校验」逐行 + §4.4 模板列表。

A 段的承诺是**失败零副作用**：每条用例都顺带断言磁盘上没长出任何东西。
"""
import pytest

import hub2util as u
from hub2util import OMIT, addbot_payload

EP = "/hub/api/bots"
TOKEN = "123456789:test-AAaaBBbbCCccDDddEEeeFFggHHhh11"


def _post(apif, telegram, **over):
    api = apif(HUB_TELEGRAM_API_BASE=telegram.base)
    over.setdefault("telegram_token", TOKEN)
    return api.post(EP, addbot_payload(**over))


def _clean(configs_dir, channels_dir, bot_id="xiaoyu"):
    assert not (configs_dir / f"{bot_id}.yml").exists(), "校验失败却写了 configs/<bot>.yml"
    assert not (channels_dir / bot_id).exists(), "校验失败却建了 channels/<bot>/"


def test_加bot_body不是对象_400_bad_body(apif, telegram, configs_dir, channels_dir):
    api = apif(HUB_TELEGRAM_API_BASE=telegram.base)
    code, body = api.post(EP, [1, 2])
    assert code == 400, body
    assert body["error"] == "bad_body"
    _clean(configs_dir, channels_dir)


@pytest.mark.parametrize("missing", ["bot_id", "telegram_token", "dispatcher_port",
                                     "chat_id"])
def test_加bot_缺必填字段_400_bad_body(apif, telegram, configs_dir, channels_dir,
                                        port, missing):
    over = {"dispatcher_port": port, missing: OMIT}
    code, body = _post(apif, telegram, **over)
    assert code == 400, body
    assert body["error"] == "bad_body"
    _clean(configs_dir, channels_dir)


def test_加bot_人设两项都没给_400_bad_field(apif, telegram, configs_dir, channels_dir, port):
    code, body = _post(apif, telegram, dispatcher_port=port,
                       persona_text=OMIT, persona_template=OMIT)
    assert code == 400, body
    assert body["error"] == "bad_field"
    _clean(configs_dir, channels_dir)


def test_加bot_人设两项都给了_400_bad_field(apif, telegram, persona_template,
                                             configs_dir, channels_dir, port):
    code, body = _post(apif, telegram, dispatcher_port=port,
                       persona_text="正文", persona_template=persona_template)
    assert code == 400, body
    assert body["error"] == "bad_field"
    _clean(configs_dir, channels_dir)


def test_加bot_人设模板不存在_400_bad_field(apif, telegram, configs_dir, channels_dir, port):
    code, body = _post(apif, telegram, dispatcher_port=port,
                       persona_text=OMIT, persona_template="no_such_template")
    assert code == 400, body
    assert body["error"] == "bad_field"
    _clean(configs_dir, channels_dir)


def test_加bot_人设正文超过64KiB_413_payload_too_large(apif, telegram, port,
                                                       configs_dir, channels_dir):
    code, body = _post(apif, telegram, dispatcher_port=port,
                       persona_text="人" * (64 * 1024 + 8))
    assert code == 413, body
    assert body["error"] == "payload_too_large"
    _clean(configs_dir, channels_dir)


@pytest.mark.parametrize("bot_id", [
    pytest.param("Xiaoyu", id="大写开头"),
    pytest.param("_xiaoyu", id="下划线开头会被list_enabled_bots跳过"),
    pytest.param("1xiaoyu", id="数字开头"),
    pytest.param("a", id="只有一个字符"),
    pytest.param("a" * 33, id="超长33字符"),
    pytest.param("小雨", id="中文"),
    pytest.param("xiao-yu", id="含连字符"),
    pytest.param("xiao yu", id="含空格"),
    pytest.param("xiaoyu/../etc", id="含路径穿越"),
])
def test_加bot_botid不合正则_400_bad_field(apif, telegram, configs_dir, channels_dir,
                                            port, bot_id):
    code, body = _post(apif, telegram, bot_id=bot_id, dispatcher_port=port)
    assert code == 400, body
    assert body["error"] == "bad_field"
    _clean(configs_dir, channels_dir, bot_id)


@pytest.mark.parametrize("field,value", [
    pytest.param("bot_id", "", id="botid空串"),
    pytest.param("bot_id", "   ", id="botid纯空格"),
    pytest.param("bot_id", "\t\n", id="botid纯空白"),
    pytest.param("telegram_token", "", id="token空串"),
    pytest.param("telegram_token", "   ", id="token纯空格"),
    pytest.param("dispatcher_port", "", id="端口空串"),
    pytest.param("chat_id", "", id="chatid空串"),
])
def test_加bot_必填字段是空串或纯空白_400_bad_body且detail点名该字段(apif, telegram,
                                                                    configs_dir,
                                                                    channels_dir,
                                                                    port, field, value):
    """§13 A4 裁决：空串与纯空白 = 「缺必填」→ `bad_body`（**不是** `bad_field`），
    `detail` 含字段名；非空但不合形状才 `bad_field`。该口径对全部必填字段统一生效。"""
    over = {"dispatcher_port": port, field: value}
    code, body = _post(apif, telegram, **over)
    assert code == 400, body
    assert body["error"] == "bad_body", f"{field} 空值应按「缺必填」处理"
    assert field in body["detail"], f"detail 没点名字段：{body.get('detail')!r}"
    assert list(configs_dir.glob("*.yml")) == [], "校验失败却写了配置文件"
    assert list(channels_dir.iterdir()) == [], "校验失败却建了频道目录"


def test_加bot_botid已存在_409_bot_exists(apif, telegram, existing_bot, port,
                                          configs_dir):
    bot_id = existing_bot("chenlulu", port=17801)
    before = (configs_dir / f"{bot_id}.yml").read_bytes()
    code, body = _post(apif, telegram, bot_id=bot_id, dispatcher_port=port)
    assert code == 409, body
    assert body["error"] == "bot_exists"
    assert (configs_dir / f"{bot_id}.yml").read_bytes() == before, "重名请求覆盖了既有 bot"


@pytest.mark.parametrize("bad_port", [
    pytest.param(1023, id="小于1024"),
    pytest.param(65536, id="大于65535"),
    pytest.param(0, id="零"),
    pytest.param(-1, id="负数"),
    pytest.param("17802", id="字符串"),
    pytest.param(17802.0, id="浮点"),
    pytest.param(None, id="null"),
])
def test_加bot_端口非法_400_bad_field(apif, telegram, configs_dir, channels_dir, bad_port):
    code, body = _post(apif, telegram, dispatcher_port=bad_port)
    assert code == 400, body
    assert body["error"] == "bad_field"
    _clean(configs_dir, channels_dir)


def test_加bot_端口被注册表里别的bot占_409_bot_exists且指出占用者(apif, telegram,
                                                                  existing_bot,
                                                                  configs_dir,
                                                                  channels_dir):
    existing_bot("chenlulu", port=17801)
    code, body = _post(apif, telegram, dispatcher_port=17801)
    assert code == 409, body
    assert body["error"] == "bot_exists"
    assert "chenlulu" in body["detail"]
    _clean(configs_dir, channels_dir)


def test_加bot_端口被本机其它进程监听_409_port_in_use(apif, telegram, held_port,
                                                      configs_dir, channels_dir):
    """R6-4：这与 bot 存在与否无关，回 bot_exists 会让用户去找一个不存在的同名 bot。"""
    code, body = _post(apif, telegram, dispatcher_port=held_port)
    assert code == 409, body
    assert body["error"] == "port_in_use"
    _clean(configs_dir, channels_dir)


@pytest.mark.parametrize("name,why", [
    pytest.param(OMIT, "缺失", id="缺失"),
    pytest.param(123, "非字符串", id="非字符串"),
    pytest.param("名" * 65, "超过64字", id="超长65字"),
    pytest.param("小\x00雨", "含控制字符", id="含NUL"),
    pytest.param("小\n雨", "含换行", id="含换行"),
])
def test_加bot_显示名非法_400_bad_field(apif, telegram, configs_dir, channels_dir,
                                        port, name, why):
    """R8-1：display_name 会进 yml、进页面渲染、可能进 plist Label。"""
    code, body = _post(apif, telegram, dispatcher_port=port, display_name=name)
    assert code == 400, body
    assert body["error"] in ("bad_field", "bad_body"), (why, body)
    _clean(configs_dir, channels_dir)


@pytest.mark.parametrize("chat_id", ["123", 12.5, None, [1]])
def test_加bot_chatid非整数_400_bad_field(apif, telegram, configs_dir, channels_dir,
                                          port, chat_id):
    code, body = _post(apif, telegram, dispatcher_port=port, chat_id=chat_id)
    assert code == 400, body
    assert body["error"] in ("bad_field", "bad_body")
    _clean(configs_dir, channels_dir)


def test_加bot_getMe失败_200且ok为false且磁盘无任何产物(apif, telegram, port,
                                                        configs_dir, channels_dir):
    """§4.2 最后一行 + §4.3 用例②：业务失败不用 4xx，且不落盘。"""
    telegram.mode = "401"
    code, body = _post(apif, telegram, dispatcher_port=port)
    assert code == 200, body
    assert body["ok"] is False
    assert body["stage"] == "invalid_token"
    _clean(configs_dir, channels_dir)
    assert list(channels_dir.iterdir()) == []


def test_加bot_token已被别的bot使用_409_token_in_use(apif, telegram, port, port2,
                                                     configs_dir, channels_dir):
    """R1：两个 dispatcher 对同一 Telegram bot long-poll → 用户消息静默丢一半。
    判定依据是 getMe 回的 bot_id（桩固定 123456789，与 token 前段一致）。"""
    telegram.mode = "ok"
    api = apif(HUB_TELEGRAM_API_BASE=telegram.base)
    first = api.post(EP, addbot_payload(bot_id="first_bot", telegram_token=TOKEN,
                                        dispatcher_port=port))
    assert first[0] in (201, 207), first
    other_token = "123456789:test-ZZzzYYyyXXxxWWwwVVvvUUuuTTss22"
    code, body = api.post(EP, addbot_payload(bot_id="second_bot",
                                             telegram_token=other_token,
                                             dispatcher_port=port2))
    assert code == 409, body
    assert body["error"] == "token_in_use"
    assert not (configs_dir / "second_bot.yml").exists()
    assert not (channels_dir / "second_bot").exists()


def test_加bot_基址是明文http非回环_200_blocked_base且不落盘(apif, telegram, port,
                                                              configs_dir, channels_dir):
    """§4.1 硬约束 1 末句 / §13 A1：§4.2 A 段的 getMe 步命中拒发时**返回同一形状且不落盘**。"""
    api = apif(HUB_TELEGRAM_API_BASE="http://evil.example")
    code, body = api.post(EP, addbot_payload(telegram_token=TOKEN,
                                             dispatcher_port=port))
    assert code == 200, body
    assert body["ok"] is False
    assert body["stage"] == "blocked_base"
    assert telegram.hits == [], "拒发却发出了网络请求"
    _clean(configs_dir, channels_dir)


def test_模板列表_返回目录名与空白模板(api, persona_template, channels_dir):
    (channels_dir / "_persona_template").mkdir(exist_ok=True)
    code, body = api.get("/hub/api/bots/templates")
    assert code == 200, body
    ids = {t["id"] for t in body["templates"]}
    assert "chenlulu" in ids and "_persona_template" in ids
    assert all("display_name" in t for t in body["templates"])


def test_模板列表_频道目录读不到时200空列表(make_client, tmp_path):
    """§7：模板扫描也经 `HUB_CHANNELS_DIR`（§13 A2）—— 指到不存在的目录是正常态，不是错误。"""
    c, _ = make_client(HUB_CHANNELS_DIR=str(tmp_path / "no-such-channels"))
    r = c.get("/hub/api/bots/templates")
    assert r.status_code == 200
    assert r.get_json() == {"templates": []}
