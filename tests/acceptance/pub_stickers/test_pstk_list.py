# -*- coding: utf-8 -*-
"""列表与目录文件格式（接口文件 §0 item 形状、§1.3、§2 序列化、§3 兼容；行为清单 7、8、14、28 的格式部分）。"""
import json
import os
import re
import sys

import pytest

import pstk_helpers as K


# ── 列表 ─────────────────────────────────────────────────────────────────────

def test_列表_目录文件不存在_按空库返回(c, paths):
    r = c.get(K.LIST)
    assert r.status_code == 200 and r.get_json() == K.EMPTY_LIST


def test_列表_目录文件不存在_不创建任何文件(c, paths):
    c.get(K.LIST)
    assert K.listing(paths.shared) == set() and not paths.dir.exists()


def test_列表_形状_顺序_不完整只计数_分类按首次出现(c, paths):
    lib = K.sample_library(paths)
    lib["nsfw"].append({"url": "", "label": "成人占位2", "category": "占位类甲",
                        "path": str(paths.dir / "nsfw" / "009_0000000000.gif"), "size": 7})   # 文件不在
    lib["nsfw"].append({"label": "缺路径"})
    K.write_lib(paths, lib)
    r = c.get(K.LIST)
    out = r.get_json()
    assert r.status_code == 200
    assert set(out) == {"ok", "groups", "categories", "incomplete", "limits"} and out["ok"] is True
    assert [i["label"] for i in out["groups"]["sfw"]] == ["占位0", "占位1", "占位2"]
    assert [i["label"] for i in out["groups"]["nsfw"]] == ["成人占位0", "成人占位1", "成人占位2"]
    assert out["incomplete"] == {"sfw": 2, "nsfw": 1}
    assert out["categories"] == ["占位类甲", "占位类乙"]
    assert out["limits"] == K.LIMITS


def test_列表_nsfw条目的item形状逐键相等(c, paths):
    lib = K.sample_library(paths)
    K.write_lib(paths, lib)
    e = lib["nsfw"][0]
    name = K.base(e["path"])
    got = c.get(K.LIST).get_json()["groups"]["nsfw"][0]
    assert got == {"id": "nsfw/" + name, "group": "nsfw", "label": "成人占位0", "category": "占位类甲",
                   "url": e["url"], "file": name, "size": e["size"], "missing": False,
                   "img": "/console/stickers/img/nsfw/" + name}


def test_列表_sfw条目带category键也给null_图片不在时missing为true(c, paths):
    lib = K.sample_library(paths)
    lib["sfw"][0]["category"] = "sfw 档的分类"
    lib["sfw"][0]["path"] = str(paths.dir / "sfw" / "050_0000000000.gif")   # 文件不在
    K.write_lib(paths, lib)
    s0 = c.get(K.LIST).get_json()["groups"]["sfw"][0]
    assert s0["category"] is None and s0["group"] == "sfw" and s0["missing"] is True


@pytest.mark.parametrize("content,reason", [
    (b'{"sfw": [', None),
    (b"\xff\xfe{}", "不是 UTF-8 文本"),
    (b"[]", "顶层不是对象"),
    (b'{"sfw": {}, "nsfw": []}', "sfw 不是列表"),
    (b'{"sfw": []}', "nsfw 不是列表"),
    (b'{"sfw": [], "nsfw": null}', "nsfw 不是列表"),
], ids=["json", "utf8", "array", "sfw_obj", "nsfw_missing", "nsfw_null"])
def test_列表_文件损坏_500_library_broken_文案写明原因(c, paths, content, reason):
    paths.json.write_bytes(content)
    b = K.err(c.get(K.LIST), "library_broken", 500)
    msg = b["message"]
    assert msg.startswith(K.BROKEN_HEAD) and msg.endswith(K.BROKEN_TAIL), msg
    inner = msg[len(K.BROKEN_HEAD):-len(K.BROKEN_TAIL)]
    if reason is None:
        assert re.fullmatch(r"不是合法的 JSON，第 \d+ 行第 \d+ 列", inner), inner
    else:
        assert inner == reason
    assert paths.json.read_bytes() == content


# ── 目录文件格式（写回）──────────────────────────────────────────────────────

def test_改了再改回来_目录文件逐字节等于原文件(c, paths, sample):
    before = K.raw(paths)
    assert not before.endswith(b"\n")
    name = K.base(sample["sfw"][0]["path"])
    K.ok_item(c.patch(K.item_api("sfw", name), json={"label": "临时"}))
    K.ok_item(c.patch(K.item_api("sfw", name), json={"label": "占位0"}))
    assert K.raw(paths) == before


def test_上传后_文本等于预期数据的dumps_多余键_条目先后_新条目键顺序(c, paths, sample):
    expected = json.loads(json.dumps(sample))
    data = K.webp("new", 33)
    K.ok_item(K.upload(c, data, label="  新的占位  ", category="sfw 带了分类也不写", filename="x.webp"))
    expected["sfw"].append({"url": "", "label": "新的占位",
                            "path": str(paths.dir / "sfw" / ("003_%s.webp" % K.sha10(data))), "size": len(data)})
    assert K.raw(paths) == K.dumps(expected).encode("utf-8")
    data2 = K.jpg("nnew", 20)
    K.ok_item(K.upload(c, data2, group="nsfw", label="成人新", category="占位类乙", filename="y.jpg"))
    expected["nsfw"].append({"url": "", "label": "成人新", "category": "占位类乙",
                             "path": str(paths.dir / "nsfw" / ("002_%s.jpg" % K.sha10(data2))), "size": len(data2)})
    assert K.raw(paths) == K.dumps(expected).encode("utf-8")
    got = K.load(paths)
    assert list(got) == ["sfw", "version", "nsfw"]
    assert list(got["sfw"][-1]) == ["url", "label", "path", "size"]
    assert list(got["nsfw"][-1]) == ["url", "label", "category", "path", "size"]
    assert list(got["sfw"][1]) == ["url", "label", "path", "size", "note"]
    assert got["sfw"][2] == sample["sfw"][2] and got["sfw"][4] == "占位串"


# ── 随包安装生成的清单（行为 8、§3）───────────────────────────────────────────

def _installed(paths, sep=None):
    """随包安装风格：带 id/tier、sfw 有 category、没有 size、结尾有换行。sep 给定时 path 用该分隔符拼。"""
    def p(group, name):
        if sep is None:
            return K.put_image(paths, group, name, K.jpg(group + name))
        return sep.join(["C:", "Users", "someone", "stickers", group, name])
    lib = {"sfw": [
        {"id": "sfw/022_1bb0500050.jpg", "tier": "sfw", "url": "", "label": "Kiss", "category": None,
         "path": p("sfw", "022_1bb0500050.jpg")},
        {"id": "sfw/gz_hd_110.jpg", "tier": "sfw", "url": "", "label": "回答 110", "category": "回答",
         "path": p("sfw", "gz_hd_110.jpg")}],
        "nsfw": [
        {"id": "nsfw/n_001.jpg", "tier": "nsfw", "url": "", "label": "成人一", "category": "占位类甲",
         "path": p("nsfw", "n_001.jpg")}]}
    K.write_lib(paths, lib, trailing_newline=True)
    return lib


def test_随包清单_列表里file是单层文件名_sfw分类为null_size为null(c, paths):
    _installed(paths)
    g = c.get(K.LIST).get_json()["groups"]
    assert [i["file"] for i in g["sfw"]] == ["022_1bb0500050.jpg", "gz_hd_110.jpg"]
    assert [i["category"] for i in g["sfw"]] == [None, None]
    assert [i["size"] for i in g["sfw"] + g["nsfw"]] == [None, None, None]
    assert g["nsfw"][0]["category"] == "占位类甲" and g["nsfw"][0]["missing"] is False


def test_随包清单_反斜杠path_file取最后一段(c, paths):
    _installed(paths, sep="\\")
    g = c.get(K.LIST).get_json()["groups"]
    assert [i["file"] for i in g["sfw"]] == ["022_1bb0500050.jpg", "gz_hd_110.jpg"]
    assert g["nsfw"][0]["file"] == "n_001.jpg"
    assert [i["id"] for i in g["sfw"]] == ["sfw/022_1bb0500050.jpg", "sfw/gz_hd_110.jpg"]


def test_随包清单_能改_其他键原样_结尾换行消失(c, paths):
    lib = _installed(paths)
    K.ok_item(c.patch(K.item_api("sfw", "gz_hd_110.jpg"), json={"label": "回答改"}))
    lib["sfw"][1]["label"] = "回答改"
    assert K.raw(paths) == K.dumps(lib).encode("utf-8")


def test_随包清单_能删_图挪进回收目录(c, paths):
    lib = _installed(paths)
    r = c.delete(K.item_api("nsfw", "n_001.jpg"), json={})
    assert r.status_code == 200 and r.get_json() == {"ok": True, "moved": True}
    lib["nsfw"] = []
    assert K.raw(paths) == K.dumps(lib).encode("utf-8")
    assert (paths.removed / "nsfw" / "n_001.jpg").read_bytes() == K.jpg("nsfwn_001.jpg")


# ── STICKERS_DIR 不合规（公开版差异）─────────────────────────────────────────

def test_STICKERS_DIR是相对路径_列表500_library_broken_说明要求(c, paths, monkeypatch):
    monkeypatch.chdir(paths.tmp)
    monkeypatch.setenv("STICKERS_DIR", "rel-stickers")
    K.write_lib(paths, {"sfw": [], "nsfw": []})
    b = K.err(c.get(K.LIST), "library_broken", 500)
    assert "STICKERS_DIR" in b["message"]


def test_STICKERS_DIR是相对路径_上传500_library_broken_什么都不写(c, paths, monkeypatch):
    monkeypatch.chdir(paths.tmp)
    monkeypatch.setenv("STICKERS_DIR", "rel-stickers")
    K.err(K.upload(c, K.gif("rel")), "library_broken", 500, contains="STICKERS_DIR")
    assert not paths.json.exists() and not (paths.tmp / "rel-stickers").exists()
    assert K.listing(paths.shared) == set()


@pytest.mark.skipif(sys.platform != "win32", reason="不带盘符的反斜杠路径只在 Windows 上算不合规")
def test_Windows_STICKERS_DIR不带盘符_列表500_library_broken(c, paths, monkeypatch):
    monkeypatch.setenv("STICKERS_DIR", "\\stickers")
    K.err(c.get(K.LIST), "library_broken", 500, contains="STICKERS_DIR")
    assert os.path.exists(str(paths.json)) is False
