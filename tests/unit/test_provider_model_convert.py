# -*- coding: utf-8 -*-
"""§3.0 KIND_SPEC / §3.1 id 派生与对象形状 / §3.0c 两条 alias / cliproxy↔门户互转。"""
import hashlib

import pytest

from conftest import payload, no_dns
from moments.provider_model import (DEFAULT_HAIKU_ALIAS, KIND_SPEC, ROUTE, alias_occupied,
                                    derive_id, entry_api_key, from_block_entry, mask_key,
                                    provider_id, to_block_entry, validate_provider_payload)

ALIAS = "claude-sonnet-4-5-20250929"


def norm(**over):
    return validate_provider_payload(payload(**over), resolve=no_dns)


def test_三个kind的承载块名与S0实测一致():
    """⑨：anthropic-compatibility / gemini-compatibility 在 v7 不存在（管理 API 实测 404）。"""
    assert KIND_SPEC["openai"]["block"] == "openai-compatibility"
    assert KIND_SPEC["anthropic"]["block"] == "claude-api-key"
    assert KIND_SPEC["gemini"]["block"] == "gemini-api-key"
    blocks = {s["block"] for s in KIND_SPEC.values()}
    assert not blocks & {"anthropic-compatibility", "gemini-compatibility",
                         "anthropic-api-key"}


def test_id是四元组sha1前8位():
    want = hashlib.sha1((
        "openai-compatibility|https://api.deepseek.com/v1|deepseek-v4-flash|%s"
        % ALIAS).encode("utf-8")).hexdigest()[:8]
    assert provider_id(norm()) == want
    assert len(want) == 8


def test_改haiku或label不换id():
    a = provider_id(norm())
    assert provider_id(norm(haiku_alias="claude-3-5-haiku-latest")) == a
    assert provider_id(norm(label="改个名")) == a


@pytest.mark.parametrize("over", [{"base_url": "https://gw.example.com/v1"},
                                  {"upstream_model": "glm-4.6"},
                                  {"model_alias": "claude-opus-4-5-20251101"},
                                  {"kind": "anthropic"}])
def test_四元组任一维变化就换id(over):
    assert provider_id(norm(**over)) != provider_id(norm())


def test_落地条目必须两条models且name相同():
    """§3.0c：少注册一条，claude CLI 的后台摘要请求就打到别的后端或 502。"""
    models = to_block_entry(norm())["models"]
    assert len(models) == 2
    assert models[0]["name"] == models[1]["name"] == "deepseek-v4-flash"
    assert [m["alias"] for m in models] == [ALIAS, DEFAULT_HAIKU_ALIAS]
    assert all(m["force-mapping"] is True for m in models)
    assert all(m["display-name"] == "DeepSeek" for m in models)


def test_openai块的key在api_key_entries数组里():
    e = to_block_entry(norm())
    assert e["api-key-entries"] == [{"api-key": "sk-test-0000000000"}]
    assert "api-key" not in e
    assert e["name"].startswith("hub-")          # cliproxy 会小写它当内部 provider key


@pytest.mark.parametrize("kind", ["anthropic", "gemini"])
def test_claude与gemini块的key是标量字段(kind):
    e = to_block_entry(norm(kind=kind))
    assert e["api-key"] == "sk-test-0000000000"
    assert "api-key-entries" not in e
    assert "name" not in e                       # 这两个块没有 name 字段（config.example.yaml）


@pytest.mark.parametrize("kind", ["openai", "anthropic", "gemini"])
def test_落选条目靠prefix停用_当选条目prefix为空(kind):
    """§3.0d：三块统一走 prefix，落选值 = 自己的 id。"""
    p = norm(kind=kind)
    assert to_block_entry(p, disabled=True)["prefix"] == provider_id(p)
    assert to_block_entry(p)["prefix"] == ""


@pytest.mark.parametrize("kind", ["openai", "anthropic", "gemini"])
def test_绝不往cliproxy写disabled键(kind):
    """disabled 只在 openai-compatibility 有，另两块会静默忽略 → 互斥失效、请求在两后端轮询。"""
    for d in (True, False):
        assert "disabled" not in to_block_entry(norm(kind=kind), disabled=d)


def test_带prefix的条目读回来就是disabled():
    e = to_block_entry(norm(), disabled=True)
    assert from_block_entry("openai", e)["disabled"] is True
    assert from_block_entry("openai", to_block_entry(norm()))["disabled"] is False


def test_手写条目的原生disabled照样认():
    """用户自己在 openai-compatibility 里写了 disabled:true —— cliproxy 真的不路由它，
    报成 enabled 会让 §3.2 的 active 判定说谎。"""
    e = to_block_entry(norm())
    e["disabled"] = True
    assert from_block_entry("openai", e)["disabled"] is True


@pytest.mark.parametrize("kind", ["openai", "anthropic", "gemini"])
def test_互转往返不掉字段也不换id(kind):
    p = norm(kind=kind)
    back = from_block_entry(kind, to_block_entry(p))
    assert back["id"] == provider_id(p)
    for k in ("label", "kind", "base_url", "upstream_model", "model_alias", "haiku_alias"):
        assert back[k] == p[k], k
    assert "api_key" not in back                 # §3.1：明文永不出现在响应体里
    assert back["key_masked"] == "****0000"
    assert back["route"] == ROUTE == "cliproxy"
    assert back["active"] is False


def test_读用户手写的单model条目_haiku为None():
    """页面据此提示"补一条 haiku 映射"，而不是假装它已经注册好了。"""
    entry = {"base-url": "https://gw.example.com", "api-key": "sk-abcd1234",
             "models": [{"name": "glm-4.6", "alias": ALIAS}]}
    p = from_block_entry("anthropic", entry)
    assert p["haiku_alias"] is None
    assert p["model_alias"] == ALIAS
    assert p["label"] == ALIAS                  # 没有 display-name 就回落 alias
    assert p["key_masked"] == "****1234"


def test_alias为空时回落用name_与cliproxy口径一致():
    """service.go:958-963：alias 为空则对外用 name。id 派生必须跟着同一口径。"""
    entry = {"base-url": "https://gw.example.com", "api-key": "k",
             "models": [{"name": "glm-4.6"}]}
    p = from_block_entry("anthropic", entry)
    assert p["model_alias"] == "glm-4.6"
    assert p["id"] == derive_id("claude-api-key", "https://gw.example.com",
                                "glm-4.6", "glm-4.6")


def test_空条目不炸():
    p = from_block_entry("openai", {})
    assert p["upstream_model"] == "" and p["key_masked"] == ""


@pytest.mark.parametrize("key,masked", [("", ""), ("ab", "****"), ("abcd", "****abcd"),
                                        ("sk-test-9f3a", "****9f3a"), (None, "")])
def test_脱敏恒是四星加末4位(key, masked):
    assert mask_key(key) == masked


def test_取明文key只在需要时发生():
    assert entry_api_key("openai", {"api-key-entries": [{"api-key": "sk-x"}]}) == "sk-x"
    assert entry_api_key("openai", {"api-key-entries": []}) == ""
    assert entry_api_key("anthropic", {"api-key": "sk-y"}) == "sk-y"
    assert entry_api_key("gemini", {}) == ""


# ---------------- §3.3：alias 被 enabled 条目占用 → 新建即 disabled ----------------

def _p(pid, alias=ALIAS, haiku=DEFAULT_HAIKU_ALIAS, disabled=False):
    return {"id": pid, "model_alias": alias, "haiku_alias": haiku, "disabled": disabled}


def test_主alias被别的enabled条目占用():
    assert alias_occupied([_p("aaaa1111")], ALIAS) is True


def test_只被disabled条目占用不算占用():
    assert alias_occupied([_p("aaaa1111", disabled=True)], ALIAS) is False


def test_空台账不算占用():
    assert alias_occupied([], ALIAS) is False
    assert alias_occupied([_p("aaaa1111")], None) is False


def test_排除自己后不算占用_幂等activate要用():
    assert alias_occupied([_p("aaaa1111")], ALIAS, exclude_id="aaaa1111") is False


def test_haiku名撞车也能被查出来():
    """默认 haiku 名人人相同：调用方把 haiku 也传进来，就能保证同一时刻只一个 enabled。"""
    other = _p("bbbb2222", alias="claude-opus-4-5-20251101")
    assert alias_occupied([other], ALIAS) is False
    assert alias_occupied([other], [ALIAS, DEFAULT_HAIKU_ALIAS]) is True
