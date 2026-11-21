# -*- coding: utf-8 -*-
"""pub-nai-1121 · 出图脚本的人物 negative（行为清单 13、14、15、16）。

只走命令行 + 本机假 NovelAI 服务；断言请求体里的角色负面槽，不碰脚本内部。
金样本（zero/one）复用 tests/acceptance/pub_comic_nai/golden，它是"改前"的逐字节基准。
"""
import copy
import json

import pytest

import pn_helpers as H

P = H.P
BODY = "standing in a park, sunny day"
PREFIX = "[novelai] characters 不合法："


def two_chars(first_negative):
    """第 1 个角色按需带 negative，第 2 个角色不写。"""
    first = {"positive": "girl, long hair"}
    if first_negative is not None:
        first["negative"] = first_negative
    return [first, {"positive": "boy, short hair"}]


def run_with_second_negative(sandbox, value):
    return sandbox.run({"prompt": BODY, "characters": [{"positive": "girl, long hair"},
                                                      {"positive": "boy, short hair", "negative": value}]})


# 行为 13
def test_行为13_角色写negative_对应负面槽是去首尾空白后的值_坐标同正面_未写的角色槽为空串(sandbox):
    cp, res, reqs = sandbox.run({"prompt": BODY, "characters": two_chars("  futanari, penis \n")})
    assert cp.returncode == 0, cp.stderr[-800:]
    assert len(reqs) == 1
    body = P.body_json(reqs[0])
    pos, neg = H.char_captions(body, "v4_prompt"), H.char_captions(body, "v4_negative_prompt")
    assert len(pos) == len(neg) == 2
    assert [n["char_caption"] for n in neg] == ["futanari, penis", ""]
    assert [n["centers"] for n in neg] == [p["centers"] for p in pos], "负面槽的坐标必须与正面相同"
    assert all("futanari" not in p["char_caption"] for p in pos), "negative 不该混进正面角色提示词"


# 行为 14
@pytest.mark.parametrize("extra", [
    pytest.param({}, id="不写"),
    pytest.param({"negative": None}, id="null"),
    pytest.param({"negative": " \t\n  "}, id="全空白"),
])
def test_行为14_negative不写_null或全空白_请求体与结果文件与改前逐字节相同(tmp_path, fake, extra):
    case = copy.deepcopy(P.GOLDEN_CASES["one"])
    case["characters"][0].update(extra)
    sb = H.Sandbox(tmp_path / "b", fake)
    cp, res, reqs = sb.run(case, result_name="result.json")
    assert cp.returncode == 0, cp.stderr[-800:]
    assert len(reqs) == 1
    assert P.normalize(reqs[0]["body"], sb.root) == (P.GOLDEN_DIR / "one.request.json").read_bytes(), \
        "请求体与改前金样本不一致"
    assert P.normalize(res.read_bytes(), sb.root) == (P.GOLDEN_DIR / "one.result.json").read_bytes(), \
        "结果文件与改前金样本不一致"


# 行为 15
@pytest.mark.parametrize("value,reason", [
    pytest.param(123, "第 2 个角色的 negative 必须是字符串，收到 int", id="整数"),
    pytest.param(["a"], "第 2 个角色的 negative 必须是字符串，收到 list", id="列表"),
    pytest.param({"x": 1}, "第 2 个角色的 negative 必须是字符串，收到 dict", id="对象"),
    pytest.param(True, "第 2 个角色的 negative 必须是字符串，收到 bool", id="布尔"),
    pytest.param("  " + "x" * 1001 + "  ", "第 2 个角色的 negative 太长：最多 1000 字，收到 1001 字", id="去空白后1001字"),
    pytest.param("好" * 1001, "第 2 个角色的 negative 太长：最多 1000 字，收到 1001 字", id="中文1001字"),
])
def test_行为15_negative非法_退出码3_报具体原因_不发请求不写图片与记录(sandbox, value, reason):
    cp, res, reqs = run_with_second_negative(sandbox, value)
    assert cp.returncode == 3, cp.stderr[-800:]
    lines = [ln for ln in cp.stderr.splitlines() if ln.startswith(PREFIX)]
    assert len(lines) == 1 and reason in lines[0], cp.stderr
    assert sandbox.fake.requests == [], "非法输入不该发出任何请求"
    assert not res.exists(), "非法输入不该写结果文件"
    assert sandbox.generated_files() == [], "非法输入不该写图片或记录"


@pytest.mark.parametrize("value", [
    pytest.param("n" * 1000, id="恰好1000字"),
    pytest.param("  " + "n" * 1000 + " \n", id="首尾空白不计入字数"),
])
def test_行为15_negative恰好1000字_合法并原样进入负面槽(sandbox, value):
    cp, res, reqs = run_with_second_negative(sandbox, value)
    assert cp.returncode == 0, cp.stderr[-800:]
    assert len(reqs) == 1
    neg = H.char_captions(P.body_json(reqs[0]), "v4_negative_prompt")
    assert [n["char_caption"] for n in neg] == ["", "n" * 1000]


# 行为 16
def test_行为16_400后平铺重发_negative随角色丢弃_不出现在全局负面里(tmp_path, fake_reject_first):
    sb = H.Sandbox(tmp_path / "b", fake_reject_first)
    cp, res, reqs = sb.run({"prompt": BODY, "characters": two_chars("NEGMARK_A, futanari")})
    assert cp.returncode == 0, cp.stderr[-800:]
    assert len(reqs) == 2, "第一次被 400 后应平铺重发一次，实际请求数 %d" % len(reqs)
    first = H.char_captions(P.body_json(reqs[0]), "v4_negative_prompt")
    assert first[0]["char_caption"] == "NEGMARK_A, futanari", "前提：第一次请求应带角色 negative"
    second = P.body_json(reqs[1])
    assert "NEGMARK_A" not in second["parameters"]["negative_prompt"], "角色 negative 漏进了全局负面"
    assert "NEGMARK_A" not in json.dumps(second, ensure_ascii=False), "平铺重发的请求体里不该再有角色 negative"
