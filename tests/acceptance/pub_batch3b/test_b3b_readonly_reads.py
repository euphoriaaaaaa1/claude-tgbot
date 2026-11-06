# -*- coding: utf-8 -*-
"""pub-batch3b · P6/P7/P8：迁出行读接口 200 正常显示；404 unknown_bot 显示"这一侧已经删除"；其它状态照旧报错。

只经浏览器打开 /console，读接口响应由 page.route 伪造，按可观察的 HTTP 状态断言页面表现。
"""
import pytest

from b3b_helpers import (  # noqa: F401  夹具
    BACKEND_DOWN, DELETED, backend, base_url, bot, chrome, no_prod_port_connect, open_console,
    overview, reads_with,
)

DELETED_TEXT = "这一侧已经删除"


def _data():
    return overview(
        [
            bot(backend_name="dsh", bot_id="alpha", name="Alpha", moved_to="claude", person="p-1"),
            bot(backend_name="claude", bot_id="beta", name="Beta", person="p-2"),
        ],
        backends=[backend("claude", "Claude"), backend("dsh", "dsh")],
    )


def _open(open_console, **overrides):
    shell = open_console(_data(), reads=reads_with(**overrides), query="?b=dsh/alpha")
    shell.wait_reads("persona")
    return shell


def test_P6_迁出行读接口全200_五块内容照常显示(open_console):
    shell = _open(open_console)
    got = shell.page.evaluate("""() => {
      const val = (id) => { const el = document.getElementById(id); return el ? el.value : null; };
      const txt = (sel) => [...document.querySelectorAll(sel)].map((e) => e.textContent);
      return {name: val('displayNameInput'), persona: val('personaText'),
        life: val('life-sleep_hours'), voiceId: val('voiceId'), voiceModel: val('voiceModel'),
        textLang: val('voiceTextLang'), voiceLang: val('voiceLang'),
        featLabels: txt('[data-role="feature-label"]'), featStates: txt('[data-role="feature-state"]')};
    }""")
    assert got == {
        "name": "Alpha", "persona": "人设-alpha", "life": "23:00-alpha",
        "voiceId": "voice-alpha", "voiceModel": "s1", "textLang": "zh", "voiceLang": "zh",
        "featLabels": ["功能-alpha"], "featStates": ["开着"],
    }


def test_P7_persona返回404unknown_bot_人设块显示这一侧已经删除且不带出错样式(open_console):
    shell = _open(open_console, persona=DELETED)
    page = shell.page
    msg = page.locator("#personaMsg")
    assert DELETED_TEXT in msg.inner_text()
    assert msg.get_attribute("data-kind") != "err"
    assert page.locator('#blkProfile [role="alert"]').count() == 0
    body = page.inner_text("body")
    assert "unknown_bot" not in body
    assert "HTTP 404" not in body


@pytest.mark.parametrize("kind,msg_id,ids", [
    ("life", "#lifeMsg", []),
    ("voice", "#voiceMsg", []),
    ("profile", "#displayNameMsg", []),
    ("features", "#featuresMsg", []),
], ids=["life", "voice", "profile", "features"])
def test_P8_其余读接口404unknown_bot_对应块显示这一侧已经删除(open_console, kind, msg_id, ids):
    shell = _open(open_console, **{kind: DELETED})
    page = shell.page
    msg = page.locator(msg_id)
    assert DELETED_TEXT in msg.inner_text()
    assert msg.get_attribute("data-kind") != "err"
    assert page.locator('[role="alert"]').count() == 0
    body = page.inner_text("body")
    assert "unknown_bot" not in body
    assert "HTTP 404" not in body


def test_P8_读接口502backend_down_照旧显示原错误不显示这一侧已经删除(open_console):
    shell = _open(open_console, persona=BACKEND_DOWN)
    page = shell.page
    msg = page.locator("#personaMsg")
    text = msg.inner_text()
    assert "底层连不上" in text
    assert "失败" in text
    assert DELETED_TEXT not in text
    assert msg.get_attribute("data-kind") == "err"
