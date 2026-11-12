# -*- coding: utf-8 -*-
"""上传（接口文件 §1.4、§2 新条目；行为清单 9-13）：四种类型、拒绝的内容、标签与分类、文件名、查重、入参错误。"""
import os
import sys

import pytest

import pstk_helpers as K

ONLY4 = "只支持 GIF、JPG、PNG、WEBP 格式的图片"


def _nothing_written(paths):
    assert not paths.json.exists() and K.stray_temps(paths) == []
    for g in ("sfw", "nsfw"):
        assert K.listing(paths.dir / g) == set()


# ── 成功 ─────────────────────────────────────────────────────────────────────

def test_上传GIF_标签去首尾空白_item逐键相等(c, paths):
    data = K.gif("u1")
    item = K.ok_item(K.upload(c, data, label=" 生气 2 "))
    name = "000_%s.gif" % K.sha10(data)
    assert item == {"id": "sfw/" + name, "group": "sfw", "label": "生气 2", "category": None, "url": "",
                    "file": name, "size": len(data), "missing": False, "img": "/console/stickers/img/sfw/" + name}


def test_上传GIF_条目追加在sfw末尾_键顺序url_label_path_size(c, paths, sample):
    data = K.gif("tail")
    item = K.ok_item(K.upload(c, data, label="末尾"))
    last = K.load(paths)["sfw"][-1]
    assert last == {"url": "", "label": "末尾", "path": str(paths.dir / "sfw" / item["file"]), "size": len(data)}
    assert list(last) == ["url", "label", "path", "size"]


@pytest.mark.parametrize("ext,data", [
    ("gif", K.gif("t")), ("gif", K.gif87("t87")), ("png", K.png("t")), ("jpg", K.jpg("t")), ("webp", K.webp("t")),
], ids=["gif89a", "gif87a", "png", "jpg", "webp"])
def test_四种类型_按文件头定扩展名_图片字节原样落盘(c, paths, ext, data):
    item = K.ok_item(K.upload(c, data, filename="whatever.bin"))
    name = "000_%s.%s" % (K.sha10(data), ext)
    assert item["file"] == name and item["size"] == len(data)
    assert (paths.dir / "sfw" / name).read_bytes() == data


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 不断言权限位")
def test_上传的新图权限0644(c, paths):
    item = K.ok_item(K.upload(c, K.png("perm")))
    assert os.stat(str(paths.dir / "sfw" / item["file"])).st_mode & 0o777 == 0o644


def test_data可以带data前缀的base64(c, paths):
    item = K.ok_item(K.upload(c, "data:image/png;base64," + K.b64(K.png("u2")), group="nsfw", category="占位类",
                              filename="b.png"))
    assert item["category"] == "占位类" and item["file"].endswith(".png")


def test_恰好10MiB的图放行(c, paths):
    ok = K.png("ok", K.MAX_IMG - 8)
    assert K.ok_item(K.upload(c, ok))["size"] == K.MAX_IMG


# ── 拒绝的内容 ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("data,message,contains", [
    (b"just some text, not an image", None, "只支持"),
    (b"<!DOCTYPE html><html><body>page</body></html>", "这不是图片文件，" + ONLY4, None),
    (b"\xef\xbb\xbf  \n<html>", "这不是图片文件，" + ONLY4, None),
    (b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>', "不支持 SVG，" + ONLY4, None),
    (b"<svg xmlns='http://www.w3.org/2000/svg'/>", "不支持 SVG，" + ONLY4, None),
    (K.HEIC, "HEIC 不支持，请先转成 JPG", None),
    (b"\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00", "HEIC 不支持，请先转成 JPG", None),
    (b"\x00\x00\x00\x18ftypheix\x00\x00\x00\x00", "HEIC 不支持，请先转成 JPG", None),
    (b"RIFF\x10\x00\x00\x00WAVEfmt ", ONLY4, None),
], ids=["text", "html", "bom_html", "svg_xml", "svg", "heic", "mif1", "heix", "riff_wave"])
def test_不是四种图片_422_unsupported_type_什么都不写(c, paths, data, message, contains):
    K.err(K.upload(c, data), "unsupported_type", 422, message=message, contains=contains)
    _nothing_written(paths)


def test_解码后超过10MiB_413_too_large_什么都不写(c, paths):
    big = K.png("big", K.MAX_IMG - 8 + 1)
    assert len(big) == K.MAX_IMG + 1
    K.err(K.upload(c, big), "too_large", 413, message="图片超过 10 MiB")
    _nothing_written(paths)


# ── 标签、档、分类 ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("label", ["", "   ", "\n", "字" * 61, "前\n后", "前\r后", "响\x07铃", "删\x7f", "行 分",
                                   "段 分", "c1\x85后", "\ud800", None, 12, ["占位"]],
                         ids=["empty", "spaces", "newline_only", "61", "lf", "cr", "bel", "del", "u2028", "u2029",
                              "c1", "surrogate", "none", "int", "list"])
def test_标签不合规_422_invalid_文案以标签开头(c, paths, label):
    K.err(K.upload(c, K.gif(), label=label), "invalid", 422, prefix="标签")
    _nothing_written(paths)


@pytest.mark.parametrize("label,want", [
    ("\t 生气 2 \n", "生气 2"), ("字" * 60, "字" * 60), ("一家\U0001F468‍\U0001F469", "一家\U0001F468‍\U0001F469"),
], ids=["strip", "60", "zwj"])
def test_标签_去首尾空白_60字与表情连接符放行(c, paths, label, want):
    assert K.ok_item(K.upload(c, K.gif(want), label=label))["label"] == want


@pytest.mark.parametrize("group", ["", "SFW", "other", None, 1], ids=["empty", "upper", "other", "none", "int"])
def test_档只许sfw与nsfw_422_invalid(c, paths, group):
    K.err(K.upload(c, K.gif(), group=group, category="占位类"), "invalid", 422, message="档只能是 sfw 或 nsfw")
    _nothing_written(paths)


def test_nsfw缺分类_422_成人档要选一个分类(c, paths):
    K.err(K.upload(c, K.gif(), group="nsfw"), "invalid", 422, message="成人档要选一个分类")
    _nothing_written(paths)


@pytest.mark.parametrize("cat", ["", "  ", "x" * 61, "换\n行", 3], ids=["empty", "spaces", "61", "newline", "int"])
def test_nsfw分类不合规_422_invalid(c, paths, cat):
    K.err(K.upload(c, K.gif(), group="nsfw", category=cat), "invalid", 422)
    _nothing_written(paths)


def test_nsfw分类去首尾空白后写进条目(c, paths):
    item = K.ok_item(K.upload(c, K.gif("n"), group="nsfw", category="  占位类  "))
    assert item["category"] == "占位类" and K.load(paths)["nsfw"][0]["category"] == "占位类"


def test_sfw带了分类_忽略不写进条目(c, paths):
    item = K.ok_item(K.upload(c, K.gif("s"), category="不该出现"))
    assert item["category"] is None and "category" not in K.load(paths)["sfw"][0]


# ── 文件名 ───────────────────────────────────────────────────────────────────

def test_文件名_序号取档目录里最大数字前缀加1_至少3位(c, paths):
    data = K.gif("first")
    assert K.ok_item(K.upload(c, data))["file"] == "000_%s.gif" % K.sha10(data)
    for name in ("007_aaaaaaaaaa.gif", "012_bbbbbbbbbb.png", "abc.gif", ".DS_Store", "99x_c.gif", "0013.gif"):
        K.put_image(paths, "sfw", name, b"x")
    K.put_image(paths, "nsfw", "050_cccccccccc.gif", b"x")   # 别的档不算
    d2 = K.png("second")
    assert K.ok_item(K.upload(c, d2))["file"] == "013_%s.png" % K.sha10(d2)
    K.put_image(paths, "sfw", "1234_dddddddddd.gif", b"x")
    d3 = K.jpg("third")
    assert K.ok_item(K.upload(c, d3))["file"] == "1235_%s.jpg" % K.sha10(d3)
    d4 = K.webp("fourth")
    assert K.ok_item(K.upload(c, d4, group="nsfw", category="占位类"))["file"] == "051_%s.webp" % K.sha10(d4)


def test_文件名_库里本档条目已用这个名字时顺延(c, paths):
    data = K.gif("libname")
    h = K.sha10(data)
    K.write_lib(paths, {"sfw": [{"url": "", "label": "旧", "path": str(paths.dir / "sfw" / ("000_%s.gif" % h)),
                                 "size": 1}], "nsfw": []})   # 文件不在磁盘上
    assert K.ok_item(K.upload(c, data))["file"] == "001_%s.gif" % h


def test_文件名_h取内容sha1前10位_与filename无关(c, paths):
    d = K.png("hash2")
    assert K.ok_item(K.upload(c, d, filename="随便起的名字.gif"))["file"] == "000_%s.png" % K.sha10(d)


# ── 查重 ─────────────────────────────────────────────────────────────────────

def test_查重_同内容同档_409_duplicate_带existing(c, paths):
    data = K.jpg("same")
    K.ok_item(K.upload(c, data, group="nsfw", label="成人那张", category="占位类"))
    before = K.raw(paths)
    b = K.err(K.upload(c, data, group="nsfw", label="再来", category="占位类"), "duplicate", 409,
              message="这张已经在表情库里了（nsfw 档，标签：成人那张）")
    assert b["existing"] == {"group": "nsfw", "label": "成人那张"}
    assert K.raw(paths) == before


def test_查重_同内容跨档_409_duplicate_不落图(c, paths):
    data = K.jpg("same")
    K.ok_item(K.upload(c, data, group="nsfw", label="成人那张", category="占位类"))
    before = K.raw(paths)
    b = K.err(K.upload(c, data, label="再来"), "duplicate", 409)
    assert b["existing"] == {"group": "nsfw", "label": "成人那张"}
    assert K.raw(paths) == before and K.listing(paths.dir / "sfw") == set()


def test_查重_大小相同内容不同不算重_磁盘上有库里没有的也不算(c, paths):
    a, b = K.gif("aa", 40), K.gif("bb", 40)
    assert len(a) == len(b) and a != b
    K.ok_item(K.upload(c, a, label="甲"))
    K.ok_item(K.upload(c, b, label="乙"))
    d = K.gif("cc", 40)
    K.put_image(paths, "sfw", "777_ffffffffff.gif", d)   # 磁盘上有、库里没有
    assert K.ok_item(K.upload(c, d, label="丙"))["label"] == "丙"
    assert [e["label"] for e in K.load(paths)["sfw"]] == ["甲", "乙", "丙"]


# ── 入参错误 ─────────────────────────────────────────────────────────────────

GIFB64 = "R0lGODlhAA=="


@pytest.mark.parametrize("body,message", [
    ({"group": "sfw", "label": "占位", "data": GIFB64}, "缺文件名"),
    ({"group": "sfw", "label": "占位", "filename": 3, "data": GIFB64}, "缺文件名"),
    ({"group": "sfw", "label": "占位", "filename": "a.gif"}, "缺图片内容"),
    ({"group": "sfw", "label": "占位", "filename": "a.gif", "data": ""}, "缺图片内容"),
    ({"group": "sfw", "label": "占位", "filename": "a.gif", "data": "!!!notbase64"}, "图片内容不是合法的 base64"),
    ({"group": "sfw", "label": "占位", "filename": "a.gif", "data": "R0lGODlhAA"}, "图片内容不是合法的 base64"),
    ({"group": "sfw", "label": "占位", "filename": "a.gif", "data": "data:image/gif," + GIFB64}, "图片内容必须是 base64"),
    ({"group": "x", "label": "占位", "filename": "a.gif", "data": GIFB64}, "档只能是 sfw 或 nsfw"),
    ({"group": "nsfw", "label": "占位", "filename": "a.gif", "data": GIFB64}, "成人档要选一个分类"),
], ids=["no_filename", "filename_int", "no_data", "empty_data", "bad_b64", "no_padding", "dataurl_not_b64", "group",
        "nsfw_no_cat"])
def test_上传入参不合规_422_invalid_文案对应_不建目录文件(c, paths, body, message):
    K.err(c.post(K.UPLOAD, json=body), "invalid", 422, message=message)
    assert not paths.json.exists()


@pytest.mark.parametrize("data,ctype", [
    ("[1, 2]", "application/json"), ("{bad", "application/json"), (b"\xff\xfe", "application/json"),
    ('{"group": "sfw"}', "text/plain"),
], ids=["array", "bad_json", "not_utf8", "text_plain_no_origin"])
def test_请求体不是JSON对象_422_invalid(c, paths, data, ctype):
    K.err(c.post(K.UPLOAD, data=data, content_type=ctype), "invalid", 422, message="请求体必须是 JSON 对象")
    assert not paths.json.exists()
