# -*- coding: utf-8 -*-
"""图片路由 GET /image/<相对图片根的路径>（接口文件 1.11，D2；行为清单第 33、34 条）。"""
import sys

import pytest

import pmom_helpers as H

no_symlink = pytest.mark.skipif(sys.platform == "win32", reason="Windows 上建符号链接要额外权限")


def test_图片根下已有文件_200_内容一致(c, paths):
    data = H.png("pmom-body")
    H.write_file(paths.media / "a" / "p.png", data)
    r = c.get("/image/a/p.png")
    assert r.status_code == 200 and r.data == data


def test_图片响应带长期不可变缓存头(c, paths):
    H.write_file(paths.media / "a" / "p.png")
    assert c.get("/image/a/p.png").headers.get("Cache-Control") == "private, max-age=31536000, immutable"


def test_图片响应带nosniff(c, paths):
    H.write_file(paths.media / "a" / "p.png")
    assert c.get("/image/a/p.png").headers.get("X-Content-Type-Options") == "nosniff"


@pytest.mark.parametrize("name,data,ctype", [
    ("x.png", b"GIF89a" + b"g" * 40, "image/gif"),
    ("y.png", b"\xff\xd8\xff\xe0" + b"j" * 40, "image/jpeg"),
    ("z.gif", b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"w" * 28, "image/webp"),
])
def test_Content_Type按文件头定_与扩展名无关(c, paths, name, data, ctype):
    H.write_file(paths.media / "a" / name, data)
    r = c.get("/image/a/" + name)
    assert r.status_code == 200 and r.data == data
    assert r.headers.get("Content-Type", "").split(";")[0].strip() == ctype


@pytest.mark.parametrize("name,data", [
    ("page.png", b"<html><script>PMOM-MARK()</script></html>"),
    ("vec.png", b"<svg xmlns='http://www.w3.org/2000/svg'><script>PMOM-MARK()</script></svg>"),
    ("note.png", b"PMOM-MARK plain text, not an image"),
    ("empty.png", b""),
    ("cut.png", b"\x89PNG" + b"PMOM-MARK"),
])
def test_图片根下不是图片的文件_404且正文不含文件内容(c, paths, name, data):
    H.write_file(paths.media / "a" / name, data)
    r = c.get("/image/a/" + name)
    assert r.status_code == 404 and b"PMOM-MARK" not in r.data


def test_点点越界到根外_404(c, paths):
    H.write_file(paths.tmp / "secret.png", b"no")
    H.write_file(paths.media / "a" / "p.png")
    r = c.get("/image/a/../../secret.png")
    assert r.status_code == 404 and r.data != b"no"


def test_编码过的点点越界_404(c, paths):
    H.write_file(paths.tmp / "secret.png", b"no")
    r = c.get("/image/a/%2e%2e/%2e%2e/secret.png")
    assert r.status_code == 404 and r.data != b"no"


def test_绝对路径_404(c, paths):
    outside = H.write_file(paths.tmp / "abs-secret.png", b"no")
    r = c.get("/image/" + str(outside), follow_redirects=True)
    assert r.status_code == 404 and r.data != b"no"


def test_绝对路径指向图片根里的文件_也404(c, paths):
    inside = H.write_file(paths.media / "a" / "abs-in.png", b"inside")
    r = c.get("/image/" + str(inside), follow_redirects=True)
    assert r.status_code == 404 and r.data != b"inside"


@no_symlink
def test_根内符号链接指到根外_404(c, paths):
    outside = H.write_file(paths.tmp / "outside" / "t.png", b"no")
    (paths.media / "link.png").symlink_to(outside)
    r = c.get("/image/link.png")
    assert r.status_code == 404 and r.data != b"no"


@no_symlink
def test_根内目录链接指到根外_其下文件404(c, paths):
    H.write_file(paths.tmp / "outside" / "t.png", b"no")
    (paths.media / "dirlink").symlink_to(paths.tmp / "outside", target_is_directory=True)
    assert c.get("/image/dirlink/t.png").status_code == 404


def test_目录_404(c, paths):
    (paths.media / "adir").mkdir()
    assert c.get("/image/adir").status_code == 404


def test_不存在的文件_404(c, paths):
    assert c.get("/image/a/none.png").status_code == 404


def test_图片根本身_404(c, paths):
    assert c.get("/image/").status_code == 404
    assert c.get("/image/.").status_code == 404
