# -*- coding: utf-8 -*-
"""pub-batch3c 提示词文字回归：群聊语气、去 AI 味、人设模板小习惯一节。

只读仓内文件与模块常量；导演点名正文通过 director.inject 落在 tmp_path 的 inbox 里读回，
不起进程、不联网、不碰真实 ~/.claude。
"""
import importlib.util
import json
import os
import re
from pathlib import Path

import pytest

import director
import judge

ROOT = Path(__file__).resolve().parents[2]

VOICE_SNIPPETS = (
    ROOT / "persona-sync" / "snippets" / "voice-reply.md",
    ROOT / "voice-bridge" / "persona-snippet.md",
)
PERSONA_TEMPLATES = (
    ROOT / "channels" / "_persona_template" / "CLAUDE.md",
    ROOT / "configs" / "templates" / "claude" / "blank" / "CLAUDE.md",
)
# 本批改动的文件（含本测试文件），供脱敏检查
CHANGED_FILES = (
    *VOICE_SNIPPETS,
    ROOT / "persona-sync" / "snippets" / "group-autochat.md",
    ROOT / "configs" / "templates" / "skills" / "group" / "SKILL.md",
    ROOT / "director.py",
    ROOT / "formatter.py",
    ROOT / "judge.py",
    ROOT / "life-context.py",
    ROOT / "moments" / "post.py",
    *PERSONA_TEMPLATES,
    ROOT / "dispatcher" / "worker_manager_rel_prefix.test.ts",
    ROOT / "dispatcher" / "worker-manager.ts",
    Path(__file__),
)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _section(text: str, start: str, end: str) -> str:
    """取 start 标题行起、到 end 标题行之前的一段（不含 end）。"""
    i = text.index(start)
    return text[i:text.index(end, i)]


# ─── 两份 voice snippet ───────────────────────────────────────────

def _group_section(p: Path) -> str:
    return _section(_read(p), "### 群聊行为", "### 持续偏好记录（续）")


def _image_section(p: Path) -> str:
    return _section(_read(p), "### 发图配文规则", "### 群聊行为")


def _speech_only_line(p: Path) -> str:
    sec = _section(_read(p), "### 输出只发台词，不发心理活动", "### 双语")
    return next(l for l in sec.splitlines() if l.startswith("发给用户的 reply 正文"))


def test_两份voice_snippet_群聊行为段与发图段逐字一致():
    a, b = VOICE_SNIPPETS
    assert _group_section(a) == _group_section(b)
    assert _image_section(a) == _image_section(b)
    assert _speech_only_line(a) == _speech_only_line(b)


@pytest.mark.parametrize("p", VOICE_SNIPPETS, ids=lambda p: p.parent.name)
def test_群聊行为段_不要求收敛_无slash说明_reply_to可指向bot(p):
    sec = _group_section(p)
    assert "收敛" not in sec
    assert "slash" not in sec.lower()
    assert "可以指向其他 bot 的消息" in sec
    assert "### Telegram slash 命令" not in _read(p)


@pytest.mark.parametrize("p", VOICE_SNIPPETS, ids=lambda p: p.parent.name)
def test_发图段默认省略text且无举例(p):
    sec = _image_section(p)
    assert "默认完全省略 `text`" in sec
    assert "例：" not in sec and "给你拍了" not in sec


@pytest.mark.parametrize("p", VOICE_SNIPPETS, ids=lambda p: p.parent.name)
def test_只发台词一节无破折号(p):
    sec = _section(_read(p), "### 输出只发台词，不发心理活动", "### 双语")
    assert "——" not in sec


def test_GROUP_AUTOCHAT_含轮到你说话与每句10字():
    t = _read(ROOT / "persona-sync" / "snippets" / "group-autochat.md")
    body = _section(t, "### 轮到你说话", "<!-- GROUP-AUTOCHAT-END -->")
    assert "`[director]`" in body
    assert "群聊每句话控制在 10 字左右。" in body
    assert "### 未开导演模式时" in body


# ─── 群聊技能模板 ─────────────────────────────────────────────────

def test_群聊SKILL模板_不引用group_rules且无输出格式节():
    t = _read(ROOT / "configs" / "templates" / "skills" / "group" / "SKILL.md")
    assert "prompts/group-rules.md" not in t
    assert "## 输出格式" not in t
    assert "GROUP-AUTOCHAT" in t


# ─── 导演点名正文 ─────────────────────────────────────────────────

PREV_TEXT = "上一位那句独有原话甲乙丙"


@pytest.fixture
def inject_text(tmp_path, monkeypatch):
    monkeypatch.setenv("DIRECTOR_NO_SPAWN", "1")  # 与验收夹具一致：不真拉 worker
    monkeypatch.setattr(director, "CHANNELS_ROOT", str(tmp_path / "channels"))
    monkeypatch.setattr(director, "_is_stopped", lambda bot: False)
    monkeypatch.setattr(director, "_relations", None)

    def run(**kw) -> str:
        history = [{"ts": 1, "speaker": director.HUMAN_NAME, "text": "随便聊聊", "is_bot": False}]
        path = director.inject("bot1", "-1001234567890", history, **kw)
        assert path and os.path.isfile(path)
        with open(path, encoding="utf-8") as f:
            return json.load(f)["text"]
    return run


PREV = {"speaker": "群友", "text": PREV_TEXT}
BRANCHES = {
    "收尾轮": dict(prev=PREV, closing=True),
    "续轮": dict(prev=PREV),
    "续轮临近收尾": dict(prev=PREV, low_budget=True),
    "开场理由": dict(context="刚发了一条朋友圈"),
    "冷场发起": dict(initiate=True),
    "默认": dict(),
}


@pytest.mark.parametrize("name", list(BRANCHES))
def test_导演点名正文_以按语境回复结尾_不含你可以_不引用上一位原话(inject_text, name):
    text = inject_text(**BRANCHES[name])
    assert text.startswith("[director] ")
    assert text.endswith("（你按语境回复即可）")
    assert "你可以" not in text.splitlines()[0]  # 只约束点名开头一行；主人批次提示另有审定句
    assert PREV_TEXT not in text


def _owner(ts, text):
    return {"ts": ts, "speaker": director.HUMAN_NAME, "text": text, "is_bot": False, "msg_id": str(ts)}


def test_导演点名_主人批次提示用审定原句(inject_text):
    # 两句提示互斥：连发多条走前一句，只说一条走后一句
    multi = inject_text(user_batch=[_owner(11, "第一句"), _owner(12, "第二句")])
    assert "你可以一起回复；其中如果有明确的要求，优先照做。" in multi
    single = inject_text(user_batch=[_owner(13, "就一句")])
    assert "其中如果有明确的要求，优先照做；只是闲聊就直接回复。" in single
    for text in (multi, single):
        assert text.endswith("（你按语境回复即可）")


def test_导演点名_临近收尾附不用再开新话题(inject_text):
    assert "（这场快聊完了，不用再开新话题。）" in inject_text(**BRANCHES["续轮临近收尾"])
    assert "不用再开新话题" not in inject_text(**BRANCHES["续轮"])


# ─── formatter / judge / life-context / moments ──────────────────

def test_formatter_模式行不含必须开口_跟进行用对方():
    t = _read(ROOT / "formatter.py")
    assert "必须开口" not in t
    assert "说了话但对方还没回复" in t


JUDGE_PLACEHOLDERS = {
    "persona_summary", "speaking_threshold", "recurring", "recurring_desc", "recurring_state",
    "mood_desc", "mood_factors", "sporadic_desc", "wildcard_desc", "news_desc",
    "special_date_desc", "hobby_desc", "since_user_min", "recent_topics",
    "jiwen_state_desc", "jiwen_style_guidance", "boost_clause",
}


def _placeholders(tpl: str) -> set:
    return set(re.findall(r"(?<!\{)\{([a-z_]+)\}(?!\})", tpl))


def test_JUDGE_PROMPT_占位符集合不变_无破折号_无AI角色与方括号标题():
    p = judge.JUDGE_PROMPT
    assert _placeholders(p) == JUDGE_PLACEHOLDERS
    assert "——" not in p
    assert "AI 角色" not in p and "【" not in p and "】" not in p
    rendered = p.format(**{k: "X" for k in JUDGE_PLACEHOLDERS})
    assert rendered.rstrip().endswith('只输出 JSON: {"action": "skip|dm", "reason": "10字内"}')


def test_刚发朋友圈提示允许顺口提():
    # life-context.py 是带连字符的入口脚本，不 import，只读常量所在行
    line = next(l for l in _read(ROOT / "life-context.py").splitlines()
                if l.startswith("MOMENT_JUST_POSTED_HINT"))
    assert "可以稍微提一下" in line and "别复述" not in line


def test_MOMENT_PROMPT_无举例_保留空输出约定():
    from moments import post
    p = post.MOMENT_PROMPT
    assert "反面" not in p and "正面" not in p and "【基本规则" not in p
    names = _placeholders(p)
    rendered = p.format(**{k: "X" for k in names})
    assert rendered.rstrip().endswith('没有想分享的事就输出 {"text": "", "visibility": "public"}')


# ─── 人设模板 ─────────────────────────────────────────────────────

@pytest.mark.parametrize("p", PERSONA_TEMPLATES, ids=lambda p: p.parent.name)
def test_人设模板含小习惯一节且只有写法说明(p):
    t = _read(p)
    assert t.count("## 小习惯与条件反射") == 1
    sec = t.split("## 小习惯与条件反射", 1)[1]
    sec = re.split(r"\n#{1,2} ", sec, maxsplit=1)[0]
    m = re.fullmatch(r"\s*<!--(.*?)-->\s*", sec, flags=re.S)
    assert m, "本节只能有一段写法说明注释"
    assert m.group(1).lstrip().startswith("写法说明，写完内容后删掉本注释。")


# ─── 脱敏 ─────────────────────────────────────────────────────────

def _load_scanner():
    spec = importlib.util.spec_from_file_location("_scan_release", ROOT / "scripts" / "scan_release.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_改动文件不含私有路径用户名与真实bot名():
    scanner = _load_scanner()
    for p in CHANGED_FILES:
        for no, line in enumerate(_read(p).splitlines(), 1):
            if "scan-release: allow" in line:
                continue
            assert not scanner._scan_line(line), f"{p.relative_to(ROOT)}:{no}"


def test_改动文件不含本地名单里的私有名字():
    # 私有中文名不能写进公开仓，名单由本机文件提供（一行一个）；未提供时跳过。
    deny_path = os.environ.get("RELEASE_NAME_DENYLIST", "")
    if not deny_path or not os.path.isfile(deny_path):
        pytest.skip("未提供 RELEASE_NAME_DENYLIST")
    with open(deny_path, encoding="utf-8") as f:
        names = [n.strip().lower() for n in f if n.strip()]
    for p in CHANGED_FILES:
        t = _read(p).lower()
        hit = [n for n in names if n in t]
        assert not hit, f"{p.relative_to(ROOT)} 命中 {len(hit)} 个名字"
