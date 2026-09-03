# -*- coding: utf-8 -*-
"""§3.6 A2 解析 / 两形态落键计算 / §3.2 active 五态。全是纯计算，不落盘。"""
import os

import pytest

from moments.provider_model import (ANTHROPIC_ENV_KEYS, HubError, apply_cliproxy_env,
                                    apply_native_env, parse_settings, resolve_active,
                                    settings_path)

ALIAS = "claude-sonnet-4-5-20250929"
PORT = 8317


def test_settings_path优先取环境变量_口径与watcher一致(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(tmp_path / "s.json"))
    assert settings_path() == str(tmp_path / "s.json")


def test_settings_path缺省回落家目录(monkeypatch):
    """只比字符串，不读文件——绝不碰真实 ~/.claude/settings.json。"""
    monkeypatch.delenv("CLAUDE_SETTINGS_PATH", raising=False)
    assert settings_path() == os.path.join(os.path.expanduser("~"), ".claude",
                                           "settings.json")


@pytest.mark.parametrize("text", [None, "", "   ", "{}"])
def test_文件不存在或空视为空对象(text):
    assert parse_settings(text) == {}


@pytest.mark.parametrize("raw", ["{ 坏掉的", '"a string"', "[]", "123", "null",
                                 '{"env": "x"}', '{"env": 3}', '{"env": null}',
                                 '{"env": []}'])
def test_不可解析一律409_settings_unparsable(raw):
    with pytest.raises(HubError) as e:
        parse_settings(raw)
    assert (e.value.status, e.value.error) == (409, "settings_unparsable")


def test_合法settings原样返回():
    got = parse_settings('{"model": "sonnet", "env": {"K": "1"}}')
    assert got == {"model": "sonnet", "env": {"K": "1"}}


# ---------------- §3.6 形态一：cliproxy 三键 ----------------

def test_cliproxy形态写三键并删API_KEY():
    got = apply_cliproxy_env({"env": {"ANTHROPIC_API_KEY": "sk-old"}}, PORT, "dk", ALIAS)
    env = got["env"]
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8317"   # 不带 /v1
    assert env["ANTHROPIC_AUTH_TOKEN"] == "dk"
    assert env["ANTHROPIC_MODEL"] == ALIAS
    assert "ANTHROPIC_API_KEY" not in env


def test_其余键与顶层model原样保留():
    src = {"model": "sonnet", "permissions": {"allow": ["Bash"]}, "env": {"KEEP": "1"}}
    got = apply_cliproxy_env(src, PORT, "dk", ALIAS)
    assert got["model"] == "sonnet"
    assert got["permissions"] == {"allow": ["Bash"]}
    assert got["env"]["KEEP"] == "1"


def test_入参不被就地改写():
    """纯函数：C 阶段落盘失败时，调用方手上还必须是原文。"""
    src = {"env": {"KEEP": "1"}}
    apply_cliproxy_env(src, PORT, "dk", ALIAS)
    assert src == {"env": {"KEEP": "1"}}


def test_空settings与缺env都能建出来():
    assert apply_cliproxy_env({}, PORT, "dk", ALIAS)["env"]["ANTHROPIC_MODEL"] == ALIAS
    assert apply_cliproxy_env(None, PORT, "dk", ALIAS)["env"]["ANTHROPIC_MODEL"] == ALIAS


def test_重复算两次结果一致_幂等的前提():
    once = apply_cliproxy_env({"env": {"KEEP": "1"}}, PORT, "dk", ALIAS)
    assert apply_cliproxy_env(once, PORT, "dk", ALIAS) == once


# ---------------- §5 形态三：四键全删 ----------------

def test_原生形态四键全删其余保留():
    src = {"model": "sonnet", "env": dict({k: "x" for k in ANTHROPIC_ENV_KEYS},
                                          OTHER_KEY="keep")}
    got = apply_native_env(src)
    assert all(k not in got["env"] for k in ANTHROPIC_ENV_KEYS)
    assert got["env"]["OTHER_KEY"] == "keep"
    assert got["model"] == "sonnet"
    assert src["env"]["ANTHROPIC_MODEL"] == "x"      # 入参未被改写


def test_原生形态幂等():
    once = apply_native_env({"env": {"ANTHROPIC_MODEL": "x"}})
    assert apply_native_env(once) == once == {"env": {}}


# ---------------- §3.2 active 五态（+ 两种漂移态）----------------

def _prov(pid="a3f9c2e1", alias=ALIAS, disabled=False):
    return {"id": pid, "model_alias": alias, "haiku_alias": "claude-3-5-haiku-20241022",
            "disabled": disabled}


def act(settings, providers=(), port=PORT):
    return resolve_active(settings, port, list(providers))


def test_四键全缺是claude_native():
    a, w = act({"model": "sonnet"})
    assert (a["kind"], a["id"], a["alias"], w) == ("claude_native", None, None, [])


def test_alias恒取ANTHROPIC_MODEL原值_none态也回():
    """页面要显示"现在钉在哪个模型名上"，判不出来源不等于读不出名字。"""
    s = {"env": {"ANTHROPIC_BASE_URL": "https://other.example.com",
                 "ANTHROPIC_MODEL": "gpt-5-turbo"}}
    a, w = act(s, [_prov()])
    assert (a["kind"], a["id"], a["alias"]) == ("none", None, "gpt-5-turbo")


def test_不可解析是unknown并给warning():
    a, w = act(None)
    assert (a["kind"], a["id"]) == ("unknown", None)
    assert w == ["settings_unparsable"]


def test_指向cliproxy且alias有enabled条目():
    s = {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "ANTHROPIC_MODEL": ALIAS}}
    a, w = act(s, [_prov()])
    assert (a["kind"], a["id"], a["alias"], w) == ("cliproxy", "a3f9c2e1", ALIAS, [])


def test_alias存在但被disabled给alias_disabled():
    s = {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "ANTHROPIC_MODEL": ALIAS}}
    a, w = act(s, [_prov(disabled=True)])
    assert (a["kind"], a["id"], w) == ("cliproxy", "a3f9c2e1", ["alias_disabled"])


def test_alias根本不存在给active_unknown():
    s = {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317",
                 "ANTHROPIC_MODEL": "没人注册过的名字"}}
    a, w = act(s, [_prov()])
    assert (a["kind"], a["id"], w) == ("cliproxy", None, ["active_unknown"])


def test_同alias有enabled也有disabled时取enabled那条():
    s = {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "ANTHROPIC_MODEL": ALIAS}}
    a, w = act(s, [_prov("dead0000", disabled=True), _prov("live1111")])
    assert (a["id"], w) == ("live1111", [])


def test_用户手写的第三方直连是none():
    s = {"env": {"ANTHROPIC_BASE_URL": "https://other.example.com",
                 "ANTHROPIC_MODEL": ALIAS}}
    a, w = act(s, [_prov()])
    assert (a["kind"], a["id"], a["alias"], w) == ("none", None, ALIAS, [])


@pytest.mark.parametrize("base", ["http://localhost:8317", "http://127.0.0.1:8317/",
                                  "http://[::1]:8317"])
def test_本机cliproxy的几种写法都算cliproxy(base):
    s = {"env": {"ANTHROPIC_BASE_URL": base, "ANTHROPIC_MODEL": ALIAS}}
    assert act(s, [_prov()])[0]["kind"] == "cliproxy"


@pytest.mark.parametrize("base", ["http://127.0.0.1:9999", "http://127.0.0.1",
                                  "不是url", "", "http://192.168.1.9:8317"])
def test_端口或主机对不上就不算cliproxy(base):
    s = {"env": {"ANTHROPIC_BASE_URL": base, "ANTHROPIC_MODEL": ALIAS}}
    assert act(s, [_prov()])[0]["kind"] == "none"


def test_cliproxy没配时port为None也不炸():
    """§0.3：三键缺失时列表端点仍要 200，active 判定不能因为 port=None 抛异常。"""
    s = {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "ANTHROPIC_MODEL": ALIAS}}
    assert act(s, [_prov()], port=None)[0]["kind"] == "none"


def test_只写了AUTH_TOKEN一个键也不算native():
    """四键"全"缺才是 native；剩一个说明用户或别的工具动过，如实报 none。"""
    a, w = act({"env": {"ANTHROPIC_AUTH_TOKEN": "x"}})
    assert (a["kind"], w) == ("none", [])


@pytest.mark.parametrize("bad", [123, 4.5, True, {"a": 1}, ["x"], None])
def test_MODEL不是字符串时alias归null(bad):
    """BUG-13：alias 要往响应里回、还要拿去比对 provider，非字符串一律当没写。"""
    s = {"env": {"ANTHROPIC_BASE_URL": "https://other.example.com", "ANTHROPIC_MODEL": bad}}
    assert act(s, [_prov()])[0]["alias"] is None
