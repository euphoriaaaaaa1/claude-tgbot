# -*- coding: utf-8 -*-
"""INTERFACE §3.3 校验表逐行 + §3.0c haiku 双 alias。纯函数层，不起服务。"""
import pytest

from conftest import payload, no_dns
from moments.provider_model import (DEFAULT_HAIKU_ALIAS, HubError, SUPPORTED_KINDS,
                                    validate_provider_payload)


def v(body, **kw):
    kw.setdefault("resolve", no_dns)
    return validate_provider_payload(body, **kw)


def err(body, **kw):
    with pytest.raises(HubError) as e:
        v(body, **kw)
    return e.value


def test_合法请求体返回归一化字段():
    got = v(payload())
    assert got["kind"] == "openai"
    assert got["haiku_alias"] == DEFAULT_HAIKU_ALIAS      # §3.0c 省略时服务端填默认
    assert got["api_key"] == "sk-test-0000000000"


@pytest.mark.parametrize("kind", ["openai", "anthropic", "gemini"])
def test_三个kind全部可建(kind):
    """§3.0：supported_kinds 恒为三个，anthropic 恒可建（裁决①）。"""
    assert kind in SUPPORTED_KINDS
    assert v(payload(kind=kind))["kind"] == kind


@pytest.mark.parametrize("body", [[], "s", None, 123, ("a",)])
def test_请求体不是对象_bad_body(body):
    assert err(body).error == "bad_body"
    assert err(body).status == 400


@pytest.mark.parametrize("kind", ["anthropic_native", "openai_compatible", "", "OPENAI",
                                  "gemini_pro", None, 123])
def test_非法kind_bad_kind(kind):
    assert err(payload(kind=kind)).error == "bad_kind"


@pytest.mark.parametrize("label", ["", "x" * 41, "中" * 41, "   ", None, 5])
def test_label边界_bad_label(label):
    assert err(payload(label=label)).error == "bad_label"


@pytest.mark.parametrize("label", ["x" * 40, "中" * 40, "DeepSeek 深度求索 🐋"])
def test_label边界内合法_按字符数不按字节(label):
    assert v(payload(label=label))["label"] == label


@pytest.mark.parametrize("key", ["", None, "   ", 123])
def test_key为空_key_required(key):
    assert err(payload(api_key=key)).error == "key_required"


@pytest.mark.parametrize("url", ["api.deepseek.com/v1", "ftp://x.example.com/", "",
                                 "javascript:alert(1)", "/v1", "http://",
                                 "file:///etc/passwd", None, "https://h:99999/v1"])
def test_base_url必须是绝对http_url(url):
    assert err(payload(base_url=url)).error == "bad_base_url"


@pytest.mark.parametrize("alias", ["", "a/b", "has space", "\t", "claude sonnet",
                                   "org/claude-sonnet", None])
def test_model_alias非法_bad_alias(alias):
    assert err(payload(model_alias=alias)).error == "bad_alias"


@pytest.mark.parametrize("m", ["", None, "  "])
def test_upstream_model为空_bad_upstream_model(m):
    assert err(payload(upstream_model=m)).error == "bad_upstream_model"


def test_upstream_model允许含斜杠():
    """上游真实模型名常带斜杠（moonshotai/kimi-k2:free），只有 alias 才禁斜杠。"""
    assert v(payload(upstream_model="moonshotai/kimi-k2:free"))["upstream_model"]


# ---------------- §3.0c：haiku 双 alias ----------------

@pytest.mark.parametrize("bad", ["", "a/b", "has space", "\t", "   "])
def test_haiku_alias五种非法值_bad_haiku_alias(bad):
    assert err(payload(haiku_alias=bad)).error == "bad_haiku_alias"


def test_haiku_alias等于主alias_bad_haiku_alias():
    """同一条目里两条 model 撞名 = 两条一模一样的映射。"""
    assert err(payload(haiku_alias="claude-sonnet-4-5-20250929")).error == "bad_haiku_alias"


def test_haiku_alias显式传null也算非法():
    assert err(payload(haiku_alias=None)).error == "bad_haiku_alias"


def test_自定义haiku_alias被采用():
    assert v(payload(haiku_alias="claude-3-5-haiku-latest"))["haiku_alias"] == \
        "claude-3-5-haiku-latest"


# ---------------- 校验顺序（§3.3「先命中先返回」）----------------

def test_kind先于label被命中():
    assert err(payload(kind="nope", label="")).error == "bad_kind"


def test_label先于key被命中():
    assert err(payload(label="", api_key="")).error == "bad_label"


def test_key先于base_url被命中():
    assert err(payload(api_key="", base_url="不是url")).error == "key_required"


def test_base_url先于alias被命中():
    assert err(payload(base_url="不是url", model_alias="a/b")).error == "bad_base_url"


def test_alias先于upstream被命中():
    assert err(payload(model_alias="a/b", upstream_model="")).error == "bad_alias"


def test_upstream先于haiku被命中():
    assert err(payload(upstream_model="", haiku_alias="a/b")).error == "bad_upstream_model"


# ---------------- §3.4 PATCH：请求体是子集，base 补齐 ----------------

def _base():
    b = dict(payload())
    b["haiku_alias"] = DEFAULT_HAIKU_ALIAS
    return b


def test_patch省略api_key保持原key():
    got = v({"label": "改个名"}, base=_base())
    assert got["api_key"] == "sk-test-0000000000"
    assert got["label"] == "改个名"
    assert got["base_url"] == "https://api.deepseek.com/v1"


def test_patch传空api_key仍是key_required():
    assert err({"api_key": ""}, base=_base()).error == "key_required"


def test_patch只改haiku_alias其余不变():
    got = v({"haiku_alias": "claude-3-5-haiku-latest"}, base=_base())
    assert got["haiku_alias"] == "claude-3-5-haiku-latest"
    assert got["model_alias"] == "claude-sonnet-4-5-20250929"


def test_patch的字段照样过校验():
    assert err({"model_alias": "a b"}, base=_base()).error == "bad_alias"


def test_base里haiku为None时回落默认值():
    """用户手写的单 model 条目读出来 haiku_alias 是 None，PATCH 时补默认而不是报错。"""
    b = _base()
    b["haiku_alias"] = None
    assert v({"label": "x"}, base=b)["haiku_alias"] == DEFAULT_HAIKU_ALIAS
