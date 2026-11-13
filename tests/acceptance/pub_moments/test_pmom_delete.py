# -*- coding: utf-8 -*-
"""删除自己发的动态（接口文件 1.7；行为清单第 30–32 条）：连带删除、配图规则、错误契约、并发、日志。
断言只看接口回应、朋友圈库的三张表与图片目录。"""
import logging
import os
import re
import sys
import threading

import pytest

import pmom_helpers as H

NOT_FOUND = {"error": "这条动态不存在或已经删了"}
NOT_MINE = {"error": "只能删自己发的动态"}
no_symlink = pytest.mark.skipif(sys.platform == "win32", reason="Windows 上建符号链接要额外权限")


def _j(r):
    return r.status_code, r.get_json(silent=True)


def _delete(c, mid):
    return c.delete("/api/moment/%s" % mid, json={})


def _post(c, text, image=False):
    body = {"text": text}
    if image:
        body["image"] = H.data_url(H.png(text))
    r = c.post("/api/moment", json=body)
    assert r.status_code == 200, "造数据：用户发动态失败 %s" % r.status_code
    b = r.get_json()
    return b["id"], b["image_path"]


def _others(rows, mid):
    return [r for r in rows if r["moment_id"] != mid]


def _state(paths):
    return H.tables(paths.db_path), H.media_state(paths.media)


# ── 删成功（30）─────────────────────────────────────────────────────────────

def test_删自己的动态_200_之后详情404_列表里没有它(c, paths):
    keep = H.user_moment(paths.db, "留着的")
    mid = H.user_moment(paths.db, "要删的")
    assert _j(_delete(c, mid)) == (200, {"ok": True, "id": mid})
    assert c.get("/api/moment/%d" % mid).status_code == 404
    ids = [m["id"] for m in c.get("/api/moments").get_json()]
    assert mid not in ids and keep in ids


def test_连带删全部评论含回复的回复与全部点赞_别的动态逐行不变(c, paths):
    a = H.user_moment(paths.db, "要删的")
    c1 = H.add_comment(paths.db_path, a, H.ME, "我的评论")
    c2 = H.add_comment(paths.db_path, a, "anna", "回我", parent=c1)
    H.add_comment(paths.db_path, a, "bella", "回复的回复", parent=c2)
    H.add_comment(paths.db_path, a, "coco", "直接评论")
    for who in (H.ME, "anna", "bella"):
        H.add_like(paths.db_path, a, who)
    other = H.user_moment(paths.db, "别的")
    o1 = H.add_comment(paths.db_path, other, H.ME, "别处的评论")
    H.add_comment(paths.db_path, other, "anna", "别处的回复", parent=o1)
    H.add_like(paths.db_path, other, "coco")
    b = H.bot_moment(paths.db, "anna", "bot 的")
    H.add_comment(paths.db_path, b, H.ME, "给 bot 的")
    H.add_like(paths.db_path, b, H.ME)
    before = H.tables(paths.db_path)
    assert _delete(c, a).status_code == 200
    assert H.comment_rows(paths.db_path, a) == [] and H.like_rows(paths.db_path, a) == []
    after = H.tables(paths.db_path)
    assert after["moment_comments"] == _others(before["moment_comments"], a)
    assert after["moment_likes"] == _others(before["moment_likes"], a)


# ── 删不了（30）─────────────────────────────────────────────────────────────

def test_删bot的动态_403_库与文件都不变(c, paths):
    b = H.bot_moment(paths.db, "anna", "bot 的", image_path=H.write_file(paths.media / "anna" / "b.png"))
    cid = H.add_comment(paths.db_path, b, H.ME, "我评论")
    H.add_comment(paths.db_path, b, "anna", "回复", parent=cid, image=H.write_file(paths.media / "anna" / "c.png"))
    H.add_like(paths.db_path, b, H.ME)
    before = _state(paths)
    assert _j(_delete(c, b)) == (403, NOT_MINE)
    assert _state(paths) == before


def test_删不存在的动态_404_库与文件都不变(c, paths):
    _post(c, "留着的", image=True)
    before = _state(paths)
    assert _j(_delete(c, 987654)) == (404, NOT_FOUND)
    assert _state(paths) == before


def test_删已经删过的动态_404_库与文件都不变(c, paths):
    mid, _ = _post(c, "删两次", image=True)
    assert _delete(c, mid).status_code == 200
    once = _state(paths)
    assert _j(_delete(c, mid)) == (404, NOT_FOUND)
    assert _state(paths) == once


def test_几乎同时删两次_一次200一次404_结果与删一次相同(c, paths, new_client):
    mid, img = _post(c, "并发", image=True)
    cid = H.add_comment(paths.db_path, mid, H.ME, "并发评论")
    H.add_comment(paths.db_path, mid, "anna", "并发回复", parent=cid)
    H.add_like(paths.db_path, mid, H.ME)
    other = H.user_moment(paths.db, "旁边")
    H.add_comment(paths.db_path, other, H.ME, "旁边评论")
    before = H.tables(paths.db_path)
    clients = [new_client(), new_client()]
    gate = threading.Barrier(2)
    out = [None, None]

    def go(i):
        gate.wait(10)
        out[i] = _j(_delete(clients[i], mid))

    threads = [threading.Thread(target=go, args=(i,), daemon=True) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert None not in out, "两个删除请求应在 30 秒内都回来"
    assert sorted(out, key=lambda o: o[0]) == [(200, {"ok": True, "id": mid}), (404, NOT_FOUND)]
    assert H.comment_rows(paths.db_path, mid) == [] and H.like_rows(paths.db_path, mid) == []
    after = H.tables(paths.db_path)
    assert after["moment_comments"] == _others(before["moment_comments"], mid)
    assert not os.path.exists(img)


def test_删后在旧页面上点赞与评论_都404_库里不新增(c, paths):
    mid = H.user_moment(paths.db, "删后旧页面")
    assert _delete(c, mid).status_code == 200
    before = H.tables(paths.db_path)
    assert c.post("/api/like", json={"moment_id": mid}).status_code == 404
    assert c.post("/api/comment", json={"moment_id": mid, "text": "迟到的评论"}).status_code == 404
    assert H.tables(paths.db_path) == before


# ── 配图规则（31）───────────────────────────────────────────────────────────

def test_动态自带的上传图_删(c, paths):
    mid, own = _post(c, "带图", image=True)
    assert os.path.isfile(own)
    assert _delete(c, mid).status_code == 200
    assert not os.path.exists(own)


def test_用户评论目录里再无引用的图_删(c, paths):
    mid = H.user_moment(paths.db, "带图评论")
    mine = H.write_file(paths.user_comments / "mine.png")
    H.add_comment(paths.db_path, mid, H.ME, "我的带图评论", image=mine)
    assert _delete(c, mid).status_code == 200
    assert not mine.exists()


def test_metadata多图里的上传图_删(c, paths):
    p1, p2 = H.write_file(paths.user_auto / "m1.png"), H.write_file(paths.user_auto / "m2.png")
    mid = H.user_moment(paths.db, "多图", metadata={"author_display": H.ME, "image_paths": [str(p1), str(p2)]})
    assert _delete(c, mid).status_code == 200
    assert not p1.exists() and not p2.exists()


def test_bot评论引用的图在上传两目录外_不删(c, paths):
    mid = H.user_moment(paths.db, "bot 带图回复")
    bot_img = H.write_file(paths.media / "anna" / "gen.png")
    H.add_comment(paths.db_path, mid, "anna", "bot 带图回复", image=bot_img)
    assert _delete(c, mid).status_code == 200
    assert H.comment_rows(paths.db_path, mid) == [] and bot_img.read_bytes() == H.PNG_1PX


def test_资料目录里的头像封面_不删(c, paths):
    mid = H.user_moment(paths.db, "引用头像")
    cover = H.write_file(paths.user_profile / "cover.png")
    H.add_comment(paths.db_path, mid, H.ME, "引用头像的评论", image=cover)
    assert _delete(c, mid).status_code == 200
    assert cover.read_bytes() == H.PNG_1PX


def test_也被别的动态metadata引用的图_不删(c, paths):
    shared = H.write_file(paths.user_auto / "shared.png")
    mid = H.user_moment(paths.db, "要删的", image_path=shared)
    H.bot_moment(paths.db, "anna", "引用同一张", metadata={"image_paths": [str(shared)]})
    assert _delete(c, mid).status_code == 200
    assert shared.read_bytes() == H.PNG_1PX


def test_也被别的评论引用的图_不删(c, paths):
    shared = H.write_file(paths.user_comments / "shared-c.png")
    mid = H.user_moment(paths.db, "要删的")
    H.add_comment(paths.db_path, mid, H.ME, "带图", image=shared)
    other = H.bot_moment(paths.db, "anna", "别的")
    H.add_comment(paths.db_path, other, "bella", "引用同一张", image=shared)
    assert _delete(c, mid).status_code == 200
    assert shared.read_bytes() == H.PNG_1PX


def test_换写法引用同一文件也算引用_不删(c, paths):
    shared = H.write_file(paths.user_auto / "alias.png")
    mid = H.user_moment(paths.db, "要删的", image_path=shared)
    alias = os.path.join(str(paths.user_auto), "..", "auto", "alias.png")
    H.bot_moment(paths.db, "anna", "换写法引用", image_path=alias)
    assert _delete(c, mid).status_code == 200
    assert shared.read_bytes() == H.PNG_1PX


def test_图片根外的图_不删(c, paths):
    out = H.write_file(paths.tmp / "outside" / "out.png")
    mid = H.user_moment(paths.db, "根外", image_path=out)
    assert _delete(c, mid).status_code == 200
    assert out.read_bytes() == H.PNG_1PX


def test_经点点指到根外的图_不删(c, paths):
    out = H.write_file(paths.tmp / "outside" / "trav.png")
    mid = H.user_moment(paths.db, "点点")
    trav = os.path.join(str(paths.user_comments), "..", "..", "..", "outside", "trav.png")
    H.add_comment(paths.db_path, mid, H.ME, "点点", image=trav)
    assert _delete(c, mid).status_code == 200
    assert out.read_bytes() == H.PNG_1PX


@no_symlink
def test_上传目录里指到根外的符号链接_链接目标不删(c, paths):
    target = H.write_file(paths.tmp / "outside" / "target.png")
    paths.user_comments.mkdir(parents=True, exist_ok=True)
    link = paths.user_comments / "link.png"
    link.symlink_to(target)
    mid = H.user_moment(paths.db, "链接")
    H.add_comment(paths.db_path, mid, H.ME, "链接图", image=link)
    assert _delete(c, mid).status_code == 200
    assert target.read_bytes() == H.PNG_1PX


@no_symlink
def test_经符号链接指向同一文件的引用也算引用_不删(c, paths):
    real = H.write_file(paths.user_auto / "real.png")
    other_dir = paths.media / "anna"
    other_dir.mkdir(parents=True, exist_ok=True)
    (other_dir / "ln.png").symlink_to(real)
    mid = H.user_moment(paths.db, "要删的", image_path=real)
    H.bot_moment(paths.db, "anna", "经链接引用", image_path=other_dir / "ln.png")
    assert _delete(c, mid).status_code == 200
    assert real.read_bytes() == H.PNG_1PX


def test_引用的文件本来就不在_也算成功(c, paths):
    mid = H.user_moment(paths.db, "缺文件", image_path=paths.user_auto / "gone.png")
    assert _j(_delete(c, mid)) == (200, {"ok": True, "id": mid})


# ── 日志（32）───────────────────────────────────────────────────────────────

def test_删除成功只记一行日志_含编号与四个计数_不含正文(c, paths, capfd, caplog):
    H.x(paths.db_path, "INSERT INTO moments (id, bot_id, ts, text, metadata_json, moment_kind, visibility) "
                       "VALUES (73100, 'anna', 1, '占位', '{}', 'daily', 'public')")
    mid, _ = _post(c, "日志动态正文", image=True)                      # 动态自带 1 张，会删
    c1 = H.add_comment(paths.db_path, mid, H.ME, "日志评论正文一", image=H.write_file(paths.user_comments / "c1.png"))
    c2 = H.add_comment(paths.db_path, mid, "anna", "日志评论正文二", parent=c1,
                       image=H.write_file(paths.media / "l" / "c2.png"))
    H.add_comment(paths.db_path, mid, "bella", "日志评论正文三", parent=c2)
    H.add_like(paths.db_path, mid, H.ME)
    H.add_like(paths.db_path, mid, "anna")                            # 评论 3、点赞 2、删掉文件 2
    caplog.set_level(logging.DEBUG)
    capfd.readouterr()
    caplog.clear()
    r = _delete(c, mid)
    out, err = capfd.readouterr()
    assert r.status_code == 200
    printed = [ln for ln in (out + "\n" + err).splitlines() if ln.strip()]
    logged = [rec.getMessage() for rec in caplog.records]
    lines = printed + [m for m in logged if not any(m in p for p in printed)]
    assert len(lines) == 1, "一次成功删除应恰好记一行日志，实际 %d 行：%r" % (len(lines), lines)
    line = lines[0]
    for label, n in (("动态编号", mid), ("评论数", 3), ("点赞数", 2), ("删掉的文件数", 2)):
        assert re.search(r"(?<!\d)%d(?!\d)" % n, line), "日志里应有%s %d：%r" % (label, n, line)
    assert len(re.findall(r"(?<!\d)2(?!\d)", line)) >= 2, "点赞数与文件数都是 2：%r" % line
    assert re.search(r"(?<!\d)0(?!\d)", line), "删不掉的文件数 0 应出现：%r" % line
    assert "日志动态正文" not in line and "日志评论正文" not in line
