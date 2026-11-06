# -*- coding: utf-8 -*-
"""pub-batch3b · P1/P2/P3：选中行的只读提示（#roBanner）。

只经浏览器打开 /console 观察可见性与文案；/api 由 page.route 伪造。
"""
import pytest

from b3b_helpers import (  # noqa: F401  夹具（不用 conftest.py，见 b3b_helpers 文件头）
    banner_count, banner_text, banner_visible, base_url, backend, bot, chrome, detail_has_ro,
    no_prod_port_connect, open_console, overview,
)

PENDING_TEXT = "正在切换底层，暂时只能查看。"


def _wait_banner(shell):
    # 详情区读完显示名后再看提示条，避免抢在渲染前断言
    shell.wait_for("!!document.getElementById('displayNameInput') "
                   "&& document.getElementById('displayNameInput').value !== ''")


def _moved_data(moved_to):
    return overview(
        [
            bot(backend_name="dsh", bot_id="alpha", name="Alpha", moved_to=moved_to, person="p-1"),
            bot(backend_name="claude", bot_id="beta", name="Beta", person="p-2"),
        ],
        backends=[backend("claude", "Claude"), backend("dsh", "dsh")],
    )


def test_P1_选中已迁出的dsh行_提示已切到Claude且位于人设区之上(open_console):
    shell = open_console(_moved_data("claude"), query="?b=dsh/alpha")
    _wait_banner(shell)
    assert banner_visible(shell.page), "#roBanner 不可见"
    text = banner_text(shell.page)
    assert "已切到 Claude" in text, text
    assert "只能查看" in text, text
    # 位于 #pickHint 之后、人设区之前（DOM 顺序）
    order = shell.page.evaluate(
        "() => {const b=document.getElementById('roBanner');"
        " const p=document.getElementById('pickHint');"
        " const prof=document.getElementById('blkProfile');"
        " if(!b) return null;"
        " return {afterPick: !!(p && (p.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)),"
        "         beforeProfile: !!(prof && (b.compareDocumentPosition(prof) & Node.DOCUMENT_POSITION_FOLLOWING))};}")
    assert order == {"afterPick": True, "beforeProfile": True}, order


def test_P2_选中切换中的行_提示正在切换底层(open_console):
    shell = open_console(_moved_data("pending"), query="?b=dsh/alpha")
    _wait_banner(shell)
    assert banner_visible(shell.page), "#roBanner 不可见"
    assert banner_text(shell.page) == PENDING_TEXT


@pytest.mark.parametrize("moved_to", ["", "null", "undefined", "   ", "telegram"],
                         ids=["空串", "null", "undefined", "空白", "名单外字符串"])
def test_P3_moved_to为空或坏值_按正常行处理_提示隐藏且文本为空(open_console, moved_to):
    shell = open_console(_moved_data(moved_to), query="?b=dsh/alpha")
    _wait_banner(shell)
    assert banner_count(shell.page) == 1, "#roBanner 容器不存在"
    assert not banner_visible(shell.page), "#roBanner 不该可见"
    assert banner_text(shell.page) == ""
    assert detail_has_ro(shell.page) is False
