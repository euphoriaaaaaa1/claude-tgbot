# -*- coding: utf-8 -*-
"""pub-batch3b · GEN 时序回归（P6/P17 的补充回归）：选中正常行后详情区块必须显示读接口内容，
不能停在「正在读取…」；切到另一只正常 bot（第二个 key）后新区块同样要显示。

只经浏览器观察页面文本/控件值，不断言内部状态变量。
"""
from b3b_helpers import (  # noqa: F401  夹具
    backend, base_url, bot, chrome, no_prod_port_connect, open_console, overview,
)

EXPECTED = {
    "persona": "人设正文-ALPHA",
    "life": "23:00-07:00",
    "voiceId": "voice-alpha",
    "featLabels": ["功能甲"],
}

SNAPSHOT = """() => {
  const val = (id) => { const el = document.getElementById(id); return el ? el.value : null; };
  const labels = [...document.querySelectorAll('[data-role="feature-label"]')].map((e) => e.textContent);
  return {persona: val('personaText'), life: val('life-sleep_hours'),
          voiceId: val('voiceId'), featLabels: labels};
}"""


def _data():
    return overview(
        [bot(backend_name="claude", bot_id="alpha", name="Alpha"),
         bot(backend_name="claude", bot_id="beta", name="Beta")],
        backends=[backend("claude", "Claude")],
    )


def test_选中正常行五块显示内容_切到另一只后新区块同样显示(open_console):
    shell = open_console(_data(), query="?b=claude/alpha")
    shell.wait_reads("persona")
    page = shell.page
    assert page.evaluate(SNAPSHOT) == EXPECTED, "选中后详情区块内容没显示出来"
    assert "正在读取" not in page.inner_text("#detail"), "详情区还停在正在读取"

    page.locator('[data-role="bot-card"][data-backend="claude"][data-bot-id="beta"]').click()
    page.wait_for_function("() => document.getElementById('botIdLine').textContent === 'beta'")
    shell.wait_reads("persona")
    assert page.evaluate(SNAPSHOT) == EXPECTED, "切到另一只后新区块没显示内容"
    assert "正在读取" not in page.inner_text("#detail"), "切换后详情区还停在正在读取"
