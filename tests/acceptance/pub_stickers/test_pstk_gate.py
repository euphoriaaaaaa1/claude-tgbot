# -*- coding: utf-8 -*-
"""登录门、跨站核对、页面、方法不对（接口文件 §0、§1.1；行为清单 1-6、30）。"""
import pytest

import pstk_helpers as K

NAME = "000_aaaaaaaaaa.gif"
ITEM = K.item_api("sfw", NAME)
WRITES = [("POST", K.UPLOAD), ("POST", K.LINK), ("PATCH", ITEM), ("DELETE", ITEM)]
WRITE_IDS = ["upload", "link", "patch", "delete"]
HTML = {"Accept": "text/html,application/xhtml+xml"}
BODY = {"group": "sfw", "label": "新标签", "filename": "b.gif", "data": K.b64(K.gif("csrf2")),
        "url": "https://pics.stk-test.example/a.png"}


@pytest.fixture
def one_entry(paths):
    """库里一条、图片也在；只用测试自己的写法建，不经被测接口。"""
    p = K.put_image(paths, "sfw", NAME, K.gif("csrf"))
    K.write_lib(paths, {"sfw": [{"url": "", "label": "原标签", "path": p, "size": 70}], "nsfw": []})
    return K.raw(paths)


# ── 未登录（行为 1-3，现在就应成立）─────────────────────────────────────────

def test_未登录_打开页面_302去登录且next是原路径(anon):
    r = anon.get(K.PAGE, headers=HTML)
    assert r.status_code == 302
    assert r.headers["Location"] == "/login?next=/console/stickers"


def test_未登录_取图片_302去登录且Location含原路径(anon):
    r = anon.get("/console/stickers/img/sfw/" + NAME, headers=HTML)
    assert r.status_code == 302
    loc = r.headers["Location"]
    assert loc.startswith("/login?next=") and "/console/stickers/img/sfw/" + NAME in loc


def test_未登录_取列表_401(anon, paths):
    r = anon.get(K.LIST)
    assert r.status_code == 401 and r.get_json() == {"error": "unauthorized"}
    assert not paths.json.exists()


@pytest.mark.parametrize("method,path", WRITES, ids=WRITE_IDS)
def test_未登录_写接口_401且什么都不写(anon, paths, one_entry, method, path):
    r = anon.open(path, method=method, json=BODY)
    assert r.status_code == 401 and r.get_json() == {"error": "unauthorized"}
    assert K.raw(paths) == one_entry
    assert K.listing(paths.dir / "sfw") == {NAME} and not paths.removed.exists()


# ── 跨站与内容类型（行为 4、5，现在就应成立）──────────────────────────────────

@pytest.mark.parametrize("method,path", WRITES, ids=WRITE_IDS)
def test_写接口_跨站Origin_403且文件不动(c, paths, one_entry, method, path):
    r = c.open(path, method=method, json=BODY, headers={"Origin": "https://evil.test"})
    assert r.status_code == 403 and r.get_json() == {"error": "cross-site request rejected"}
    assert K.raw(paths) == one_entry


@pytest.mark.parametrize("method,path", WRITES, ids=WRITE_IDS)
def test_写接口_同源但text_plain_415且文件不动(c, paths, one_entry, method, path):
    r = c.open(path, method=method, data="{}", content_type="text/plain", headers={"Origin": "http://localhost"})
    assert r.status_code == 415
    assert r.get_json() == {"error": "content-type must be application/json"}
    assert K.raw(paths) == one_entry


# ── 同源 JSON 放行（红）────────────────────────────────────────────────────

def test_PATCH_同源Origin加JSON_放行改成功(c, paths, one_entry):
    r = c.patch(ITEM, json={"label": "新标签"}, headers={"Origin": "http://localhost"})
    assert K.ok_item(r)["label"] == "新标签"


def test_DELETE_同源Origin加空JSON体_放行删成功(c, paths, one_entry):
    r = c.delete(ITEM, json={}, headers={"Origin": "http://localhost"})
    assert r.status_code == 200 and r.get_json() == {"ok": True, "moved": True}


# ── 页面（行为 6）──────────────────────────────────────────────────────────

def test_已登录_打开页面_200的HTML(c):
    r = c.get(K.PAGE, headers=HTML)
    assert r.status_code == 200
    assert r.mimetype == "text/html"
    assert "<h1>表情包</h1>" in r.get_data(as_text=True)


def test_已登录_页面带上管理台共用顶栏(c):
    body = c.get(K.PAGE, headers=HTML).get_data(as_text=True)
    assert 'href="/provider"' in body and 'action="/logout"' in body


# ── 方法不对与不存在的路径（行为 30，公开版差异）──────────────────────────────

def test_GET单条接口_405_method_not_allowed(c):
    r = c.get("/api/console/stickers/sfw/x.gif")
    assert r.status_code == 405
    assert r.get_json() == {"error": "method_not_allowed"}


def test_api下不存在的路径_404_not_found(c):
    r = c.get("/api/console/stickers-nope/x")
    assert r.status_code == 404
    assert r.get_json() == {"error": "not_found"}
