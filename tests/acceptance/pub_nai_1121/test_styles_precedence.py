# -*- coding: utf-8 -*-
"""pub-nai-1121 · 本地画风 / 出厂画风的读写优先级与坏文件处理（行为清单 7、8、9、10、11、12）。

画风页：在仓库副本里用 Flask 测试客户端调 /api/styles（NOVELAI_SKILL_ROOT 指向本用例的技能目录副本）。
出图脚本：命令行跑技能目录副本里的脚本，假 NovelAI 服务截下请求。
哪个画风生效，用画风文件里独一无二的 positive_prefix 标记词来认。
"""
import json

import pytest

import pn_helpers as H

LOCAL_MARK, FACTORY_MARK = "LOCALMARK_Q7", "FACTORYMARK_Q7"
LOCAL_OK = H.style_doc("style_l", LOCAL_MARK)
FACTORY_OK = H.style_doc("style_f", FACTORY_MARK)
LEAK = "LEAKWORD_Z9"
INTERMEDIATE = {"prompt": "standing in a park, sunny day", "characters": [{"positive": "girl, long hair"}]}
WARN_HEAD = "[novelai] 画风文件读不出或格式不对，本次不套画风：assets/"
ERR_500 = {"error": "internal_server_error"}
GET = ("GET", "/api/styles", None)


def setup(sandbox, local, factory):
    H.put(sandbox.local_styles, local)
    H.put(sandbox.factory_styles, factory)


def page(tmp_path, sandbox, calls):
    repo = H.light_copy(tmp_path / "repo")
    return H.page_calls(repo, calls, skill_root=sandbox.skill)


def page_mark(data):
    sid = data["active_by_bot"].get("testagent") or data.get("active")
    return next((s["positive_prefix"] for s in data["styles"] if s["id"] == sid), None)


def cli_marks(sandbox):
    cp, res, reqs = sandbox.run(INTERMEDIATE)
    assert cp.returncode == 0, cp.stderr[-800:]
    assert len(reqs) == 1, "应恰好发出 1 个生图请求，实际 %d" % len(reqs)
    return cp, {m for m in (LOCAL_MARK, FACTORY_MARK) if m in H.request_input(reqs[0])}


# 行为 7
def test_行为7_本地与出厂都没有_画风页显示空白底稿且保存后新建本地文件(tmp_path, sandbox):
    setup(sandbox, H.ABSENT, H.ABSENT)
    got, saved = page(tmp_path, sandbox, [GET, ("POST", "/api/styles", {"name": "新预设", "positive_prefix": "SAVEMARK"})])
    assert got["status"] == 200 and got["json"]["styles"] == [] and got["json"]["active_by_bot"] == {}, got
    assert saved["status"] == 200, saved
    assert sandbox.local_styles.is_file(), "保存后没有新建本地画风文件"
    doc = json.loads(sandbox.local_styles.read_text(encoding="utf-8"))
    assert [s["name"] for s in doc["styles"]] == ["新预设"] and doc["styles"][0]["positive_prefix"] == "SAVEMARK"
    assert doc["active_by_bot"] == {}
    assert not sandbox.factory_styles.exists(), "保存不该凭空建出厂文件"


def test_行为7_本地与出厂都没有_出图脚本不套画风且不告警(sandbox):
    setup(sandbox, H.ABSENT, H.ABSENT)
    cp, marks = cli_marks(sandbox)
    assert marks == set()
    assert [ln for ln in cp.stderr.splitlines() if "画风" in ln] == [], cp.stderr


# 行为 8
def test_行为8_只有出厂_画风页与出图脚本都按出厂(tmp_path, sandbox):
    setup(sandbox, H.ABSENT, FACTORY_OK)
    got, = page(tmp_path, sandbox, [GET])
    assert got["status"] == 200 and page_mark(got["json"]) == FACTORY_MARK, got
    _, marks = cli_marks(sandbox)
    assert marks == {FACTORY_MARK}


def test_行为8_只有出厂_保存后本地等于出厂内容加本次修改且出厂逐字节不变(tmp_path, sandbox):
    setup(sandbox, H.ABSENT, FACTORY_OK)
    factory_bytes = sandbox.factory_styles.read_bytes()
    saved, = page(tmp_path, sandbox, [("POST", "/api/styles", {"name": "追加的预设", "positive_prefix": "SAVEMARK"})])
    assert saved["status"] == 200, saved
    assert sandbox.factory_styles.read_bytes() == factory_bytes, "出厂文件被改了"
    doc = json.loads(sandbox.local_styles.read_text(encoding="utf-8"))
    assert doc["styles"][:1] == FACTORY_OK["styles"], "本地没有带上出厂的预设"
    assert [s["name"] for s in doc["styles"][1:]] == ["追加的预设"]
    assert doc["active"] == FACTORY_OK["active"] and doc["active_by_bot"] == FACTORY_OK["active_by_bot"]


# 行为 9
def test_行为9_本地正常_画风页与出图脚本都按本地_出厂内容不生效(tmp_path, sandbox):
    setup(sandbox, LOCAL_OK, FACTORY_OK)
    got, = page(tmp_path, sandbox, [GET])
    assert got["status"] == 200 and page_mark(got["json"]) == LOCAL_MARK, got
    assert FACTORY_MARK not in json.dumps(got["json"], ensure_ascii=False)
    _, marks = cli_marks(sandbox)
    assert marks == {LOCAL_MARK}


# 行为 10、11：坏文件（本地坏时出厂是好的；出厂坏时本地不存在）
_BAD = [
    pytest.param(b"", FACTORY_OK, "styles.local.json", id="本地_0字节"),
    pytest.param('{"styles": "%s"}' % LEAK, FACTORY_OK, "styles.local.json", id="本地_styles不是数组"),
    pytest.param(H.DIR, FACTORY_OK, "styles.local.json", id="本地_是目录"),
]
_BAD_FACTORY = [pytest.param(H.ABSENT, "   \n\t ", "styles.json", id="出厂_全空白")]


@pytest.mark.parametrize("local,factory,named", _BAD + _BAD_FACTORY)
def test_行为10_11_画风文件坏_画风页读与存都是500且不写盘(tmp_path, sandbox, local, factory, named):
    setup(sandbox, local, factory)
    before = H.tree_snapshot(sandbox.skill)
    got, saved = page(tmp_path, sandbox, [GET, ("POST", "/api/styles", {"name": "不该写进去"})])
    assert got["status"] == 500 and got["json"] == ERR_500, got
    assert saved["status"] == 500 and saved["json"] == ERR_500, saved
    assert H.tree_snapshot(sandbox.skill) == before, "500 的保存请求仍然改动了技能目录"


@pytest.mark.parametrize("local,factory,named", _BAD + _BAD_FACTORY)
def test_行为10_11_画风文件坏_出图脚本不套画风_stderr告警点名坏文件_退出码不变(sandbox, local, factory, named):
    setup(sandbox, H.ABSENT, H.ABSENT)
    base_cp, base_marks = cli_marks(sandbox)
    setup(sandbox, local, factory)
    cp, marks = cli_marks(sandbox)
    assert cp.returncode == base_cp.returncode
    assert marks == set(), "坏文件时不该套任何画风，却套了 %s" % marks
    warns = [ln for ln in cp.stderr.splitlines() if ln.startswith(WARN_HEAD)]
    assert len(warns) == 1, cp.stderr
    assert warns[0].startswith(WARN_HEAD + named) and ("styles.local.json" in warns[0]) == (named == "styles.local.json")
    assert LEAK not in cp.stderr, "告警里不该含文件内容"


# 行为 12
@pytest.mark.parametrize("local,factory,expected", [
    pytest.param(H.ABSENT, H.ABSENT, None, id="都没有"),
    pytest.param(H.ABSENT, FACTORY_OK, FACTORY_MARK, id="只有出厂"),
    pytest.param(LOCAL_OK, H.ABSENT, LOCAL_MARK, id="只有本地"),
    pytest.param(LOCAL_OK, FACTORY_OK, LOCAL_MARK, id="本地与出厂都有"),
])
def test_行为12_画风页与出图脚本对同一组场景给出一致的生效画风(tmp_path, sandbox, local, factory, expected):
    setup(sandbox, local, factory)
    got, = page(tmp_path, sandbox, [GET])
    assert got["status"] == 200, got
    _, marks = cli_marks(sandbox)
    assert page_mark(got["json"]) == expected
    assert marks == ({expected} if expected else set())
