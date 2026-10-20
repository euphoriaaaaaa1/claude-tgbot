# -*- coding: utf-8 -*-
"""base-url 推导（纯字符串函数，无网络、无副作用）—— PLAN §5.2 / INTERFACE §3.2。

实例的两个块**各自**会往「送入值」后面拼一个固定后缀，且拼接规则不同：

    openai-compatibility  →  <送入值> + "/chat/completions"
    claude-api-key        →  <送入值> + "/v1/messages"

而面板存的 ``base_url`` 是为「Claude Code 直连」设计的（CLI 自己拼 ``/v1/messages``）。
故两个块要各自推导一次「送入值」，推导规则见下（两条规则都只做「剥后缀」，
**不新增任何后缀** —— 重活由实例的拼接规则承担）。

规则（B = 面板 base_url 去尾 ``/``）：

    目标          规则
    openai_url    若 B 以 ``/anthropic`` 结尾 → 剥掉该后缀；否则 = B
    anthropic_url 若 B 以 ``/v1`` 结尾        → 剥掉该后缀；否则 = B

用户可用 provider 条目的 ``anthropic_base_url`` / ``openai_base_url`` 显式覆盖；
非空即**直接采用，不再推导**（见 ``effective_endpoints``）。
"""
from __future__ import annotations

from urllib.parse import urlsplit

_OPENAI_STRIP = "/anthropic"
_ANTHROPIC_STRIP = "/v1"


def _strip_trailing_slash(base_url: str) -> str:
    return base_url.rstrip("/")


def derive_openai_url(base_url: str) -> str:
    """``openai-compatibility`` 块的送入值：剥掉 ``/anthropic`` 后缀。"""
    b = _strip_trailing_slash(base_url or "")
    if b.endswith(_OPENAI_STRIP) and len(b) > len(_OPENAI_STRIP):
        return b[: -len(_OPENAI_STRIP)]
    return b


def derive_anthropic_url(base_url: str) -> str:
    """``claude-api-key`` 块的送入值：剥掉 ``/v1`` 后缀。"""
    b = _strip_trailing_slash(base_url or "")
    if b.endswith(_ANTHROPIC_STRIP) and len(b) > len(_ANTHROPIC_STRIP):
        return b[: -len(_ANTHROPIC_STRIP)]
    return b


def direct_anthropic_url(base_url):
    """直连时写给 bot 的接口地址（INTERFACE-provider-ux §1.1）。

    命令行会在接口地址后自己拼 ``/v1/messages``，所以地址以 ``/v1`` 结尾时要先剥掉，
    否则请求落到 ``/v1/v1/messages``。与 ``derive_anthropic_url`` 的两点不同：

    - **不命中时一个字节不动**（结尾 ``/`` 也保留）：不以 ``/v1`` 结尾的地址，写给 bot 的
      内容必须与加本函数之前逐字节相同；
    - **按路径判断**而不是按整串判断：``https://v1`` 的 ``v1`` 是主机名，不能剥。

    带查询串或片段的地址不处理（剥了会把后缀切坏）；入参不是字符串时原样返回。
    """
    if not isinstance(base_url, str):
        return base_url
    try:
        parts = urlsplit(base_url)
    except ValueError:  # 形如未闭合的 IPv6 括号：不是能推导的地址，原样交回
        return base_url
    if parts.query or parts.fragment:
        return base_url
    stripped = base_url.rstrip("/")
    # 两处都要以 /v1 结尾：路径（排除主机名恰为 v1）与原串（排除 "…/v1?" 这类空查询串写法）
    if parts.path.rstrip("/").endswith(_ANTHROPIC_STRIP) and stripped.endswith(_ANTHROPIC_STRIP):
        return stripped[: -len(_ANTHROPIC_STRIP)]
    return base_url


def effective_endpoints(base_url: str, anthropic_base_url: str = "",
                        openai_base_url: str = "") -> tuple[str, str]:
    """返回 ``(anthropic_url, openai_url)``。

    显式声明优先：对应的覆盖字段非空即**直接采用**（用户已看过上游文档，比推导准），
    不做推导；空串 = 未设 → 走推导。
    """
    a = anthropic_base_url.strip() if isinstance(anthropic_base_url, str) else ""
    o = openai_base_url.strip() if isinstance(openai_base_url, str) else ""
    return (
        _strip_trailing_slash(a) if a else derive_anthropic_url(base_url),
        _strip_trailing_slash(o) if o else derive_openai_url(base_url),
    )


# ── 自测（python3 scripts/derive_endpoints.py）────────────────────────────

def _selftest() -> None:
    # INTERFACE §3.2 的四行示例表
    cases = [
        ("https://api.deepseek.com/anthropic",
         "https://api.deepseek.com/anthropic", "https://api.deepseek.com"),
        ("https://api.commandcode.ai/provider/v1",
         "https://api.commandcode.ai/provider", "https://api.commandcode.ai/provider/v1"),
        ("https://open.bigmodel.cn/api/anthropic",
         "https://open.bigmodel.cn/api/anthropic", "https://open.bigmodel.cn/api"),
        ("https://api.example.com",
         "https://api.example.com", "https://api.example.com"),
    ]
    for b, want_a, want_o in cases:
        got_a, got_o = derive_anthropic_url(b), derive_openai_url(b)
        assert got_a == want_a, (b, got_a, want_a)
        assert got_o == want_o, (b, got_o, want_o)

    # 尾斜杠被剥掉
    assert derive_anthropic_url("https://a.example/v1/") == "https://a.example"
    assert derive_openai_url("https://a.example/anthropic/") == "https://a.example"

    # 后缀剥除后不得变成空串（整体恰等于后缀时不剥）
    assert derive_openai_url("/anthropic") == "/anthropic"
    assert derive_anthropic_url("/v1") == "/v1"

    # 只剥一次：/v1/v1 只掉最后一节
    assert derive_anthropic_url("https://a.example/v1/v1") == "https://a.example/v1"

    # 显式覆盖优先，且覆盖值也去尾斜杠
    assert effective_endpoints("https://a.example/v1", "https://x.example/", "") == \
        ("https://x.example", "https://a.example/v1")
    assert effective_endpoints("https://a.example/anthropic", "", "https://y.example") == \
        ("https://a.example/anthropic", "https://y.example")

    # 空输入不炸
    assert derive_anthropic_url("") == ""
    assert derive_openai_url(None or "") == ""

    # 直连地址（INTERFACE-provider-ux §1.1 的形态表）：命中才剥，不命中逐字节原样
    direct_cases = [
        ("https://a.example/v1", "https://a.example"),
        ("https://a.example/v1/", "https://a.example"),
        ("https://a.example/v1//", "https://a.example"),
        ("https://a.example/api/v1", "https://a.example/api"),
        ("http://127.0.0.1:8080/v1", "http://127.0.0.1:8080"),
        ("HTTPS://A.Example:8443/v1", "HTTPS://A.Example:8443"),
        ("https://[::1]:9/v1", "https://[::1]:9"),
        ("https://a.example/v1/v1", "https://a.example/v1"),
    ]
    for b, want in direct_cases:
        assert direct_anthropic_url(b) == want, (b, direct_anthropic_url(b), want)
    for b in ("https://a.example", "https://a.example/", "https://a.example/V1",
              "https://a.example/v1beta", "https://a.example/xv1", "https://a.example/anthropic",
              "https://a.example/v1/anthropic", "https://a.example/v1/chat",
              "https://a.example/v1?x=1", "https://a.example/v1#f", "https://a.example/v1?",
              "https://v1", "https://v1/", "https://[::1/v1", ""):
        assert direct_anthropic_url(b) == b, (b, direct_anthropic_url(b))
    for b in (None, 123, ["https://a.example/v1"]):
        assert direct_anthropic_url(b) is b

    print("derive_endpoints selftest OK")


if __name__ == "__main__":
    _selftest()
