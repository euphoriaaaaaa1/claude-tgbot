# -*- coding: utf-8 -*-
"""接口（接口文件 1.2–1.6、1.8、1.10；行为清单第 18–21、29 条）：参数与错误契约、返回形状、点赞、评论、发动态、删评论。
上传图片、请求体大小在 test_pmom_upload.py；资料写入在 test_pmom_profile.py；删动态在 test_pmom_delete.py。"""
import json
import re

import pytest

import pmom_helpers as H


def _j(r):
    return r.status_code, r.get_json(silent=True)


# ── GET /api/moments（18）────────────────────────────────────────────────────

@pytest.mark.parametrize("qs,param", [("since=abc", "since"), ("limit=x", "limit"), ("since=1&limit=1.5", "limit"),
                                      ("since=", "since"), ("since=abc&limit=x", "since")])
def test_动态列表_参数不是整数_400并点名(c, qs, param):
    assert _j(c.get("/api/moments?" + qs)) == (400, {"error": "参数 %s 必须是整数" % param, "param": param})


@pytest.mark.parametrize("limit", ["0", "201", "-1"])
def test_动态列表_limit不在1到200_400(c, limit):
    assert _j(c.get("/api/moments?limit=" + limit)) == (
        400, {"error": "参数 limit 必须是 1 到 200 的整数", "param": "limit"})


@pytest.mark.parametrize("limit", ["1", "200"])
def test_动态列表_limit在边界上照常(c, paths, limit):
    H.user_moment(paths.db, "一条")
    H.user_moment(paths.db, "两条")
    r = c.get("/api/moments?since=0&limit=" + limit)
    assert r.status_code == 200 and len(r.get_json()) == min(int(limit), 2)


def test_动态列表_字段齐全(c, paths):
    H.user_moment(paths.db, "字段")
    (item,) = c.get("/api/moments").get_json()
    assert H.MOMENT_KEYS <= set(item), sorted(H.MOMENT_KEYS - set(item))
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", item["time_str"]), item["time_str"]
    assert re.fullmatch(r"\d{2}:\d{2}", item["time_short"]), item["time_short"]
    assert re.fullmatch(r"\d+(秒|分钟|小时|天)前", item["ago"]), item["ago"]
    assert (item["bot_id"], item["text"], item["kind"], item["visibility"]) == ("__user__", "字段", "user_post", "public")


def test_动态列表_按ts再按id倒序(c, paths):
    a = H.user_moment(paths.db, "a", ts=100)
    b = H.user_moment(paths.db, "b", ts=200)
    d = H.user_moment(paths.db, "d", ts=200)
    assert [m["id"] for m in c.get("/api/moments").get_json()] == [d, b, a]


def test_动态列表_bot参数只看这个人(c, paths):
    H.user_moment(paths.db, "我的")
    mine = H.bot_moment(paths.db, "anna", "安娜的")
    H.bot_moment(paths.db, "bella", "贝拉的")
    assert [m["id"] for m in c.get("/api/moments?bot=anna").get_json()] == [mine]


def test_动态列表_since只给ts不小于它的(c, paths):
    H.user_moment(paths.db, "旧", ts=100)
    keep = H.user_moment(paths.db, "新", ts=300)
    assert [m["id"] for m in c.get("/api/moments?since=300").get_json()] == [keep]


def test_动态对象_image_paths优先取metadata(c, paths):
    p1, p2 = (str(H.write_file(paths.media / "anna" / n)) for n in ("a.png", "b.png"))
    H.bot_moment(paths.db, "anna", "多图", image_path=p1, metadata={"image_paths": [p1, p2]})
    (item,) = c.get("/api/moments").get_json()
    assert item["image_paths"] == [p1, p2]
    assert item["image_urls"] == ["/image/anna/a.png", "/image/anna/b.png"]


def test_动态对象_没有metadata图时image_paths是单图或空(c, paths):
    p1 = str(H.write_file(paths.media / "anna" / "one.png"))
    single = H.bot_moment(paths.db, "anna", "单图", image_path=p1, ts=200)
    none = H.bot_moment(paths.db, "anna", "无图", ts=100)
    got = {m["id"]: (m["image_paths"], m["image_urls"]) for m in c.get("/api/moments").get_json()}
    assert got[single] == ([p1], ["/image/anna/one.png"])
    assert got[none] == ([], [])


def test_动态对象_图片根外的图对应image_urls为null(c, paths):
    inside = str(H.write_file(paths.media / "anna" / "in.png"))
    outside = str(H.write_file(paths.tmp / "outside" / "out.png"))
    H.bot_moment(paths.db, "anna", "混合", metadata={"image_paths": [inside, outside]})
    (item,) = c.get("/api/moments").get_json()
    assert item["image_urls"] == ["/image/anna/in.png", None]


def test_动态对象_visibility为空当public_坏metadata当空对象(c, paths):
    H.x(paths.db_path, "INSERT INTO moments (bot_id, ts, text, image_path, metadata_json, moment_kind, visibility) "
                       "VALUES ('anna', 1, '坏', NULL, '{bad', 'daily', '')")
    (item,) = c.get("/api/moments").get_json()
    assert (item["visibility"], item["metadata"], item["kind"]) == ("public", {}, "daily")


# ── POST /api/like（19）──────────────────────────────────────────────────────

def test_点赞两次_先赞上再取消_likers跟着变(c, paths):
    mid = H.bot_moment(paths.db, "anna")
    assert _j(c.post("/api/like", json={"moment_id": mid})) == (200, {"liked": True, "likers": ["我"]})
    assert _j(c.post("/api/like", json={"moment_id": mid})) == (200, {"liked": False, "likers": []})
    assert H.like_rows(paths.db_path, mid) == []


def test_点赞人是全局参数里的显示名(c, paths, set_global):
    set_global(user_display_name="测试者")
    mid = H.bot_moment(paths.db, "anna")
    assert _j(c.post("/api/like", json={"moment_id": mid})) == (200, {"liked": True, "likers": ["测试者"]})


@pytest.mark.parametrize("body", [{}, {"moment_id": "abc"}, {"moment_id": None}])
def test_点赞_moment_id缺或不是整数_400(c, body):
    assert _j(c.post("/api/like", json=body)) == (400, {"error": "moment_id required"})


def test_点赞_动态不存在_404_不写库(c, paths):
    assert _j(c.post("/api/like", json={"moment_id": 99999})) == (404, {"error": "moment not found"})
    assert H.q(paths.db_path, "SELECT * FROM moment_likes") == []


# ── POST /api/comment 错误（20）──────────────────────────────────────────────

BAD_COMMENTS = [
    ("缺moment_id", lambda mid: {"text": "x"}, 400, "moment_id required"),
    ("moment_id不是整数", lambda mid: {"moment_id": "abc", "text": "x"}, 400, "moment_id required"),
    ("缺moment_id且正文空_先报moment_id", lambda mid: {"text": ""}, 400, "moment_id required"),
    ("正文与图都空", lambda mid: {"moment_id": mid, "text": ""}, 400, "empty"),
    ("正文与图都空且动态不存在_先报empty", lambda mid: {"moment_id": 99999}, 400, "empty"),
    ("正文501字", lambda mid: {"moment_id": mid, "text": "字" * 501}, 400, "text too long (>500)"),
    ("正文超长且parent坏_先报超长", lambda mid: {"moment_id": mid, "text": "字" * 501, "parent_id": "x"}, 400,
     "text too long (>500)"),
    ("parent_id不是整数", lambda mid: {"moment_id": mid, "text": "x", "parent_id": "x"}, 400, "invalid parent_id"),
    ("parent坏且动态不存在_先报parent", lambda mid: {"moment_id": 99999, "text": "x", "parent_id": "x"}, 400,
     "invalid parent_id"),
    ("动态不存在", lambda mid: {"moment_id": 99999, "text": "x"}, 404, "moment not found"),
]


@pytest.mark.parametrize("label,body,status,error", BAD_COMMENTS, ids=[b[0] for b in BAD_COMMENTS])
def test_评论错误按顺序与文案返回_什么都不写(c, paths, backends, label, body, status, error):
    backends.add("claude", bots=[H.bot("anna")])
    mid = H.user_moment(paths.db, "被评论")
    before = (H.tables(paths.db_path), H.media_state(paths.media))
    assert _j(c.post("/api/comment", json=body(mid))) == (status, {"error": error})
    assert (H.tables(paths.db_path), H.media_state(paths.media)) == before
    assert backends.calls == []


def test_评论正文恰好500字_收下(c, paths):
    mid = H.user_moment(paths.db, "被评论")
    r = c.post("/api/comment", json={"moment_id": mid, "text": "字" * 500})
    assert r.status_code == 200 and r.get_json()["text"] == "字" * 500


@pytest.mark.parametrize("body", [[1, 2], "x", 3, None])
def test_评论请求体不是JSON对象_422(c, body):
    r = c.post("/api/comment", data=H.jdump(body), content_type="application/json")
    assert _j(r) == (422, {"error": "请求体必须是 JSON 对象"})


def test_评论请求体不是合法JSON_422(c):
    r = c.post("/api/comment", data="{not json", content_type="application/json")
    assert _j(r) == (422, {"error": "请求体必须是 JSON 对象"})


# ── POST /api/comment 成功（21）──────────────────────────────────────────────

def test_评论返回体恰好八个键_署名与去空白(c, paths):
    mid = H.user_moment(paths.db, "被评论")
    r = c.post("/api/comment", json={"moment_id": mid, "text": "  风好大  "})
    assert r.status_code == 200
    b = r.get_json()
    assert set(b) == H.COMMENT_KEYS, sorted(b)
    assert (b["from"], b["text"], b["parent_id"], b["image_path"], b["image_url"]) == ("我", "风好大", None, None, None)
    (row,) = H.comment_rows(paths.db_path, mid)
    assert (row["id"], row["from_user"], row["text"]) == (b["id"], "我", "风好大")


def test_评论署名是全局参数里的显示名(c, paths, set_global):
    set_global(user_display_name="测试者")
    mid = H.user_moment(paths.db, "被评论")
    assert c.post("/api/comment", json={"moment_id": mid, "text": "x"}).get_json()["from"] == "测试者"


def test_回复评论_返回体带parent_id(c, paths):
    mid = H.user_moment(paths.db, "被评论")
    first = c.post("/api/comment", json={"moment_id": mid, "text": "一"}).get_json()["id"]
    b = c.post("/api/comment", json={"moment_id": mid, "text": "二", "parent_id": first}).get_json()
    assert b["parent_id"] == first


# ── POST /api/moment（1.5）───────────────────────────────────────────────────

def test_发动态_返回四个键_入库为用户动态(c, paths):
    r = c.post("/api/moment", json={"text": "今天好热", "visibility": "private"})
    assert r.status_code == 200
    b = r.get_json()
    assert set(b) == {"id", "image_path", "visibility", "text"}
    assert (b["text"], b["visibility"], b["image_path"]) == ("今天好热", "private", None)
    (row,) = H.q(paths.db_path, "SELECT * FROM moments WHERE id=?", (b["id"],))
    assert (row["bot_id"], row["moment_kind"], row["visibility"]) == ("__user__", "user_post", "private")
    assert json.loads(row["metadata_json"]) == {"author_display": "我"}


@pytest.mark.parametrize("vis", ["friends", "", None, 3])
def test_发动态_visibility不认识的当public(c, vis):
    assert c.post("/api/moment", json={"text": "x", "visibility": vis}).get_json()["visibility"] == "public"


def test_发动态_正文与图都空_400_不入库(c, paths):
    assert _j(c.post("/api/moment", json={"text": ""})) == (400, {"error": "empty"})
    assert H.q(paths.db_path, "SELECT * FROM moments") == []


def test_发动态_正文2001字_400_不入库(c, paths):
    assert _j(c.post("/api/moment", json={"text": "字" * 2001})) == (400, {"error": "text too long (>2000)"})
    assert H.q(paths.db_path, "SELECT * FROM moments") == []


def test_发动态_正文恰好2000字_收下(c):
    assert c.post("/api/moment", json={"text": "字" * 2000}).status_code == 200


@pytest.mark.parametrize("body", [[1, 2], "x", 3])
def test_发动态请求体不是JSON对象_422(c, paths, body):
    r = c.post("/api/moment", data=H.jdump(body), content_type="application/json")
    assert _j(r) == (422, {"error": "请求体必须是 JSON 对象"})
    assert H.q(paths.db_path, "SELECT * FROM moments") == []


# ── GET /api/moment/<id>（1.10）与 GET /api/profile（1.8）────────────────────

def test_动态详情_带点赞人与评论行(c, paths):
    mid = H.bot_moment(paths.db, "anna", "原帖")
    H.add_like(paths.db_path, mid, "bella")
    cid = H.add_comment(paths.db_path, mid, "anna", "bot 的回复")
    d = c.get("/api/moment/%d" % mid).get_json()
    assert (d["id"], d["text"], d["likers"]) == (mid, "原帖", ["bella"])
    assert H.MOMENT_KEYS <= set(d)
    (cm,) = d["comments"]
    assert {"id", "moment_id", "from_user", "text", "ts", "parent_id", "pending", "image_path"} <= set(cm)
    assert (cm["id"], cm["moment_id"], cm["from_user"], cm["text"]) == (cid, mid, "anna", "bot 的回复")


def test_动态详情_不存在_404(c):
    assert _j(c.get("/api/moment/99999")) == (404, {"error": "not found"})


def test_资料_没有记录时前三项与显示名为null_时间为0(c):
    assert _j(c.get("/api/profile/someone")) == (200, {
        "avatar_url": None, "banner_url": None, "signature": None, "sig_updated_at": 0, "sig_asked_at": 0,
        "display_name": None, "updated_at": 0})


def test_资料_用户主页键可读(c):
    assert c.get("/api/profile/__user__").status_code == 200


# ── DELETE /api/comment/<id>（29）────────────────────────────────────────────

def test_删自己的评论_200_库里没了(c, paths):
    mid = H.bot_moment(paths.db, "anna")
    cid = c.post("/api/comment", json={"moment_id": mid, "text": "我的"}).get_json()["id"]
    assert _j(c.delete("/api/comment/%d" % cid, json={})) == (200, {"ok": True, "id": cid})
    assert H.comment_rows(paths.db_path, mid) == []


def test_删bot的评论_404_库不变(c, paths):
    mid = H.bot_moment(paths.db, "anna")
    cid = H.add_comment(paths.db_path, mid, "anna", "bot 的回复")
    before = H.tables(paths.db_path)
    assert _j(c.delete("/api/comment/%d" % cid, json={})) == (404, {"error": "comment not found or not yours"})
    assert H.tables(paths.db_path) == before


def test_删不存在的评论_404(c):
    assert _j(c.delete("/api/comment/99999", json={})) == (404, {"error": "comment not found or not yours"})


def test_删评论不删它引用的图片文件(c, paths):
    mid = H.bot_moment(paths.db, "anna")
    img = H.write_file(paths.user_comments / "keep.png")
    cid = H.add_comment(paths.db_path, mid, H.ME, "带图", image=img)
    assert c.delete("/api/comment/%d" % cid, json={}).status_code == 200
    assert img.is_file()


def test_删评论不连带删回复(c, paths):
    mid = H.bot_moment(paths.db, "anna")
    cid = H.add_comment(paths.db_path, mid, H.ME, "我的")
    reply = H.add_comment(paths.db_path, mid, "anna", "回我", parent=cid)
    assert c.delete("/api/comment/%d" % cid, json={}).status_code == 200
    assert [r["id"] for r in H.comment_rows(paths.db_path, mid)] == [reply]
