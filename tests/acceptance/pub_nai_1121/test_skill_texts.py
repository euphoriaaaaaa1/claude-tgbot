# -*- coding: utf-8 -*-
"""pub-nai-1121 · 技能文本与人设同步片段（行为清单 19、20、21、22）。

- 19：用 `python -m skill_gen` 渲染 image 技能（临时 HOME），检查渲染结果文本；
- 20：读 skills/novelai-skill/SKILL.md 文本；
- 21：用 persona-sync 命令行把语音段同步进临时目录里的 CLAUDE.md；
- 22：比对两份人设片段里同一条规则。
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import pn_helpers as H

COMFYUI_SENTENCE = "此条只对 NovelAI 有效，引擎是 comfyui 时本轮按普通出图。"
ONOMATOPOEIA = "拟声词不朗读"


def lines_with(text, part):
    return [ln for ln in text.splitlines() if part in ln]


# 行为 19
def test_行为19_image技能_恰好一行auto_comic_含三页与引擎是comfyui的那句():
    lines = lines_with(H.default_rendered(), "[auto-comic]")
    assert len(lines) == 1, "含 [auto-comic] 的行应恰好 1 条，实际 %d" % len(lines)
    assert "三页" in lines[0] and COMFYUI_SENTENCE in lines[0], lines[0]


def test_行为19_image技能_恰好一行auto_photo_含三张且以同一句结尾():
    lines = lines_with(H.default_rendered(), "[auto-photo]")
    assert len(lines) == 1, "含 [auto-photo] 的行应恰好 1 条，实际 %d" % len(lines)
    assert "三张" in lines[0], lines[0]
    assert lines[0].rstrip().endswith(COMFYUI_SENTENCE), lines[0]


def section_body(text, heading_part):
    """含 heading_part 的标题行之后、到下一个同级或更高级标题之前的全部文本。"""
    lines = text.splitlines()
    idx = next(i for i, ln in enumerate(lines) if re.match(r"^#{1,6}\s", ln) and heading_part in ln)
    level = len(re.match(r"^(#+)", lines[idx]).group(1))
    body = []
    for ln in lines[idx + 1:]:
        m = re.match(r"^(#+)\s", ln)
        if m and len(m.group(1)) <= level:
            break
        body.append(ln)
    return "\n".join(body)


def test_行为19_image技能_机位景别与张力一节_不含两张图():
    section = section_body(H.default_rendered(), "机位、景别与张力")
    assert section.strip(), "找不到「机位、景别与张力」一节或该节为空"
    assert "两张图" not in section, "该节仍写着「两张图」"


def test_行为19_image技能_机位景别与张力一节_含各张图从镜头视角词库取不同条目():
    section = section_body(H.default_rendered(), "机位、景别与张力")
    assert "各张图从镜头视角词库取不同条目" in section, section


def test_行为19_image技能_机位景别与张力一节_含第一视角各张按novelai_skill的第一视角取景_不写full_body():
    section = section_body(H.default_rendered(), "机位、景别与张力")
    assert "第一视角的各张按 novelai-skill 的「第一视角」取景，不写 full body" in section, section


def test_行为19_image技能_comfyui那句指向comfyui_skill的SKILL_md():
    text = H.default_rendered()
    pointers = [ln for ln in lines_with(text, "comfyui-skill/SKILL.md") if "comfyui" in ln.replace("comfyui-skill", "")]
    assert pointers, "找不到指向 comfyui-skill/SKILL.md 的 comfyui 那一句"


# 行为 20
def skill_text():
    return H.SKILL_MD.read_text(encoding="utf-8")


def test_行为20_SKILL_md_含人物negative的写法说明():
    text = skill_text()
    assert "characters[].negative" in text, "缺 characters[].negative 说明"
    assert "每一项写 `positive` 与 `negative`" in text
    assert any("女性" in ln and "futanari, penis, testicles" in ln for ln in text.splitlines()), \
        "缺「女性人物项 negative 写 futanari, penis, testicles」"


def test_行为20_SKILL_md_含生殖器归属_第一视角首条_角色本人照常写一项():
    text = skill_text()
    assert "生殖器与交合部位写在所属人物的项里" in text
    assert "角色本人在 `characters` 中照常写一项" in text
    first = H.section_first_line(text, "第一视角")
    assert re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", first).startswith("user 的第一视角下"), first


def test_行为20_SKILL_md_漫画三句_不含4koma_所有权重不超过1点5():
    text = skill_text()
    for phrase in ("每轮出三页", "先为每页写台本", "有主有次"):
        assert phrase in text, "缺漫画规则「%s」" % phrase
    assert "4koma" not in text
    over = [m.group(0) for m in re.finditer(r"(?<![\w.])(-?\d+(?:\.\d+)?)::[^:\n]*?::", text) if float(m.group(1)) > 1.5]
    assert over == [], "权重超过 1.5：%s" % over


# 行为 21
def test_行为21_persona_sync同步后_目标CLAUDE_md的语音段含拟声词不朗读一条(tmp_path):
    target = tmp_path / "CLAUDE.md"
    target.write_text("# persona\n\n人设正文。\n", encoding="utf-8")
    cfg = tmp_path / "snippets.json"
    cfg.write_text(json.dumps({
        "targets": [str(target)],
        "snippets": [{"source": str(H.REPO / "persona-sync" / "snippets" / "voice-reply.md"),
                      "start": "VOICE-REPLY-START", "end": "VOICE-REPLY-END"}],
    }), encoding="utf-8")
    cp = subprocess.run([sys.executable, "-B", str(H.REPO / "persona-sync" / "sync_snippet.py"), "--config", str(cfg)],
                        cwd=str(tmp_path), capture_output=True, text=True, timeout=60,
                        env=H.clean_env(HOME=str(tmp_path / "home")))
    assert cp.returncode == 0, cp.stdout + cp.stderr
    text = target.read_text(encoding="utf-8")
    start, end = text.index("VOICE-REPLY-START"), text.index("VOICE-REPLY-END")
    segment = text[start:end]
    hits = lines_with(segment, ONOMATOPOEIA)
    assert len(hits) == 1, "语音段里含「%s」的行应恰好 1 条，实际 %d" % (ONOMATOPOEIA, len(hits))


# 行为 22
def test_行为22_两份人设片段里以拟声词不朗读开头的那一行逐字相同():
    found = {}
    for rel in ("persona-sync/snippets/voice-reply.md", "voice-bridge/persona-snippet.md"):
        text = (H.REPO / rel).read_text(encoding="utf-8")
        rows = [ln for ln in text.splitlines() if ln.startswith(ONOMATOPOEIA)]
        assert len(rows) == 1, "%s 里以「%s」开头的行应恰好 1 条，实际 %d" % (rel, ONOMATOPOEIA, len(rows))
        found[rel] = rows[0]
    a, b = found.values()
    assert a == b, "两份片段里的这一行不一致：\n%s\n%s" % (a, b)
