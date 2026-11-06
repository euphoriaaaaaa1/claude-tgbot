# -*- coding: utf-8 -*-
"""pub-batch3b · 切行回归：A 的读接口 200、B 的 persona/life/voice 404 unknown_bot 时，
从 A 切到 B 后人设/生活/语音不得残留 A 的内容（P7/P8 是 404 开局，证明不了清空）。
"""
from b3b_helpers import (  # noqa: F401  夹具
    DELETED, backend, base_url, bot, chrome, default_reads, no_prod_port_connect, open_console,
    overview, reads_with,
)

DELETED_TEXT = "这一侧已经删除"

SNAPSHOT = """() => ({
  persona: document.getElementById('personaText').value,
  voiceId: document.getElementById('voiceId').value,
  lifeValues: [...document.querySelectorAll('[data-life-key]')].map((e) => e.value),
  lifeText: document.getElementById('lifeRows').innerText,
})"""


def _reads():
    alpha = default_reads("alpha")
    return reads_with(
        persona={"alpha": alpha["persona"], "beta": DELETED},
        life={"alpha": alpha["life"], "beta": DELETED},
        voice={"alpha": alpha["voice"], "beta": DELETED},
    )


def test_切到本侧已删除的行_人设生活语音不残留上一只的内容(open_console):
    data = overview(
        [bot(backend_name="claude", bot_id="alpha", name="Alpha"),
         bot(backend_name="claude", bot_id="beta", name="Beta")],
        backends=[backend("claude", "Claude")],
    )
    shell = open_console(data, reads=_reads(), query="?b=claude/alpha")
    shell.wait_reads("persona")
    page = shell.page

    first = page.evaluate(SNAPSHOT)
    assert first["persona"] == "人设-alpha"
    assert first["voiceId"] == "voice-alpha"
    assert first["lifeValues"] == ["23:00-alpha"]

    page.locator('[data-role="bot-card"][data-backend="claude"][data-bot-id="beta"]').click()
    page.wait_for_function("() => document.getElementById('botIdLine').textContent === 'beta'")
    page.wait_for_function(
        "() => document.getElementById('personaMsg').textContent.indexOf('%s') >= 0" % DELETED_TEXT,
        timeout=5000)

    after = page.evaluate(SNAPSHOT)
    assert after["persona"] == "", "人设还残留上一只的内容：%r" % after["persona"]
    assert after["voiceId"] == "", "语音编号还残留：%r" % after["voiceId"]
    assert all("alpha" not in (v or "") for v in after["lifeValues"]), after["lifeValues"]
    assert "alpha" not in after["lifeText"].lower()
    assert DELETED_TEXT in page.inner_text("#personaMsg")
    assert DELETED_TEXT in page.inner_text("#voiceMsg")
