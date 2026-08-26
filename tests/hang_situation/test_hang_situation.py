# -*- coding: utf-8 -*-
"""被晾感知的作息桥 hang_situation.py。

桥的契约就三条：吐得出合法 JSON、interruptible 认得出作息表里的标记、
出错时只报异常类名不回显配置内容（configs 里有 token 和 chat_id）。
作息表用假 cfg 顶掉 config_loader.load_bot，不碰真配置、不联网。
"""
import json
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(ROOT, "hang_situation.py")


def _all_day(name, state, **extra):
    """整天覆盖的一条作息：weekday/weekend 都放同一条，判定就与今天是不是工作日无关。"""
    item = {"name": name, "when": "00:00-23:59", "state": state, "description": ""}
    item.update(extra)
    return {"weekday": [item], "weekend": [item]}


@pytest.fixture
def fake_cfg(monkeypatch):
    """把 config_loader.load_bot 换成返回指定 cfg 的桩，返回一个 setter。"""
    import config_loader

    def use(recurring):
        cfg = {"id": "faketestbot", "recurring_activities": recurring}
        monkeypatch.setattr(config_loader, "load_bot", lambda _bot: cfg)
        return cfg

    return use


def test_命中的作息标了interruptible_false_原样透出(fake_cfg):
    import hang_situation
    fake_cfg(_all_day("上课", "busy_class", interruptible=False))
    out = hang_situation.query("faketestbot")
    assert out == {"name": "上课", "state": "busy_class",
                   "interruptible": False, "term_label": ""}


def test_没标interruptible_默认可打断(fake_cfg):
    import hang_situation
    fake_cfg(_all_day("追剧", "free"))
    assert hang_situation.query("faketestbot")["interruptible"] is True


def test_显式标true_也是可打断(fake_cfg):
    import hang_situation
    fake_cfg(_all_day("发呆", "free", interruptible=True))
    assert hang_situation.query("faketestbot")["interruptible"] is True


def test_作息表整个缺失_不炸_退到兜底档且可打断(fake_cfg):
    import hang_situation
    fake_cfg({})
    out = hang_situation.query("faketestbot")
    assert out["interruptible"] is True
    assert out["name"] and out["state"]        # 兜底档（睡觉中 / 自由时间）


def test_没配school_calendar_学期标签为空串_在校是常态不用讲(fake_cfg):
    import hang_situation
    fake_cfg(_all_day("备课", "busy_work"))
    assert hang_situation.query("faketestbot")["term_label"] == ""


@pytest.mark.parametrize("term,label", [
    ("summer_break", "暑假"), ("winter_break", "寒假"),
    ("early_return", "提前返校期"), ("in_session", ""), ("啥也不是", ""),
])
def test_学期标签取act_term映射中文_在校与未知一律空串(term, label):
    import hang_situation
    from generators.situation import Activity
    act = Activity(name="x", description="", state="free", term=term)
    assert hang_situation._term_label(act) == label


def test_活动对象没有term字段_不炸_按在校处理():
    import hang_situation

    class Bare:
        name, state = "x", "free"

    assert hang_situation._term_label(Bare()) == ""


def _run(*args):
    return subprocess.run([sys.executable, SCRIPT, *args],
                          cwd=ROOT, capture_output=True, text=True, timeout=60)


def test_命令行跑真配置_吐一行合法JSON且三个键齐全():
    r = _run("chenlulu")
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert set(out) == {"name", "state", "interruptible", "term_label"}
    assert isinstance(out["name"], str) and out["name"]
    assert isinstance(out["state"], str) and out["state"]
    assert isinstance(out["interruptible"], bool)
    assert isinstance(out["term_label"], str)


def test_缺bot参数_退2且不吐JSON():
    r = _run()
    assert r.returncode == 2
    assert r.stdout.strip() == ""


def test_bot不存在_退1_报错只有类名不回显配置内容():
    r = _run("no_such_bot_zzz")
    assert r.returncode == 1
    assert r.stdout.strip() == ""
    # 只允许出现 "hang_situation: <bot> failed (<ExceptionClass>)"
    assert "no_such_bot_zzz failed (" in r.stderr
    for leak in ("token", "chat_id", "allowFrom", "configs/"):
        assert leak not in r.stderr
