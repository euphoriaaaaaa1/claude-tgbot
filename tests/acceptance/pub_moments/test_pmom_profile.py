# -*- coding: utf-8 -*-
"""资料写入（接口文件 1.8、1.9；行为清单第 26、28 条）：地址规则、只收三个键、显示名、请求体形状、bot_id 合法性。
头像上传 data: 地址在 test_pmom_upload.py。"""
import pytest

import pmom_helpers as H

GOOD_URLS = ["https://example.com/a.png", "http://example.com/a.jpg?x=1&y=2", "/image/anna/a.png", ""]
BAD_URLS = ["x');background:url('http://evil.example/t", "javascript:alert(1)", "ftp://example.com/a.png",
            "//evil.example/a.png", "image/a.png", "/image/", "https://", "https://example.com/a'b.png",
            'https://example.com/a"b.png', "https://example.com/a(1).png", "https://example.com/a\\b.png",
            "https://example.com/a\nb.png", "https://example.com/a\rb.png", "https://example.com/a\x0cb.png",
            "https://example.com/a\x00b.png", "https://example.com/a\x7fb.png", "/image/a.png)", 123,
            ["https://example.com/a.png"]]
OLD = "https://example.com/old.png"


def _j(r):
    return r.status_code, r.get_json(silent=True)


def _prof(c, who="anna"):
    return c.get("/api/profile/" + who).get_json()


# ── 地址规则（26）────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["avatar_url", "banner_url"])
@pytest.mark.parametrize("url", GOOD_URLS)
def test_头像封面_合法地址收下_空串表示清除(c, key, url):
    c.post("/api/profile/anna", json={key: OLD})
    r = c.post("/api/profile/anna", json={key: url})
    assert r.status_code == 200
    assert _prof(c)[key] == (url or None)


@pytest.mark.parametrize("key", ["avatar_url", "banner_url"])
@pytest.mark.parametrize("url", BAD_URLS, ids=range(len(BAD_URLS)))
def test_头像封面_不合规地址400_原值不变(c, paths, key, url):
    paths.db.set_profile("anna", **{key: OLD})
    assert _j(c.post("/api/profile/anna", json={key: url})) == (400, {"error": H.URL_RULE_MSG % key})
    assert paths.db.get_profile("anna")[key] == OLD


def test_头像地址恰好2048字符收下_2049字符400(c):
    base = "https://example.com/"
    assert c.post("/api/profile/anna", json={"avatar_url": base + "a" * (2048 - len(base))}).status_code == 200
    r = c.post("/api/profile/anna", json={"avatar_url": base + "a" * (2049 - len(base))})
    assert _j(r) == (400, {"error": H.URL_RULE_MSG % "avatar_url"})


def test_一个地址不合规_整次不写_另一个合法地址也不写(c, paths):
    r = c.post("/api/profile/anna", json={"avatar_url": "https://example.com/a.png", "banner_url": "javascript:x"})
    assert r.status_code == 400
    p = paths.db.get_profile("anna")
    assert (p["avatar_url"], p["banner_url"]) == (None, None)


# ── 只收三个键、请求体形状 ───────────────────────────────────────────────────

def test_三个键一起提交照常可用_返回ok加七个键(c, paths):
    r = c.post("/api/profile/anna", json={"avatar_url": "https://example.com/a.png", "banner_url": "",
                                          "display_name": "新名字"})
    assert r.status_code == 200
    b = r.get_json()
    assert set(b) == {"ok"} | H.PROFILE_KEYS and b["ok"] is True
    assert (b["avatar_url"], b["banner_url"], b["display_name"]) == ("https://example.com/a.png", None, "新名字")
    p = paths.db.get_profile("anna")
    assert (p["avatar_url"], p["banner_url"], p["display_name"]) == ("https://example.com/a.png", None, "新名字")


def test_多出来的键_422_报出排好序的键_什么都不写(c, paths):
    r = c.post("/api/profile/anna", json={"avatar_url": "https://example.com/a.png", "signature": "x", "bio": "y"})
    assert _j(r) == (422, {"error": "不认识的字段：bio、signature", "unknown_keys": ["bio", "signature"]})
    p = paths.db.get_profile("anna")
    assert (p["avatar_url"], p["signature"]) == (None, None)


@pytest.mark.parametrize("extra", [{"signature": "x"}, {"display_name": "a", "__proto__": 1}])
def test_多出来的键_unknown_keys只列多出来的(c, paths, extra):
    r = c.post("/api/profile/anna", json=extra)
    assert r.status_code == 422
    assert r.get_json()["unknown_keys"] == sorted(set(extra) - {"avatar_url", "banner_url", "display_name"})
    assert paths.db.get_profile("anna")["display_name"] is None


@pytest.mark.parametrize("body", [[1, 2], "x", 3, None])
def test_请求体不是JSON对象_422(c, body):
    r = c.post("/api/profile/anna", data=H.jdump(body), content_type="application/json")
    assert _j(r) == (422, {"error": "请求体必须是 JSON 对象"})


def test_请求体不是合法JSON_422(c):
    r = c.post("/api/profile/anna", data="{oops", content_type="application/json")
    assert _j(r) == (422, {"error": "请求体必须是 JSON 对象"})


def test_不传的键保持原值(c, paths):
    paths.db.set_profile("anna", avatar_url="https://example.com/a.png", banner_url="https://example.com/b.png")
    assert c.post("/api/profile/anna", json={"display_name": "只改名"}).status_code == 200
    p = paths.db.get_profile("anna")
    assert (p["avatar_url"], p["banner_url"], p["display_name"]) == (
        "https://example.com/a.png", "https://example.com/b.png", "只改名")


# ── 显示名 ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [3, ["a"], {"a": 1}])
def test_显示名不是字符串_400(c, paths, name):
    assert _j(c.post("/api/profile/anna", json={"display_name": name})) == (400, {"error": "display_name 必须是字符串"})
    assert paths.db.get_profile("anna")["display_name"] is None


def test_显示名去空白后31字_400(c, paths):
    assert _j(c.post("/api/profile/anna", json={"display_name": "长" * 31})) == (
        400, {"error": "display_name too long (>30)"})
    assert paths.db.get_profile("anna")["display_name"] is None


def test_显示名去空白后恰好30字_收下并去掉首尾空白(c):
    assert c.post("/api/profile/anna", json={"display_name": "  " + "长" * 30 + "  "}).status_code == 200
    assert _prof(c)["display_name"] == "长" * 30


def test_显示名给空串_恢复默认(c, paths):
    paths.db.set_display_name("anna", "旧名")
    assert c.post("/api/profile/anna", json={"display_name": ""}).status_code == 200
    assert _prof(c)["display_name"] is None


# ── bot_id 合法性（28，D8）──────────────────────────────────────────────────

BAD_IDS = ["a%20b", "...", "x" * 65, "%E4%B8%AD"]


@pytest.mark.parametrize("bot_id", BAD_IDS, ids=["含空格", "只有点", "65字符", "中文"])
def test_读资料_bot_id不合规_404(c, bot_id):
    assert _j(c.get("/api/profile/" + bot_id)) == (404, {"error": "not found"})


@pytest.mark.parametrize("bot_id", BAD_IDS, ids=["含空格", "只有点", "65字符", "中文"])
def test_写资料_bot_id不合规_404_什么都不写(c, paths, bot_id):
    r = c.post("/api/profile/" + bot_id, json={"display_name": "x"})
    assert r.status_code == 404
    assert H.q(paths.db_path, "SELECT * FROM bot_profiles") == []


@pytest.mark.parametrize("bot_id", ["__user__", "a.b-c_9", "x" * 64])
def test_合规bot_id与用户主页键_可写可读(c, bot_id):
    assert c.post("/api/profile/" + bot_id, json={"display_name": "名"}).status_code == 200
    assert _prof(c, bot_id)["display_name"] == "名"
