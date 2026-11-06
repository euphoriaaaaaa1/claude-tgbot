# -*- coding: utf-8 -*-
"""pub-batch3b · P4/P5/P12/P13：只读行的写入口不可见、输入只读、控制台调用不发请求。

只经浏览器观察 DOM 与 Network（page.route 记录所有 /api 请求）。
"""
import pytest

from b3b_helpers import (  # noqa: F401  夹具
    backend, bot, base_url, chrome, enabled_write_ids, no_prod_port_connect, open_console,
    overview, visible_write_ids,
)

CALLS = """() => {
  const calls = [
    ['saveName', () => saveName()],
    ['toggleEnabled', () => toggleEnabled()],
    ['saveLife', () => saveLife()],
    ['savePersona', () => savePersona()],
    ['saveVoice', () => saveVoice()],
    ['toggleFeature', () => toggleFeature('feat_alpha')],
    ['restartBot', () => restartBot('dsh', 'alpha', 'Alpha', null)],
    ['confirmArchive', () => confirmArchive()],
    ['confirmDelete', () => confirmDelete()],
    ['submitBackendSwitch', () => submitBackendSwitch()],
  ];
  const errors = [];
  for (const [name, fn] of calls) {
    try { fn(); } catch (e) { errors.push(name + ': ' + (e && e.message)); }
  }
  return errors;
}"""


def _data(moved_to):
    return overview(
        [
            bot(backend_name="dsh", bot_id="alpha", name="Alpha", moved_to=moved_to, person="p-1"),
            bot(backend_name="claude", bot_id="beta", name="Beta", person="p-2"),
        ],
        backends=[backend("claude", "Claude"), backend("dsh", "dsh")],
    )


def _wait_profile(shell):
    shell.wait_reads("persona")


def test_P4_选中已迁出的行_可见写按钮集合为空(open_console):
    shell = open_console(_data("claude"), query="?b=dsh/alpha")
    _wait_profile(shell)
    assert visible_write_ids(shell.page) == []


def test_P5_选中切换中的行_写按钮集合为空且输入只读(open_console):
    shell = open_console(_data("pending"), query="?b=dsh/alpha")
    _wait_profile(shell)
    assert visible_write_ids(shell.page) == []
    readonly = shell.page.evaluate("""() => ({
      name: document.getElementById('displayNameInput').readOnly,
      persona: document.getElementById('personaText').readOnly,
      life: (document.getElementById('life-sleep_hours') || {}).readOnly === true,
    })""")
    assert readonly == {"name": True, "persona": True, "life": True}


def test_P12_选中已迁出的行_输入控件只读且语音模型禁用(open_console):
    shell = open_console(_data("claude"), query="?b=dsh/alpha")
    _wait_profile(shell)
    state = shell.page.evaluate("""() => ({
      nameReadOnly: document.getElementById('displayNameInput').readOnly,
      personaReadOnly: document.getElementById('personaText').readOnly,
      lifeReadOnly: (document.getElementById('life-sleep_hours') || {}).readOnly === true,
      voiceModelDisabled: document.getElementById('voiceModel').disabled,
    })""")
    assert state == {"nameReadOnly": True, "personaReadOnly": True,
                     "lifeReadOnly": True, "voiceModelDisabled": True}


def test_P13_只读态下控制台调10个写函数_没有任何内容写请求也不报新错(open_console):
    shell = open_console(_data("claude"), query="?b=dsh/alpha")
    _wait_profile(shell)
    before = list(shell.api_writes())
    errs_before = shell.page.locator('[data-kind="err"]:visible').count()
    errors = shell.page.evaluate(CALLS)          # 逐个调用；同步抛错也记下，不中断
    shell.page.wait_for_timeout(500)             # 给可能发出的请求留出时间
    writes = shell.api_writes()[len(before):]
    assert errors == [], "控制台调用抛错: %s" % errors
    assert writes == [], "只读态发出了写请求: %s" % writes
    assert shell.page.locator('[data-role="load-error"]').count() == 0
    assert shell.page.locator('[data-kind="err"]:visible').count() == errs_before
