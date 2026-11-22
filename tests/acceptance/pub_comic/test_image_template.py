# -*- coding: utf-8 -*-
"""pub-comic · image 技能模板渲染（行为清单 16 与 16 补）。

只通过命令行 `python -m skill_gen` 渲染：HOME、bot 配置目录都指向 tmp_path，不碰真实用户目录。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
BOT = "mybot"


def _env(tmp: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
    env.update({
        "HOME": str(tmp),
        "BOTLIFE_DIR": str(ROOT),
        "CLAUDEBOT_CONFIG_DIR": str(tmp / "cfg"),
        "HUB_CONFIGS_DIR": str(tmp / "cfg"),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    return env


def _run(tmp: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "skill_gen", *args], cwd=str(ROOT), env=_env(tmp),
                          capture_output=True, text=True, timeout=120)


@pytest.fixture
def world():
    """临时目录里造一只名为 mybot 的 bot（配置 + 圈外目录），返回 (目录, image 技能文件路径)。
    目录名用中性前缀而不用 pytest 的 tmp_path：tmp_path 带用例名，会被渲染进技能文本里污染断言。"""
    tmp_path = Path(tempfile.mkdtemp(prefix="pc-"))
    try:
        (tmp_path / "cfg").mkdir()
        cfg = (ROOT / "configs" / "_example.yml").read_text(encoding="utf-8")
        cfg = cfg.replace("id: my-bot", f"id: {BOT}").replace("channels/my-bot", f"channels/{BOT}")
        (tmp_path / "cfg" / f"{BOT}.yml").write_text(cfg, encoding="utf-8")
        (tmp_path / ".claude" / "channels" / BOT).mkdir(parents=True)
        yield tmp_path, tmp_path / ".claude" / "channels" / BOT / ".claude" / "skills" / f"{BOT}-image" / "SKILL.md"
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


@pytest.fixture
def rendered(world):
    tmp, skill = world
    r = _run(tmp, "--bot", BOT, "--mode", "fill", "--names", "image", "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    assert skill.exists(), r.stdout + r.stderr
    return skill.read_text(encoding="utf-8")


def test_渲染后_含先读novelai_skill的SKILL_md一句_且路径已被渲染成真实目录(rendered):
    m = re.search(r"写提示词前先读 (\S+)/novelai-skill/SKILL\.md", rendered)
    assert m is not None, "缺少「写提示词前先读 <skills_root>/novelai-skill/SKILL.md」"
    assert "{{" not in m.group(1)
    assert m.group(1).startswith("/")


def test_渲染后_没有任何未替换的占位符(rendered):
    assert re.findall(r"\{\{[^}]*\}\}", rendered) == []


def test_渲染后_含auto_comic三页一条(rendered):
    lines = [ln for ln in rendered.splitlines() if "[auto-comic]" in ln]
    assert len(lines) == 1, f"含 [auto-comic] 的行应恰好 1 条，实际 {len(lines)}"
    assert "三页" in lines[0]
    assert "两页" not in lines[0]


def test_渲染后_漫画那一条含引擎是comfyui时本轮按普通出图(rendered):
    lines = [ln for ln in rendered.splitlines() if "[auto-comic]" in ln]
    assert len(lines) == 1
    assert "引擎是 comfyui 时本轮按普通出图" in lines[0]


def test_渲染后_含两人及以上有肢体接触的画面一条(rendered):
    assert "两人及以上有肢体接触的画面" in rendered


def test_渲染后_含第一视角中user是镜头不在characters中写项(rendered):
    assert "第一视角中 user 是镜头，不在 characters 中写项" in rendered


def test_refresh_已建bot圈内形象段逐字节不变(world, rendered):
    tmp, skill = world
    start, end = "<!-- portable:start -->", "<!-- portable:end -->"
    text = skill.read_text(encoding="utf-8")
    custom = "\n我的私有形象：银发，蓝眼，\t制服 \n  第二行带尾空格  \n"
    text = text[:text.index(start) + len(start)] + custom + text[text.index(end):]
    skill.write_text(text, encoding="utf-8")
    before = skill.read_bytes()
    seg_before = before[before.index(start.encode()):before.index(end.encode()) + len(end)]
    r = _run(tmp, "--all", "--mode", "refresh", "--names", "image", "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    after = skill.read_bytes()
    seg_after = after[after.index(start.encode()):after.index(end.encode()) + len(end)]
    assert seg_after == seg_before
    assert custom.encode() in seg_after
