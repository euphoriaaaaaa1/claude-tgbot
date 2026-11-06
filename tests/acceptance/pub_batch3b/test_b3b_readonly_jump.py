# -*- coding: utf-8 -*-
"""pub-batch3b · P9/P10：迁出行的「去看在跑的那份」跳转。

只经浏览器观察 DOM 与点击后的 URL/详情。
"""
import pytest

from b3b_helpers import (  # noqa: F401  夹具
    backend, base_url, bot, chrome, jump_visible, no_prod_port_connect, open_console, overview,
)

BACKENDS = [backend("claude", "Claude"), backend("dsh", "dsh")]


def _data(extra_bots=None, moved_person="p-1"):
    bots = [bot(backend_name="dsh", bot_id="alpha", name="Alpha", moved_to="claude", person=moved_person)]
    bots += extra_bots if extra_bots is not None else [
        bot(backend_name="claude", bot_id="beta", name="Beta", person="p-1"),
        bot(backend_name="claude", bot_id="gamma", name="Gamma", person="p-1"),
    ]
    return overview(bots, backends=BACKENDS)


def test_P9_同person另有正常行_出现跳转_点击后切到第一条正常行(open_console):
    shell = open_console(_data(), query="?b=dsh/alpha")
    shell.wait_reads("persona")
    jump = shell.page.locator("#roJump")
    assert jump.count() == 1 and jump.first.is_visible(), "#roJump 不可见"
    assert jump.first.inner_text() == "去看在跑的那份（Claude）"
    assert jump.first.get_attribute("data-backend") == "claude"
    assert jump.first.get_attribute("data-bot-id") == "beta"     # DATA.bots 顺序第一条
    jump.first.click()
    assert shell.page.url.endswith("/console?b=claude/beta"), shell.page.url
    assert shell.page.inner_text("#botIdLine") == "beta"


def test_P10_找不到同person正常行_跳转不可见且不带目标(open_console):
    cases = {
        "person为空": _data(moved_person=""),
        "person缺省": overview(
            [bot(backend_name="dsh", bot_id="alpha", name="Alpha", moved_to="claude"),
             bot(backend_name="claude", bot_id="beta", name="Beta", person="p-9")],
            backends=BACKENDS),
        "无同person正常行": _data(
            extra_bots=[bot(backend_name="claude", bot_id="beta", name="Beta", person="p-9")],
            moved_person="p-1"),
        "同person但对方已迁出": _data(
            extra_bots=[bot(backend_name="claude", bot_id="beta", name="Beta",
                             person="p-1", moved_to="dsh")],
            moved_person="p-1"),
    }
    for name, data in cases.items():
        shell = open_console(data, query="?b=dsh/alpha")
        shell.wait_reads("persona")
        jump = shell.page.locator("#roJump")
        visible = jump.count() > 0 and jump.first.is_visible()
        assert not visible, "[%s] #roJump 不该可见" % name
        if jump.count():
            assert jump.first.get_attribute("data-backend") is None, name
            assert jump.first.get_attribute("data-bot-id") is None, name
