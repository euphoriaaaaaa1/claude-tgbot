# -*- coding: utf-8 -*-
"""带图上传（接口文件 1.4、1.5、1.9 的图片部分；行为清单第 22–25、27 条）：落盘目录、内容一致、不覆盖、
非图片字节、超 8 MiB、请求体超 12 MiB、存不下时的固定提示。"""
import os

import pytest

import pmom_helpers as H


def _j(r):
    return r.status_code, r.get_json(silent=True)


def _under(p, d):
    return os.path.realpath(str(p)).startswith(os.path.realpath(str(d)) + os.sep)


# ── 评论带图（22）────────────────────────────────────────────────────────────

def test_带图评论_图写进用户评论目录_内容与上传一致(c, paths):
    mid = H.user_moment(paths.db, "被评论")
    data = H.png("comment-1")
    b = c.post("/api/comment", json={"moment_id": mid, "text": "", "image": H.data_url(data)}).get_json()
    assert b["image_path"] and os.path.isabs(b["image_path"])
    assert _under(b["image_path"], paths.user_comments)
    with open(b["image_path"], "rb") as fh:
        assert fh.read() == data


def test_带图评论_返回image_url是本站相对地址_再取回内容一致(c, paths):
    mid = H.user_moment(paths.db, "被评论")
    data = H.png("comment-url")
    b = c.post("/api/comment", json={"moment_id": mid, "text": "看图", "image": H.data_url(data)}).get_json()
    name = os.path.basename(b["image_path"])
    assert b["image_url"] == "/image/__user__/comments/" + name
    r = c.get(b["image_url"])
    assert r.status_code == 200 and r.data == data


def test_同一秒两次上传同样的评论图_两个文件都在(c, paths):
    mid = H.user_moment(paths.db, "被评论")
    url = H.data_url(H.png("same"))
    p1 = c.post("/api/comment", json={"moment_id": mid, "text": "", "image": url}).get_json()["image_path"]
    p2 = c.post("/api/comment", json={"moment_id": mid, "text": "", "image": url}).get_json()["image_path"]
    assert p1 != p2 and os.path.isfile(p1) and os.path.isfile(p2)
    assert len(H.files_in(paths.user_comments)) == 2


def test_评论图不覆盖目录里已有的文件(c, paths):
    mid = H.user_moment(paths.db, "被评论")
    old = [H.write_file(paths.user_comments / n, b"old-" + n.encode()) for n in ("a.png", "b.png")]
    c.post("/api/comment", json={"moment_id": mid, "text": "", "image": H.data_url(H.png("new"))})
    assert [p.read_bytes() for p in old] == [b"old-a.png", b"old-b.png"]
    assert len(H.files_in(paths.user_comments)) == 3


# ── 发动态带图（23）──────────────────────────────────────────────────────────

def test_发动态带图_图写进用户自动目录_内容一致(c, paths):
    data = H.png("moment-1")
    b = c.post("/api/moment", json={"text": "", "image": H.data_url(data)}).get_json()
    assert _under(b["image_path"], paths.user_auto)
    with open(b["image_path"], "rb") as fh:
        assert fh.read() == data


def test_同一秒两次发同样的图_两张都在互不覆盖(c, paths):
    url = H.data_url(H.png("same-m"))
    p1 = c.post("/api/moment", json={"text": "一", "image": url}).get_json()["image_path"]
    p2 = c.post("/api/moment", json={"text": "二", "image": url}).get_json()["image_path"]
    assert p1 != p2 and os.path.isfile(p1) and os.path.isfile(p2)
    assert len(H.files_in(paths.user_auto)) == 2


@pytest.mark.parametrize("mime,data", [("image/jpeg", b"\xff\xd8\xff\xe0" + b"j" * 40),
                                       ("image/gif", b"GIF89a" + b"g" * 40),
                                       ("image/webp", b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"w" * 28)],
                         ids=["JPEG", "GIF", "WEBP"])
def test_发动态_其他三种图片头也收下(c, paths, mime, data):
    r = c.post("/api/moment", json={"text": "", "image": H.data_url(data, mime)})
    assert r.status_code == 200 and _under(r.get_json()["image_path"], paths.user_auto)


# ── 非图片、解不开（24）──────────────────────────────────────────────────────

NOT_IMAGES = [
    ("PNG标签下是HTML", H.data_url(b"<html><script>x()</script></html>")),
    ("base64只有四个A", "data:image/png;base64,AAAA"),
    ("没有逗号", "data:image/png;base64"),
    ("base64坏", "data:image/png;base64,a"),
    ("不是图片类型", H.data_url(H.PNG_1PX, "text/html")),
    ("不是data地址", "https://example.com/a.png"),
]


@pytest.mark.parametrize("label,image", NOT_IMAGES, ids=[n[0] for n in NOT_IMAGES])
def test_评论上传非图片_400固定提示_不落盘不入库(c, paths, label, image):
    mid = H.user_moment(paths.db, "被评论")
    assert _j(c.post("/api/comment", json={"moment_id": mid, "text": "x", "image": image})) == (
        400, {"error": H.DECODE_FAILED})
    assert H.files_in(paths.media) == [] and H.comment_rows(paths.db_path, mid) == []


@pytest.mark.parametrize("label,image", NOT_IMAGES, ids=[n[0] for n in NOT_IMAGES])
def test_发动态上传非图片_400固定提示_不落盘不入库(c, paths, label, image):
    assert _j(c.post("/api/moment", json={"text": "x", "image": image})) == (400, {"error": H.DECODE_FAILED})
    assert H.files_in(paths.media) == [] and H.q(paths.db_path, "SELECT * FROM moments") == []


def test_评论图存不下_400固定提示_不带路径(c, paths, monkeypatch):
    blocker = paths.tmp / "media-is-a-file"
    blocker.write_text("x")
    monkeypatch.setenv("MOMENTS_MEDIA_ROOT", str(blocker))
    mid = H.user_moment(paths.db, "被评论")
    r = c.post("/api/comment", json={"moment_id": mid, "text": "x", "image": H.data_url(H.png("nostore"))})
    assert _j(r) == (400, {"error": H.DECODE_FAILED})
    assert str(paths.tmp) not in r.get_data(as_text=True)
    assert H.comment_rows(paths.db_path, mid) == []


def test_发动态图存不下_400固定提示_不入库(c, paths, monkeypatch):
    blocker = paths.tmp / "media-is-a-file"
    blocker.write_text("x")
    monkeypatch.setenv("MOMENTS_MEDIA_ROOT", str(blocker))
    r = c.post("/api/moment", json={"text": "x", "image": H.data_url(H.png("nostore"))})
    assert _j(r) == (400, {"error": H.DECODE_FAILED})
    assert H.q(paths.db_path, "SELECT * FROM moments") == []


def test_解不开的图_日志里没有base64原文(c, paths, caplog, capfd):
    import logging
    mid = H.user_moment(paths.db, "被评论")
    raw = H.data_url(b"<html>PMOM-B64-SECRET-MARK</html>")
    b64 = raw.split(",", 1)[1]
    caplog.set_level(logging.DEBUG)
    c.post("/api/comment", json={"moment_id": mid, "text": "x", "image": raw})
    out, err = capfd.readouterr()
    logged = "\n".join(rec.getMessage() for rec in caplog.records) + out + err
    assert b64 not in logged and b64[:40] not in logged


# ── 超 8 MiB（解码后）────────────────────────────────────────────────────────

def _big_png(n):
    return H.PNG_1PX + b"\0" * (n - len(H.PNG_1PX))


def test_评论图解码后超8MiB_400_image_too_large(c, paths):
    mid = H.user_moment(paths.db, "被评论")
    r = c.post("/api/comment", json={"moment_id": mid, "text": "x", "image": H.data_url(_big_png(H.MAX_IMG + 1))})
    assert _j(r) == (400, {"error": "image too large"})
    assert H.files_in(paths.media) == [] and H.comment_rows(paths.db_path, mid) == []


def test_发动态图解码后超8MiB_400(c, paths):
    r = c.post("/api/moment", json={"text": "x", "image": H.data_url(_big_png(H.MAX_IMG + 1))})
    assert _j(r) == (400, {"error": "image too large (>8MB raw)"})
    assert H.files_in(paths.media) == [] and H.q(paths.db_path, "SELECT * FROM moments") == []


def test_评论图恰好8MiB_收下(c, paths):
    mid = H.user_moment(paths.db, "被评论")
    r = c.post("/api/comment", json={"moment_id": mid, "text": "x", "image": H.data_url(_big_png(H.MAX_IMG))})
    assert r.status_code == 200 and os.path.getsize(r.get_json()["image_path"]) == H.MAX_IMG


# ── 请求体超 12 MiB（25）─────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/api/comment", "/api/moment", "/api/profile/__user__"])
def test_带图接口请求体超12MiB_413_不落盘(c, paths, path):
    mid = H.user_moment(paths.db, "被评论")
    before = H.tables(paths.db_path)
    body = H.sized_json('{"moment_id": %d, "text": "x"' % mid, H.MAX_BODY + 1)
    r = c.post(path, data=body, content_type="application/json")
    assert _j(r) == (413, {"error": "请求体太大"})
    assert H.files_in(paths.media) == [] and H.tables(paths.db_path) == before


def test_评论请求体恰好12MiB_不报413(c, paths):
    mid = H.user_moment(paths.db, "被评论")
    body = H.sized_json('{"moment_id": %d, "text": "x"' % mid, H.MAX_BODY)
    assert c.post("/api/comment", data=body, content_type="application/json").status_code != 413


# ── 资料上传头像（27）────────────────────────────────────────────────────────

def test_头像封面上传真PNG_存进用户资料目录_资料里记本站地址_取回一致(c, paths):
    url = H.data_url(H.PNG_1PX)
    r = c.post("/api/profile/anna", json={"avatar_url": url, "banner_url": url})
    assert r.status_code == 200, r.get_json(silent=True)
    prof = c.get("/api/profile/anna").get_json()
    for k in ("avatar_url", "banner_url"):
        assert prof[k].startswith("/image/__user__/profile/"), prof[k]
        got = c.get(prof[k])
        assert got.status_code == 200 and got.data == H.PNG_1PX
    assert len(H.files_in(paths.user_profile)) >= 1


@pytest.mark.parametrize("fake", [H.data_url(b"<html>not an image</html>"), "data:image/png;base64,AAAA",
                                  H.data_url(H.PNG_1PX + b"\0" * (H.MAX_IMG + 1 - len(H.PNG_1PX)))],
                         ids=["HTML冒充PNG", "四个A", "超8MiB"])
def test_头像上传不合格_400固定提示_资料不写不落盘(c, paths, fake):
    paths.db.set_profile("anna", avatar_url="https://example.com/old.png")
    r = c.post("/api/profile/anna", json={"avatar_url": fake})
    assert _j(r) == (400, {"error": H.DECODE_FAILED})
    assert paths.db.get_profile("anna")["avatar_url"] == "https://example.com/old.png"
    assert H.files_in(paths.user_profile) == []

