# -*- coding: utf-8 -*-
"""T08 技能生成（skill_gen）：模板渲染 golden、fill/refresh、portable 圈保留、手管与软链跳过。"""
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import skill_gen  # noqa: E402


@pytest.fixture
def channel(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BOTLIFE_DIR", str(_ROOT))
    d = tmp_path / "channels" / "mybot"
    d.mkdir(parents=True)
    return d


def skill_file(channel, name="gif"):
    return channel / ".claude" / "skills" / ("mybot-" + name) / "SKILL.md"


GIF_GOLDEN = """---
name: mybot-gif
description: 表情包技能：从本机表情库挑图发送
---

# mybot-gif（mybot）

发表情包时从本机表情库挑图，在 reply 的 files 里带上路径；空 text 才没有配文；按对话情绪挑，不刷屏。一条消息至多一个表情包，不得连续发送。

<!-- portable:start -->

<!-- portable:end -->
"""


def test_fill_gif模板渲染golden(channel):
    out = skill_gen.write_all(None, "fill", names=["gif"], bot_dir=str(channel), apply=True)
    assert out["errors"] == 0 and out["placed"] == ["gif"]
    assert skill_file(channel).read_text(encoding="utf-8") == GIF_GOLDEN
    assert skill_file(channel).stat().st_mode & 0o777 == 0o600


def test_fill已有文件不动且重复跑报没变(channel):
    skill_gen.write_all(None, "fill", names=["gif"], bot_dir=str(channel), apply=True)
    path = skill_file(channel)
    before = path.read_bytes()
    out = skill_gen.write_all(None, "fill", names=["gif"], bot_dir=str(channel), apply=True)
    assert out["placed"] == []
    assert out["results"][0]["action"] == "没变"
    assert path.read_bytes() == before


def test_refresh保留portable圈内并重写圈外(channel):
    skill_gen.write_all(None, "fill", names=["gif"], display_name="旧名",
                        bot_dir=str(channel), apply=True)
    path = skill_file(channel)
    text = path.read_text(encoding="utf-8")
    text = text.replace("<!-- portable:start -->\n\n<!-- portable:end -->",
                        "<!-- portable:start -->\n我的私有内容\n<!-- portable:end -->")
    path.write_text(text, encoding="utf-8")
    out = skill_gen.write_all(None, "refresh", names=["gif"], display_name="新名",
                              bot_dir=str(channel), apply=True)
    assert out["errors"] == 0
    new = path.read_text(encoding="utf-8")
    assert "# mybot-gif（新名）" in new
    assert "我的私有内容" in new
    assert new.count("<!-- portable:start -->") == 1


def test_refresh没有标记的旧文件跳过(channel):
    path = skill_file(channel)
    path.parent.mkdir(parents=True)
    path.write_text("# 手动技能\n", encoding="utf-8")
    out = skill_gen.write_all(None, "refresh", names=["gif"], bot_dir=str(channel), apply=True)
    assert out["results"][0]["reason"] == "legacy"
    assert path.read_text(encoding="utf-8") == "# 手动技能\n"


def test_技能手管整只跳过(channel):
    (channel / ".skills-manual").write_text("manual\n", encoding="utf-8")
    out = skill_gen.write_all(None, "fill", names=["gif"], bot_dir=str(channel), apply=True)
    assert out["placed"] == []
    assert out["results"][0]["reason"] == "skills_manual"
    assert not skill_file(channel).exists()


def test_技能目录是软链跳过(channel, tmp_path):
    real = tmp_path / "real-skills"
    real.mkdir()
    (channel / ".claude").mkdir()
    os.symlink(real, channel / ".claude" / "skills")
    out = skill_gen.write_all(None, "fill", names=["gif"], bot_dir=str(channel), apply=True)
    assert out["placed"] == []
    assert out["results"][0]["reason"] == "symlink"


def test_不在模板里的通用名与未知占位符(channel):
    with pytest.raises(skill_gen.SkillGenError) as ei:
        skill_gen.write_all(None, "fill", names=["nsfw"], bot_dir=str(channel), apply=False)
    assert ei.value.code == "usage"
    with pytest.raises(skill_gen.SkillGenError):
        skill_gen.render("{{bogus}}", {})


def test_image模板appearance默认与自定义(channel):
    out = skill_gen.write_all(None, "fill", names=["image"], appearance="黑发，红衣",
                              bot_dir=str(channel), apply=True)
    assert out["errors"] == 0
    text = skill_file(channel, "image").read_text(encoding="utf-8")
    assert "黑发，红衣" in text
    assert "{{" not in text
