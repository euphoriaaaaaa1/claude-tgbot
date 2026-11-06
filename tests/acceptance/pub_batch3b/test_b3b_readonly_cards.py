# -*- coding: utf-8 -*-
"""pub-batch3b · P14/P15/P16/P17：卡片文本、迁出卡不出现重启、切到正常行后写入口恢复。

只经浏览器观察卡片与详情区。
"""
from b3b_helpers import (  # noqa: F401  夹具
    backend, banner_visible, base_url, bot, chrome, detail_has_ro,
    no_prod_port_connect, open_console, overview, visible_write_ids,
)

BACKENDS = [backend("claude", "Claude"), backend("dsh", "dsh")]

DATA = overview(
    [
        bot(backend_name="dsh", bot_id="alpha", name="Alpha", moved_to="claude", person="p-1"),
        bot(backend_name="dsh", bot_id="delta", name="Delta", moved_to="pending", person="p-3"),
        bot(backend_name="claude", bot_id="beta", name="Beta", person="p-2"),
    ],
    backends=BACKENDS,
)


def _card(page, backend_name, bot_id):
    return page.locator('[data-role="bot-card"][data-backend="%s"][data-bot-id="%s"]'
                        % (backend_name, bot_id))


def test_P14_从迁出行切到正常行_提示消失且写入口恢复可点(open_console):
    shell = open_console(DATA, query="?b=dsh/alpha")
    shell.wait_reads("persona")
    _card(shell.page, "claude", "beta").click()
    shell.page.wait_for_function(
        "() => document.getElementById('botIdLine').textContent === 'beta'")
    shell.wait_reads("persona")
    page = shell.page
    assert not banner_visible(page)
    assert detail_has_ro(page) is False
    state = page.evaluate("""() => {
      const ids = ['displayNameSave', 'toggleEnabled', 'lifeSave', 'personaSave', 'voiceSave', 'lifecycleFoot'];
      const out = {};
      for (const id of ids) {
        const el = document.getElementById(id);
        out[id] = !!el && !el.hidden && el.offsetParent !== null && !el.disabled;
      }
      out.nameReadOnly = document.getElementById('displayNameInput').readOnly;
      out.personaReadOnly = document.getElementById('personaText').readOnly;
      out.voiceModelDisabled = document.getElementById('voiceModel').disabled;
      return out;
    }""")
    assert state == {
        "displayNameSave": True, "toggleEnabled": True, "lifeSave": True, "personaSave": True,
        "voiceSave": True, "lifecycleFoot": True,
        "nameReadOnly": False, "personaReadOnly": False, "voiceModelDisabled": False,
    }


def test_P15_卡片文本_迁出行显示已切到_切换中显示正在切换_正常卡都没有(open_console):
    shell = open_console(DATA, query="?b=claude/beta")
    page = shell.page
    moved = _card(page, "dsh", "alpha").inner_text()
    pending = _card(page, "dsh", "delta").inner_text()
    normal = _card(page, "claude", "beta").inner_text()
    assert "已切到" in moved and "claude" in moved.lower()
    assert "正在切换底层" in pending
    assert "已切到" not in normal
    assert "正在切换底层" not in normal


def test_P16_迁出行的卡片动作区不出现重启按钮(open_console):
    shell = open_console(DATA, query="?b=claude/beta")
    page = shell.page
    assert _card(page, "dsh", "alpha").locator('[data-action="restart"]').count() == 0
    assert _card(page, "dsh", "delta").locator('[data-action="restart"]').count() == 0
    assert _card(page, "claude", "beta").locator('[data-action="restart"]').count() == 1


def test_P17_正常行_写入口与重启归档删除都在且无只读提示(open_console):
    shell = open_console(DATA, query="?b=claude/beta")
    shell.wait_reads("persona")
    page = shell.page
    assert not banner_visible(page)
    assert visible_write_ids(page) == ["displayNameSave", "toggleEnabled", "lifeSave",
                                       "personaSave", "voiceSave", "lifecycleFoot",
                                       "feature-toggle*1", "card-restart*1"]
    assert _card(page, "claude", "beta").locator('[data-action="restart"]').count() == 1
    assert page.locator('#lifecycleFoot').is_visible()
    assert page.locator('#archiveBot').is_visible()
    assert page.locator('#deleteBot').is_visible()
