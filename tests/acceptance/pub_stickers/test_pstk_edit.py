# -*- coding: utf-8 -*-
"""改与移除（接口文件 §1.6、§1.7；行为清单 24-26）：原地改、找不到、同档重复、档目录外拒绝、
移除挪进 .removed/<档>/、重名加时间后缀、绝不删除、挪不动时条目照样删。"""
import json
import os
import sys

import pytest

import pstk_helpers as K

NO_SYMLINK = pytest.mark.skipif(sys.platform == "win32", reason="Windows 上不测符号链接")


def _copy(lib):
    return json.loads(json.dumps(lib))


# ── 改 ───────────────────────────────────────────────────────────────────────

def test_改sfw标签_原地改_其他键与位置不动_sfw给的分类忽略(c, paths, sample):
    target = sample["sfw"][1]   # 带多余键 note 的那条
    name = K.base(target["path"])
    item = K.ok_item(c.patch(K.item_api("sfw", name), json={"label": "  改过的  ", "category": "sfw 不认分类"}))
    assert item["label"] == "改过的" and item["category"] is None and item["id"] == "sfw/" + name
    expected = _copy(sample)
    expected["sfw"][1]["label"] = "改过的"
    assert K.raw(paths) == K.dumps(expected).encode("utf-8")


def test_改nsfw标签和分类_item只变这两个字段(c, paths):
    item = K.ok_item(K.upload(c, K.gif("e1"), group="nsfw", category="甲类", label="原标签"))
    got = K.ok_item(c.patch(K.item_api("nsfw", item["file"]), json={"label": "新标签", "category": "乙类"}))
    assert got == dict(item, label="新标签", category="乙类")


def test_改nsfw分类_原地改_文件里其余不动(c, paths, sample):
    e0 = sample["nsfw"][0]
    K.ok_item(c.patch(K.item_api("nsfw", K.base(e0["path"])), json={"category": "新分类"}))
    expected = _copy(sample)
    expected["nsfw"][0]["category"] = "新分类"
    assert K.raw(paths) == K.dumps(expected).encode("utf-8")


def test_改nsfw分类_原来没有分类键的补在label后面(c, paths, sample):
    lib = _copy(sample)
    del lib["nsfw"][1]["category"]
    lib["nsfw"][1]["extra"] = "占位"
    K.write_lib(paths, lib)
    item = K.ok_item(c.patch(K.item_api("nsfw", K.base(lib["nsfw"][1]["path"])),
                             json={"label": "成人改", "category": "补上的"}))
    assert item["category"] == "补上的" and item["label"] == "成人改"
    assert list(K.load(paths)["nsfw"][1]) == ["url", "label", "category", "path", "size", "extra"]


@pytest.mark.parametrize("body,message", [
    ({}, "没有要改的内容"), ({"category": "只给 sfw 分类"}, "没有要改的内容"),
    ({"label": ""}, None), ({"label": "x" * 61}, None), ({"label": "a\nb"}, None), ({"label": None}, None),
], ids=["empty", "sfw_cat_only", "blank", "long", "newline", "null"])
def test_改_入参不合规_422_invalid_文件不动(c, paths, sample, body, message):
    before = K.raw(paths)
    K.err(c.patch(K.item_api("sfw", K.base(sample["sfw"][0]["path"])), json=body), "invalid", 422, message=message)
    assert K.raw(paths) == before


def test_改_nsfw分类全空白_422_invalid(c, paths, sample):
    before = K.raw(paths)
    K.err(c.patch(K.item_api("nsfw", K.base(sample["nsfw"][0]["path"])), json={"category": " "}), "invalid", 422)
    assert K.raw(paths) == before


def test_改_请求体是数组_422_invalid(c, paths, sample):
    K.err(c.patch(K.item_api("sfw", K.base(sample["sfw"][0]["path"])), data="[]", content_type="application/json"),
          "invalid", 422, message="请求体必须是 JSON 对象")


# ── 找不到、同档重复 ─────────────────────────────────────────────────────────

# kind：biz = 路由能匹配，回业务 404 并核对文案；any = 路由本身匹配不上，只要求 404
NOT_FOUND = [
    ("sfw", "999_0000000000.gif", "absent"), ("nsfw", None, "wrong_group"), ("other", None, "unknown_group"),
    ("sfw", "..", "dotdot"), ("sfw", ".hidden.gif", "dot"), ("sfw", "a.gif%0A", "newline"),
]


def _nf_message(case):
    return "库里没有这一张（可能已被移除），请刷新后再试" if case in ("absent", "wrong_group") else "没有这一张"


@pytest.mark.parametrize("group,name,case", NOT_FOUND, ids=[x[2] for x in NOT_FOUND])
def test_PATCH找不到_404_not_found_文件不动(c, paths, sample, group, name, case):
    name = name if name is not None else K.base(sample["sfw"][0]["path"])   # sfw 的文件名拿到别的档去找
    before = K.raw(paths)
    K.err(c.patch(K.item_api(group, name), json={"label": "新"}), "not_found", 404, message=_nf_message(case))
    assert K.raw(paths) == before


@pytest.mark.parametrize("group,name,case", NOT_FOUND, ids=[x[2] for x in NOT_FOUND])
def test_DELETE找不到_404_not_found_文件不动(c, paths, sample, group, name, case):
    name = name if name is not None else K.base(sample["sfw"][0]["path"])
    before = K.raw(paths)
    K.err(c.delete(K.item_api(group, name), json={}), "not_found", 404, message=_nf_message(case))
    assert K.raw(paths) == before and not paths.removed.exists()


@pytest.mark.parametrize("path", ["/api/console/stickers/sfw/a/b.gif", "/api/console/stickers/sfw/"],
                         ids=["slash", "empty"])
def test_名字含斜杠或为空_PATCH回404_文件不动(c, paths, sample, path):
    before = K.raw(paths)
    assert c.patch(path, json={"label": "新"}).status_code == 404
    assert K.raw(paths) == before


def test_同档两条同名_PATCH_409_ambiguous(c, paths, sample):
    dup = dict(sample["sfw"][0], label="同名的另一条")
    sample["sfw"].append(dup)
    K.write_lib(paths, sample)
    before = K.raw(paths)
    K.err(c.patch(K.item_api("sfw", K.base(dup["path"])), json={"label": "新"}), "ambiguous", 409,
          message="这一档里有不止一条用这个文件名，请先手工处理表情库文件")
    assert K.raw(paths) == before


def test_同档两条同名_DELETE_409_ambiguous(c, paths, sample):
    dup = dict(sample["sfw"][0], label="同名的另一条")
    sample["sfw"].append(dup)
    K.write_lib(paths, sample)
    before = K.raw(paths)
    K.err(c.delete(K.item_api("sfw", K.base(dup["path"])), json={}), "ambiguous", 409)
    assert K.raw(paths) == before


def test_同档有重名时别的档照常能改(c, paths, sample):
    sample["sfw"].append(dict(sample["sfw"][0], label="同名的另一条"))
    K.write_lib(paths, sample)
    K.ok_item(c.patch(K.item_api("nsfw", K.base(sample["nsfw"][0]["path"])), json={"label": "别的档照常"}))


# ── 档目录外 ─────────────────────────────────────────────────────────────────

OUTSIDE_MSG = "这一条的图片不在本档的图片目录里，不能在这里操作"


@pytest.fixture
def outside_lib(paths, sample, tmp_path):
    outside = tmp_path / "outside.gif"
    outside.write_bytes(K.gif("outside"))
    other_group = K.put_image(paths, "nsfw", "070_1111111111.gif", K.gif("other-group"))
    K.put_image(paths, "nsfw", "072_3333333333.gif", K.gif("dotdot"))
    sample["sfw"] += [
        {"url": "", "label": "外面的", "path": str(outside), "size": 1},
        {"url": "", "label": "别的档的", "path": other_group, "size": 1},
        {"url": "", "label": "绕出去的", "path": os.path.join(str(paths.dir / "sfw"), "..", "nsfw", "072_3333333333.gif"),
         "size": 1},
    ]
    if sys.platform != "win32":
        link = paths.dir / "sfw" / "071_2222222222.gif"
        os.symlink(str(outside), str(link))
        sample["sfw"].append({"url": "", "label": "链接出去的", "path": str(link), "size": 1})
    K.write_lib(paths, sample)
    return outside


OUTSIDE = [pytest.param("outside.gif", id="outside"), pytest.param("070_1111111111.gif", id="other_group"),
           pytest.param("072_3333333333.gif", id="dotdot_path"),
           pytest.param("071_2222222222.gif", id="symlink_out", marks=NO_SYMLINK)]


@pytest.mark.parametrize("name", OUTSIDE)
def test_条目指向档目录外_PATCH_404_一个字节都不动(c, paths, outside_lib, name):
    before, digests = K.raw(paths), K.all_image_digests(paths.dir)
    K.err(c.patch(K.item_api("sfw", name), json={"label": "新"}), "not_found", 404, message=OUTSIDE_MSG)
    assert K.raw(paths) == before and K.all_image_digests(paths.dir) == digests


@pytest.mark.parametrize("name", OUTSIDE)
def test_条目指向档目录外_DELETE_404_图不挪_外面的文件不动(c, paths, outside_lib, name):
    before, digests = K.raw(paths), K.all_image_digests(paths.dir)
    K.err(c.delete(K.item_api("sfw", name), json={}), "not_found", 404, message=OUTSIDE_MSG)
    assert K.raw(paths) == before and K.all_image_digests(paths.dir) == digests
    assert outside_lib.read_bytes() == K.gif("outside") and not paths.removed.exists()


# ── 移除 ─────────────────────────────────────────────────────────────────────

def test_移除_200_moved为true_后面的条目前移_图挪进回收目录且内容不变(c, paths, sample):
    digests = K.all_image_digests(paths.dir)
    target = sample["sfw"][1]
    r = c.delete(K.item_api("sfw", K.base(target["path"])), json={})
    assert r.status_code == 200 and r.get_json() == {"ok": True, "moved": True}
    expected = _copy(sample)
    del expected["sfw"][1]
    assert K.raw(paths) == K.dumps(expected).encode("utf-8")
    assert not os.path.exists(target["path"])
    assert (paths.removed / "sfw" / K.base(target["path"])).read_bytes() == K.gif("s1", 41)
    assert K.all_image_digests(paths.dir) == digests   # 绝不删除任何图片


def test_移除_再删一次_404_not_found(c, paths, sample):
    p = K.item_api("sfw", K.base(sample["sfw"][0]["path"]))
    assert c.delete(p, json={}).status_code == 200
    K.err(c.delete(p, json={}), "not_found", 404)


def test_移除_回收目录里重名_扩展名前加时间后缀_已有的不动(c, paths, sample):
    target = sample["nsfw"][0]
    name = K.base(target["path"])
    (paths.removed / "nsfw").mkdir(parents=True)
    (paths.removed / "nsfw" / name).write_bytes(b"older-removed")
    digests = K.all_image_digests(paths.dir)
    r = c.delete(K.item_api("nsfw", name), json={})
    assert r.status_code == 200 and r.get_json() == {"ok": True, "moved": True}
    stem, ext = name.rsplit(".", 1)
    others = [n for n in K.listing(paths.removed / "nsfw") if n != name]
    assert len(others) == 1 and others[0].startswith(stem + "-") and others[0].endswith("." + ext)
    assert len(others[0]) == len(stem) + len("-YYYYmmdd-HHMMSS.") + len(ext)
    assert (paths.removed / "nsfw" / name).read_bytes() == b"older-removed"
    assert K.all_image_digests(paths.dir) == digests


def test_移除_图片已经不在_200_moved为false_条目已删_不带请求体也行(c, paths, sample):
    sample["sfw"].append({"url": "", "label": "图没了", "path": str(paths.dir / "sfw" / "050_0000000000.gif"), "size": 3})
    K.write_lib(paths, sample)
    r = c.delete(K.item_api("sfw", "050_0000000000.gif"))
    assert r.status_code == 200 and r.get_json() == {"ok": True, "moved": False}
    assert all(isinstance(e, str) or e.get("label") != "图没了" for e in K.load(paths)["sfw"])


def test_移除_回收目录被文件占着挪不动_moved为false_图还在原处_条目已删(c, paths, sample):
    paths.removed.write_bytes(b"not a directory")
    target = sample["sfw"][0]
    r = c.delete(K.item_api("sfw", K.base(target["path"])), json={})
    assert r.status_code == 200 and r.get_json() == {"ok": True, "moved": False}
    assert os.path.exists(target["path"]) and paths.removed.read_bytes() == b"not a directory"
    assert K.base(target["path"]) not in [K.base(e["path"]) for e in K.load(paths)["sfw"]
                                          if isinstance(e, dict) and "path" in e]
