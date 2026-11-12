# -*- coding: utf-8 -*-
"""图片路由（接口文件 §1.2；行为清单 27、14 的图片部分）：四种类型的 Content-Type 与两个响应头、
按文件头定类型、其余情况一律 404 且正文不含图片字节。"""
import sys

import pytest

import pstk_helpers as K

IMG = "/console/stickers/img/"


@pytest.mark.parametrize("ext,mt", [("gif", "image/gif"), ("jpg", "image/jpeg"), ("png", "image/png"),
                                    ("webp", "image/webp")])
def test_库内合法图_200_原样字节_ContentType按类型(c, paths, ext, mt):
    data = K.MAKERS[ext]("img-" + ext)
    item = K.ok_item(K.upload(c, data, filename="x." + ext))
    r = c.get(item["img"])
    assert r.status_code == 200 and r.mimetype == mt and r.data == data


def test_库内合法图_带缓存与nosniff两个响应头(c, paths):
    item = K.ok_item(K.upload(c, K.gif("hdr")))
    r = c.get(item["img"])
    assert r.status_code == 200
    assert r.headers["Cache-Control"] == "private, max-age=3600"
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_ContentType按文件头不按扩展名_png扩展名里是GIF(c, paths):
    p = K.put_image(paths, "sfw", "000_aaaaaaaaaa.png", K.gif("really-gif"))
    K.write_lib(paths, {"sfw": [{"url": "", "label": "甲", "path": p, "size": 1}], "nsfw": []})
    r = c.get(IMG + "sfw/000_aaaaaaaaaa.png")
    assert r.status_code == 200 and r.mimetype == "image/gif"


@pytest.fixture
def img_lib(paths, tmp_path):
    good = K.put_image(paths, "sfw", "000_aaaaaaaaaa.gif", K.gif("good"))
    K.put_image(paths, "sfw", "notinlib.gif", K.gif("nl"))
    hidden = K.put_image(paths, "sfw", ".hidden.gif", K.gif("hidden"))
    outside = tmp_path / "outside.gif"
    outside.write_bytes(K.gif("outside"))
    text = K.put_image(paths, "sfw", "002_text.gif", b"not an image at all")
    K.put_image(paths, "nsfw", "000_aaaaaaaaaa.gif", K.gif("nsfw-not-in-lib"))
    (paths.removed / "sfw").mkdir(parents=True)
    K.put_image(paths, ".removed", "000_aaaaaaaaaa.gif", K.gif("removed"))

    def e(p, label):
        return {"url": "", "label": label, "path": str(p), "size": 1}
    entries = [e(good, "好"), e(hidden, "藏"), e(outside, "外"), e(text, "字"),
               e(paths.dir / "sfw" / "003_missing.gif", "缺"), e(paths.shared / "stickers.json", "库")]
    if sys.platform != "win32":
        (paths.dir / "sfw" / "001_link.gif").symlink_to(outside)
        entries.append(e(paths.dir / "sfw" / "001_link.gif", "链"))
    K.write_lib(paths, {"sfw": entries, "nsfw": []})


def test_样本库里的合法图能取到(c, paths, img_lib):
    assert c.get(IMG + "sfw/000_aaaaaaaaaa.gif").status_code == 200


NO_SYMLINK = pytest.mark.skipif(sys.platform == "win32", reason="Windows 上不测符号链接")


@pytest.mark.parametrize("url", [
    pytest.param(IMG + "sfw/..", id="dotdot"), pytest.param(IMG + "sfw/%2e%2e", id="enc_dotdot"),
    pytest.param(IMG + "sfw/..%2fsecret.gif", id="enc_slash"), pytest.param(IMG + "sfw/%2Fetc%2Fhosts", id="abs_enc"),
    pytest.param(IMG + "sfw//etc/hosts", id="abs_double"), pytest.param(IMG + "/etc/hosts", id="abs_group"),
    pytest.param(IMG + "sfw/.hidden.gif", id="dot"), pytest.param(IMG + "sfw/%2ehidden.gif", id="enc_dot"),
    pytest.param(IMG + "other/000_aaaaaaaaaa.gif", id="unknown_group"),
    pytest.param(IMG + ".removed/000_aaaaaaaaaa.gif", id="removed_group"),
    pytest.param(IMG + "sfw/notinlib.gif", id="not_in_lib"), pytest.param(IMG + "nsfw/000_aaaaaaaaaa.gif", id="wrong_group"),
    pytest.param(IMG + "sfw/stickers.json", id="json"), pytest.param(IMG + "sfw/outside.gif", id="outside"),
    pytest.param(IMG + "sfw/001_link.gif", id="symlink_out", marks=NO_SYMLINK),
    pytest.param(IMG + "sfw/002_text.gif", id="not_image"), pytest.param(IMG + "sfw/003_missing.gif", id="missing"),
    pytest.param(IMG + "sfw/" + "a" * 129, id="too_long"),
])
def test_非法情况一律404_正文不含图片字节(c, paths, img_lib, url):
    r = c.get(url)
    assert r.status_code == 404, (url, r.status_code)
    assert b"GIF89a" not in r.data


def test_库文件坏了_图片路由404(c, paths):
    K.put_image(paths, "sfw", "000_aaaaaaaaaa.gif", K.gif("x"))
    paths.json.write_bytes(b'{"sfw": [}')
    r = c.get(IMG + "sfw/000_aaaaaaaaaa.gif")
    assert r.status_code == 404 and b"GIF89a" not in r.data
