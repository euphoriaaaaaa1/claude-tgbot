# -*- coding: utf-8 -*-
"""pub-draft 生活设定 HTTP 验收：B1/B2/B3/B4/B26/B29/B30。

只经管理台公开 HTTP 路由 + 真实 claude 适配器；配置全在临时目录。
"""
import re

import pytest

import pd_helpers as H
from conftest import BOT, write_life_cfg  # noqa: F401

PROACTIVE_EMPTY_HINT = "人设摘要或「什么情况下主动说话」没填时，bot 会很少主动找你。"
SLEEP_EMPTY_HINT = "睡觉时段没填时，bot 夜里也可能主动找你。"
PASSWORD = "pub-draft-test-pw-12345"


def _bot_url(bot=BOT, tail="life"):
    return "/api/console/bots/claude/%s/%s" % (bot, tail)


@pytest.fixture
def client(pd_env, monkeypatch):
    from adapters.claude_local import ClaudeLocalAdapter
    from adapters import registry
    from moments import panel_auth, web
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(pd_env["root"] / "settings.json"))
    monkeypatch.setenv("HUB_ENV_FILE", str(pd_env["root"] / "hub.env"))
    for k in ("CLIPROXY_PORT", "CLIPROXY_MGMT_KEY", "CLIPROXY_API_KEY", "MOMENTS_WEB_HOST"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(panel_auth, "SECRET_PATH", str(pd_env["secrets"] / "panel.env"))
    monkeypatch.setattr(panel_auth, "SESSIONS_PATH", str(pd_env["secrets"] / "sessions.json"))
    panel_auth._fail.clear()
    adapter = ClaudeLocalAdapter()
    monkeypatch.setattr(registry, "enabled_backends", lambda: ["claude"])
    monkeypatch.setattr(registry, "adapter", lambda name: adapter)
    monkeypatch.setattr(registry, "label", lambda name: "Claude")
    panel_auth.set_password(PASSWORD)
    web.app.config["TESTING"] = True
    c = web.app.test_client()
    assert c.post("/login", data={"password": PASSWORD, "next": "/"}).status_code == 302
    yield c
    panel_auth._fail.clear()


def _fields_by_key(client, bot=BOT):
    r = client.get(_bot_url(bot))
    assert r.status_code == 200, r.get_data(as_text=True)
    fields = r.get_json()["fields"]
    return fields, {f["key"]: f for f in fields}


def test_B1_B26_字段表顺序类型上限与提示文案(client, pd_env):
    write_life_cfg(pd_env["configs"], BOT, "id: %s\ndispatcher_port: 59999\nbio: 旧简介\n" % BOT)
    fields, by_key = _fields_by_key(client)
    keys = [f["key"] for f in fields]
    assert "jiwen_delta_hints" in keys and "speaking_threshold" in keys
    assert keys.index("jiwen_delta_hints") == keys.index("speaking_threshold") + 1, keys
    assert by_key["jiwen_delta_hints"]["type"] == "textarea"
    assert by_key["jiwen_delta_hints"]["max_len"] == 4000
    assert by_key["persona_summary"]["empty_hint"] == PROACTIVE_EMPTY_HINT
    assert by_key["speaking_threshold"]["empty_hint"] == PROACTIVE_EMPTY_HINT
    assert by_key["sleep_hours"]["empty_hint"] == SLEEP_EMPTY_HINT
    assert "empty_hint" not in by_key["jiwen_delta_hints"]
    assert "empty_hint" not in by_key["bio"]
    assert client.get(_bot_url(bot="ghost_bot")).status_code == 404


NESTED_BODY = """id: pd_bot
chat_id: "10001"
dispatcher_port: 59999
bio: 旧简介
jiwen:
  # 积温注释必须保留
  delta_hints: 旧规则
  rates_override:
    connection_per_min: 0.1
    pride_decay_per_min: 0.002
  x_unknown: keep-me
speaking_threshold: 旧阈值
"""


def _strip_top_block(text, key):
    """删掉某个顶层键的整块，返回其余文本（用于'其他顶层键不变'断言）。"""
    out, skipping = [], False
    for line in text.splitlines():
        if re.match(r"^%s\s*:" % re.escape(key), line):
            skipping = True
            continue
        if skipping and re.match(r"^[^\s#-]", line):
            skipping = False
        if not skipping:
            out.append(line)
    return "\n".join(out)


def test_B2_B29_嵌套写保留注释未知子键与其他顶层键(client, pd_env):
    path = write_life_cfg(pd_env["configs"], BOT, NESTED_BODY)
    before = H.load_yaml(path)
    other_before = _strip_top_block(path.read_text(encoding="utf-8"), "jiwen")

    r = client.put(_bot_url(), json={"values": {"jiwen_delta_hints": "新规则"}})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["applied"] is True
    text = path.read_text(encoding="utf-8")
    after = H.load_yaml(path)
    assert after["jiwen"]["delta_hints"] == "新规则"
    assert after["jiwen"]["rates_override"] == before["jiwen"]["rates_override"]
    assert after["jiwen"]["x_unknown"] == "keep-me"
    assert "# 积温注释必须保留" in text
    assert _strip_top_block(text, "jiwen") == other_before

    # B29：未知键拒绝
    good_hash = H.sha256(path)
    r = client.put(_bot_url(), json={"values": {"jiwen_rates_override": {}}})
    assert r.status_code == 422
    body = r.get_json()
    assert body["code"] == "invalid"
    assert "jiwen_rates_override" in body["fields"]
    assert H.sha256(path) == good_hash


@pytest.mark.parametrize("bad", ["x" * 4001, 12345], ids=["4001字", "整数"])
def test_B3_非法值422且配置不变(client, pd_env, bad):
    path = write_life_cfg(pd_env["configs"], BOT,
                          "id: %s\ndispatcher_port: 59999\njiwen:\n  delta_hints: 旧\n" % BOT)
    before = H.sha256(path)
    r = client.put(_bot_url(), json={"values": {"jiwen_delta_hints": bad}})
    assert r.status_code == 422, r.get_data(as_text=True)
    assert r.get_json()["code"] == "invalid"
    assert H.sha256(path) == before


@pytest.mark.parametrize("body,expected", [
    ("id: pd_bot\ndispatcher_port: 59999\njiwen:\n  delta_hints: 已有规则\n", "已有规则"),
    ("id: pd_bot\ndispatcher_port: 59999\njiwen:\n  rates_override:\n    connection_per_min: 0.1\n", ""),
], ids=["有值", "无值为空串"])
def test_B4_读回delta_hints(client, pd_env, body, expected):
    write_life_cfg(pd_env["configs"], BOT, body)
    r = client.get(_bot_url())
    assert r.status_code == 200
    assert r.get_json()["values"]["jiwen_delta_hints"] == expected


FLOW_BODY = """id: pd_bot
dispatcher_port: 59999
jiwen: {delta_hints: "旧", rates_override: {connection_per_min: 0.1}}
"""

FLOW_WITH_COMMENT = """id: pd_bot
dispatcher_port: 59999
# 这行注释让改写必须失败
jiwen: {delta_hints: "旧", rates_override: {connection_per_min: 0.1}}
"""


def test_B30_flow无注释可写_含注释回500且文件不变(client, pd_env):
    path = write_life_cfg(pd_env["configs"], BOT, FLOW_BODY)
    r = client.put(_bot_url(), json={"values": {"jiwen_delta_hints": "新"}})
    assert r.status_code == 200, r.get_data(as_text=True)
    after = H.load_yaml(path)
    assert after["jiwen"]["delta_hints"] == "新"
    assert after["jiwen"]["rates_override"] == {"connection_per_min": 0.1}

    path2 = write_life_cfg(pd_env["configs"], "pd_bot2", FLOW_WITH_COMMENT)
    before2 = H.sha256(path2)
    r = client.put(_bot_url(bot="pd_bot2"), json={"values": {"jiwen_delta_hints": "新"}})
    assert r.status_code == 500, r.get_data(as_text=True)
    assert H.sha256(path2) == before2
