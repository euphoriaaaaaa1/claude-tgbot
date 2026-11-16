# -*- coding: utf-8 -*-
"""pub-comic-nai 接口文件「行为清单 14 条」的验收用例，编号与清单一一对应。

只通过对外入口：generate_novelai_image.py 命令行 + SKILL.md 文本；生图服务是本机假服务。
"""
import json
import re

import pytest

import pcn_helpers as H

NOVELAI = "[novelai] "
ONE_CHAR = [{"positive": "girl, 2::long hair::, white dress, smiling"}]
BODY = "standing in a park, sunny day"

# 第 3、10、11 条用的配置前缀：要剔除的标签都夹在要保留的标签中间，
# 这样"保留的标签在请求里首尾相连、顺序不变"就等价于"夹在中间的那些都被删了"。
COMIC_POS = "best quality, 1.3::speech bubble::, masterpiece, comic, 2koma, very aesthetic, border, year 2025"
COMIC_NEG = ("lowres, text, bad anatomy, no text, 1.5::multiple views::, watermark, negative space, "
             "blank page, 4koma, blurry")
POS_KEEP = ["best quality", "masterpiece", "very aesthetic", "year 2025"]
NEG_KEEP = ["lowres", "bad anatomy", "watermark", "blurry"]
REMOVED = {"1.3::speech bubble::", "comic", "2koma", "border",
           "text", "no text", "1.5::multiple views::", "negative space", "blank page", "4koma"}


def chars(n):
    return [{"positive": "%s, 1.2::person %d::" % ("girl" if i % 2 == 0 else "boy", i)} for i in range(n)]


def novelai_lines(stderr):
    return [ln for ln in stderr.splitlines() if ln.startswith(NOVELAI)]


def contiguous(seq, sub):
    return any(seq[i:i + len(sub)] == sub for i in range(len(seq) - len(sub) + 1))


def pos_tokens(req):
    return H.tokens(H.body_json(req)["input"])


def neg_tokens(req):
    return H.tokens(H.body_json(req)["parameters"]["negative_prompt"])


def comic_config(bench):
    return bench.config(positive_prefix=COMIC_POS, negative_prefix=COMIC_NEG)


def assert_comic_stripped(req):
    pos, neg = pos_tokens(req), neg_tokens(req)
    assert contiguous(pos, POS_KEEP), "正向前缀没按漫画模式剔除（保留标签之间仍夹着别的词）：%s" % pos
    assert contiguous(neg, NEG_KEEP), "负向前缀没按漫画模式剔除（保留标签之间仍夹着别的词）：%s" % neg
    assert "1.3::speech bubble::" not in pos
    assert "1.5::multiple views::" not in neg


# 1
def test_01_comic_非布尔_退出码3_两行固定报错_不发请求(bench):
    cp, res, _ = bench.run({"prompt": BODY, "characters": ONE_CHAR, "comic": "yes"})
    assert cp.returncode == 3, cp.stderr
    lines = novelai_lines(cp.stderr)
    assert len(lines) == 2, lines
    head = NOVELAI + "comic 不合法：comic 只能是 true 或 false，收到 "
    assert lines[0].startswith(head) and "yes" in lines[0][len(head):], lines[0]
    assert lines[1] == NOVELAI + '正确写法示例："comic": true（出一页漫画）；普通出图不写这个键。'
    assert bench.fake.requests == []


# 2
def test_02_auto_compose_非布尔_退出码3_两行固定报错_不发请求(bench):
    cp, res, _ = bench.run({"prompt": BODY, "characters": ONE_CHAR, "auto_compose": 1})
    assert cp.returncode == 3, cp.stderr
    lines = novelai_lines(cp.stderr)
    assert len(lines) == 2, lines
    head = NOVELAI + "auto_compose 不合法：auto_compose 只能是 true 或 false，收到 "
    assert lines[0].startswith(head) and "1" in lines[0][len(head):], lines[0]
    assert lines[1] == (NOVELAI + '正确写法示例："auto_compose": false（第一视角与多人图不让脚本补景别、镜头与运动词）；'
                        '不写等于 true。')
    assert bench.fake.requests == []


# 3
def test_03_漫画模式_剔除前缀里的文字分格标签_结果列出被删原文_画风文件不变(bench):
    styles = bench.skill / "assets" / "styles.json"
    before = styles.read_bytes()
    cp, res, reqs = bench.run({"prompt": BODY, "characters": ONE_CHAR, "comic": True}, config=comic_config(bench))
    assert cp.returncode == 0, cp.stderr
    assert len(reqs) == 1
    assert_comic_stripped(reqs[0])
    out = json.loads(res.read_text(encoding="utf-8"))
    assert out.get("comic") is True
    removed = out.get("comic_removed_tags")
    assert isinstance(removed, list) and set(removed) == REMOVED and len(removed) == len(REMOVED), removed
    assert styles.read_bytes() == before


# 4
def test_04_漫画模式_前缀无可删标签_comic_removed_tags为空列表(bench):
    cfg = bench.config(positive_prefix="best quality, masterpiece", negative_prefix="lowres, bad anatomy")
    cp, res, reqs = bench.run({"prompt": BODY, "characters": ONE_CHAR, "comic": True}, config=cfg)
    assert cp.returncode == 0, cp.stderr
    assert len(reqs) == 1
    out = json.loads(res.read_text(encoding="utf-8"))
    assert out.get("comic") is True
    assert out.get("comic_removed_tags") == []


# 5a
def test_05a_漫画模式_20个角色_正常发请求(bench):
    cp, res, reqs = bench.run({"prompt": "a crowd in a plaza", "characters": chars(20), "comic": True})
    assert cp.returncode == 0, cp.stderr
    assert len(reqs) == 1


# 5b
def test_05b_漫画模式_21个角色_退出码3_不发请求(bench):
    cp, res, _ = bench.run({"prompt": "a crowd in a plaza", "characters": chars(21), "comic": True})
    assert cp.returncode == 3, cp.stderr
    assert "漫画模式角色最多 20 个，收到 21 个" in cp.stderr
    assert bench.fake.requests == []


# 6
def test_06_普通模式_7个角色_退出码3_原文案_不发请求(bench):
    cp, res, _ = bench.run({"prompt": "a group in a plaza", "characters": chars(7)})
    assert cp.returncode == 3, cp.stderr
    assert "角色最多 6 个，收到 7 个" in cp.stderr
    assert bench.fake.requests == []


# 7
def test_07_单人_auto_compose_false_不补景别镜头运动词_结果记false(bench):
    cfg = bench.config()
    cp0, _, r0 = bench.run({"prompt": BODY, "characters": ONE_CHAR}, config=cfg)
    assert cp0.returncode == 0, cp0.stderr
    added = set(H.auto_added(r0[0], cfg, BODY))
    assert added, "对照组（不写 auto_compose）脚本没有补任何词，无法对照"
    banned = added | H.MOTION_TOKENS
    cp, res, reqs = bench.run({"prompt": BODY, "characters": ONE_CHAR, "auto_compose": False}, config=cfg)
    assert cp.returncode == 0, cp.stderr
    leaked = [t for t in pos_tokens(reqs[0]) if t in banned]
    assert leaked == [], "auto_compose=false 仍补了词：%s" % leaked
    assert json.loads(res.read_text(encoding="utf-8")).get("auto_compose") is False


# 8
def test_08_两个角色_不写auto_compose_不补景别镜头运动词(bench):
    cfg = bench.config()
    cp0, _, r0 = bench.run({"prompt": BODY, "characters": ONE_CHAR}, config=cfg)
    assert cp0.returncode == 0, cp0.stderr
    added = set(H.auto_added(r0[0], cfg, BODY))
    assert added, "对照组（单人）脚本没有补任何词，无法对照"
    banned = added | H.MOTION_TOKENS
    cp, res, reqs = bench.run({"prompt": BODY, "characters": chars(2)}, config=cfg)
    assert cp.returncode == 0, cp.stderr
    leaked = [t for t in pos_tokens(reqs[0]) if t in banned]
    assert leaked == [], "两人图仍补了词：%s" % leaked


# 9
def test_09_0和1个角色_不写新键_请求体结果JSON和last_request与改动前逐字节相同(tmp_path, fake):
    for name in H.GOLDEN_CASES:
        files = H.run_golden_case(tmp_path / name / "b", fake, name)
        for fn, data in files.items():
            golden = (H.GOLDEN_DIR / fn).read_bytes()
            assert data == golden, "%s 与改动前金样本不一致" % fn
        out = json.loads(files["%s.result.json" % name])
        for k in ("comic", "comic_removed_tags", "auto_compose"):
            assert k not in out, "%s 结果 JSON 多出了键 %s" % (name, k)


# 10
def test_10_上一张漫画_本张revise不写comic也不写prompt_沿用漫画(bench):
    cfg = comic_config(bench)
    cp1, _, _ = bench.run({"prompt": BODY, "characters": ONE_CHAR, "comic": True}, config=cfg)
    assert cp1.returncode == 0, cp1.stderr
    cp, res, reqs = bench.run({"mode": "revise", "revision_instruction": "looking back"}, config=cfg)
    assert cp.returncode == 0, cp.stderr
    assert len(reqs) == 1
    assert_comic_stripped(reqs[0])
    assert json.loads(res.read_text(encoding="utf-8")).get("comic") is True


# 11
def test_11_上一张漫画_本张revise写了新prompt没写comic_按普通出图并提示(bench):
    cfg = comic_config(bench)
    cp1, _, _ = bench.run({"prompt": BODY, "characters": ONE_CHAR, "comic": True}, config=cfg)
    assert cp1.returncode == 0, cp1.stderr
    cp, res, reqs = bench.run({"mode": "revise", "prompt": "sitting on a bench, sunset", "characters": ONE_CHAR},
                              config=cfg)
    assert cp.returncode == 0, cp.stderr
    assert len(reqs) == 1
    assert "1.3::speech bubble::" in pos_tokens(reqs[0]), "普通出图不该剔正向前缀"
    assert "text" in neg_tokens(reqs[0]), "普通出图不该剔负向前缀"
    assert NOVELAI + "提示：修改模式写了新提示词但没写 comic，本张按普通出图" in cp.stderr.splitlines()
    assert json.loads(res.read_text(encoding="utf-8")).get("comic") is not True


# 12
BAD_INPUTS = [
    ("comic非布尔", {"prompt": BODY, "characters": ONE_CHAR, "comic": "yes"}),
    ("auto_compose非布尔", {"prompt": BODY, "characters": ONE_CHAR, "auto_compose": 1}),
    ("漫画21角色", {"prompt": BODY, "characters": chars(21), "comic": True}),
    ("普通7角色", {"prompt": BODY, "characters": chars(7)}),
]


def test_12_退出码3时_不生成结果JSON_不改last_request(bench):
    cfg = bench.config()
    cp0, _, _ = bench.run({"prompt": BODY, "characters": ONE_CHAR}, config=cfg)
    assert cp0.returncode == 0, cp0.stderr
    last = bench.state / "last_request.json"
    snapshot = last.read_bytes()
    for label, im in BAD_INPUTS:
        res_name = "bad_%s.json" % label
        cp, res, _ = bench.run(im, config=cfg, result_name=res_name)
        assert cp.returncode == 3, "%s 退出码应为 3，实际 %s" % (label, cp.returncode)
        assert not res.exists(), "%s 退出码 3 却生成了结果 JSON" % label
        assert last.read_bytes() == snapshot, "%s 退出码 3 却改了 last_request.json" % label


# 13
def test_13_SKILL文本_去姿势库_权重不超1点5_新增小节_多张图段落不变():
    text = (H.SKILL_SRC / "SKILL.md").read_text(encoding="utf-8")
    assert "pose_lookup" not in text
    assert "姿势库" not in text
    over = [m.group(0) for m in re.finditer(r"(?<![\w.])(-?\d+(?:\.\d+)?)::[^:\n]*?::", text)
            if float(m.group(1)) > 1.5]
    assert over == [], "权重超过 1.5：%s" % over
    assert "teenager" not in text
    headings = [ln for ln in text.splitlines() if re.match(r"^#{2,6}\s", ln)]
    for title in ("多人互动", "第一视角", "特写", "构图可行性", "漫画写法"):
        assert any(title in h for h in headings), "缺小节标题「%s」" % title
    golden = (H.GOLDEN_DIR / "skill_multi_image_block.md").read_text(encoding="utf-8")
    assert H.skill_multi_image_block(text) == golden


# 14
def test_14_SKILL文本_中间稿字段一句列出comic与auto_compose且只能写true或false():
    text = (H.SKILL_SRC / "SKILL.md").read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if "可选字段" in ln]
    assert lines, "找不到中间稿可选字段那一句"
    hit = [ln for ln in lines if "`comic`" in ln and "`auto_compose`" in ln]
    assert hit, "中间稿字段一句没列出 comic 与 auto_compose：%s" % lines
    assert any("只能" in ln and "true" in ln and "false" in ln for ln in hit), hit
