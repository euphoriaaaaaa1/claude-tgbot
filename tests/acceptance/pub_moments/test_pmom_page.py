# -*- coding: utf-8 -*-
"""朋友圈页渲染（接口文件 1.1、§2；行为清单第 9–16 条），以及名单按"人"合并、两边都在跑、简介。

经登录后的 GET /moments 看渲染出的 HTML；名单来自底层登记表替身。"""
import re
import time

import pytest

import pmom_helpers as H


def page(c, url="/moments"):
    r = c.get(url)
    assert r.status_code == 200, (url, r.status_code)
    return r.get_data(as_text=True)


def _insert_raw_moment(paths, bot_id, text, metadata_json, visibility="public", image_path=None):
    return H.x(paths.db_path, "INSERT INTO moments (bot_id, ts, text, image_path, metadata_json, moment_kind, visibility)"
                              " VALUES (?,?,?,?,?,?,?)",
               (bot_id, int(time.time()), text, image_path, metadata_json, "daily", visibility))


# ── 共用导航与页面不该有的东西（9、10）──────────────────────────────────────

def test_页面带共用导航_四个入口与登出(c):
    html = page(c)
    for href in ('href="/console"', 'href="/provider"', 'href="/styles"', 'href="/params"'):
        assert href in html, href
    assert re.search(r'<form\b[^>]*action="/logout"', html), "没有登出表单"


def test_页面没有生图引擎按钮与接口(c):
    html = page(c)
    assert "engine-chip" not in html and "/api/moments/image_engine" not in html and "image_provider" not in html


def test_页面不引用站外脚本样式字体图片(c):
    html = page(c)
    bad = re.findall(r'<(?:script|link|img|iframe)\b[^>]*\b(?:src|href)\s*=\s*["\']?(?:https?:)?//[^"\'\s>]*', html, re.I)
    bad += re.findall(r'@import[^;]*', html, re.I) + re.findall(r'url\(\s*["\']?(?:https?:)?//[^)]*\)', html, re.I)
    assert bad == [], bad


def test_页面没有三个bot字样(c, backends):
    backends.add("claude", bots=[H.bot("anna"), H.bot("bella"), H.bot("coco")])
    assert "三个 bot" not in page(c)


# ── 名单（11）────────────────────────────────────────────────────────────────

def test_两侧的人都出chip_停用的也出(c, backends):
    backends.add("claude", bots=[H.bot("anna"), H.bot("bella", state="stopped")])
    backends.add("dsh", bots=[H.bot("coco", state="idle"), H.bot("dora", state="broken")])
    assert set(H.chips(page(c))) == {"anna", "bella", "coco", "dora"}


def test_主页chip链到问号(c):
    assert re.search(r'<a\b[^>]*\bhref\s*=\s*"\?"', H.markup(page(c))), "没有 href=\"?\" 的主页链接"


@pytest.mark.parametrize("method", ["bots", "meta"])
def test_某一侧方法抛错_只少那一侧_页面照常200(c, backends, method):
    from adapters import AdapterUnavailable
    backends.add("claude", bots=[H.bot("anna")])
    backends.add("dsh", bots=[H.bot("coco")]).fail[method] = AdapterUnavailable("down")
    assert set(H.chips(page(c))) == {"anna"}


def test_某一侧方法抛普通异常_只少那一侧_页面照常200(c, backends):
    backends.add("claude", bots=[H.bot("anna")]).fail["bots"] = RuntimeError("模拟坏掉")
    backends.add("dsh", bots=[H.bot("coco")])
    assert set(H.chips(page(c))) == {"coco"}


def test_取适配器本身抛不可用_只少那一侧_页面照常200(c, backends):
    from adapters import AdapterUnavailable
    backends.add("claude", bots=[H.bot("anna")])
    backends.add("dsh", bots=[H.bot("coco")])
    backends.unavailable["dsh"] = AdapterUnavailable("down")
    assert set(H.chips(page(c))) == {"anna"}


def test_某一侧取名单超时_只少那一侧_页面不被拖住(c, backends, mr, monkeypatch):
    import threading
    monkeypatch.setattr(mr, "BOTS_TIMEOUT", 0.5)
    backends.add("claude", bots=[H.bot("anna")])
    backends.add("dsh", bots=[H.bot("coco")]).block = threading.Event()
    t0 = time.monotonic()
    html = page(c)
    assert time.monotonic() - t0 < 8, "取名单超时后页面应很快回来"
    assert set(H.chips(html)) == {"anna"}


def test_没有person时用bot_id当人(c, backends):
    backends.add("dsh", bots=[H.bot("coco")])
    assert set(H.chips(page(c))) == {"coco"}


def test_有person时按person归人_不出bot_id的chip(c, backends):
    backends.add("dsh", bots=[H.bot("dsh-7", person="anna")])
    assert set(H.chips(page(c))) == {"anna"}


@pytest.mark.parametrize("person", ["  ", "x" * 65, 12])
def test_person不是1到64字符的字符串时用bot_id(c, backends, person):
    backends.add("dsh", bots=[H.bot("coco", person=person)])
    assert set(H.chips(page(c))) == {"coco"}


def test_person是用户主页键的条目丢掉(c, backends):
    backends.add("dsh", bots=[H.bot("coco", person="__user__"), H.bot("dora")])
    assert set(H.chips(page(c))) == {"dora"}


@pytest.mark.parametrize("bad_id", ["../x", "a b", "x" * 65, ""])
def test_bot_id不合规的条目丢掉(c, backends, bad_id):
    backends.add("dsh", bots=[H.bot(bad_id), H.bot("dora")])
    assert set(H.chips(page(c))) == {"dora"}


def test_同一人一侧停用一侧在跑_只出一个chip(c, backends):
    backends.add("claude", bots=[H.bot("anna", state="idle")])
    backends.add("dsh", bots=[H.bot("dsh-5", person="anna", state="stopped")])
    html = page(c)
    assert set(H.chips(html)) == {"anna"} and 'data-role="conflict"' not in html


def test_同一人两侧都停用_只列一份_取dsh那份的显示名(c, backends):
    backends.add("claude", bots=[H.bot("anna", state="stopped", display_name="克劳德侧的安娜")])
    backends.add("dsh", bots=[H.bot("dsh-5", person="anna", state="stopped", display_name="dsh侧的安娜")])
    html = page(c)
    assert set(H.chips(html)) == {"anna"}
    assert "dsh侧的安娜" in html and "克劳德侧的安娜" not in html


# ── 两边都在跑（12）──────────────────────────────────────────────────────────

def test_同一人两侧都在跑_顶部有冲突提示_chip上标两边都在跑(c, backends):
    backends.add("claude", bots=[H.bot("anna", state="idle")])
    backends.add("dsh", bots=[H.bot("dsh-5", person="anna", state="running")])
    html = page(c)
    assert 'data-role="conflict"' in html
    ch = H.chips(html)
    assert set(ch) == {"anna"} and "两边都在跑" in ch["anna"]


def test_同一侧两份都在跑_不算两边都在跑(c, backends):
    backends.add("dsh", bots=[H.bot("dsh-5", person="anna"), H.bot("dsh-9", person="anna")])
    html = page(c)
    assert set(H.chips(html)) == {"anna"} and 'data-role="conflict"' not in html


def test_坏了的不算在跑_不标冲突(c, backends):
    backends.add("claude", bots=[H.bot("anna", state="idle")])
    backends.add("dsh", bots=[H.bot("dsh-5", person="anna", state="broken")])
    assert 'data-role="conflict"' not in page(c)


def test_没有冲突时页面没有冲突提示元素(c, backends):
    backends.add("claude", bots=[H.bot("anna")])
    backends.add("dsh", bots=[H.bot("coco")])
    html = page(c)
    assert 'data-role="conflict"' not in html and "两边都在跑" not in H.markup(html)


# ── 转义（13、14）────────────────────────────────────────────────────────────

def test_动态正文里的标签与引号被转义(c, paths):
    H.user_moment(paths.db, '<script>pmomXSS1()</script> Q1"Q2\'Q3')
    html = page(c)
    assert "<script>pmomXSS1()" not in html and "&lt;script&gt;pmomXSS1()" in html
    assert 'Q1"Q2' not in html and "Q2'Q3" not in html


def test_评论里的标签被转义(c, paths):
    mid = H.user_moment(paths.db, "普通动态")
    H.add_comment(paths.db_path, mid, H.ME, '<script>pmomXSS2()</script> C1"C2')
    html = page(c)
    assert "<script>pmomXSS2()" not in html and "&lt;script&gt;pmomXSS2()" in html
    assert 'C1"C2' not in html


def test_底层报的显示名里的标签被转义(c, backends):
    backends.add("claude", bots=[H.bot("anna", display_name="<i>DN-pm</i>")])
    html = page(c)
    assert "<i>DN-pm</i>" not in html and "&lt;i&gt;DN-pm&lt;/i&gt;" in html


@pytest.mark.parametrize("who", ["__user__", "anna"])
def test_库里头像封面是坏值时页面不输出该背景样式(c, paths, backends, who):
    backends.add("claude", bots=[H.bot("anna")])
    bad = "x');background:url('http://e/t"
    H.x(paths.db_path, "INSERT OR REPLACE INTO bot_profiles (bot_id, avatar_url, banner_url, updated_at) VALUES (?,?,?,?)",
        (who, bad, bad, 1))
    html = page(c, "/moments" if who == "__user__" else "/moments?bot=anna")
    assert [s for s in H.style_attrs(html) if "http://e/t" in s] == []
    styles = "".join(re.findall(r"<style\b[^>]*>([\s\S]*?)</style>", html, re.I))
    assert "http://e/t" not in styles


def test_合法封面地址里的特殊字符在样式里照常转义(c, paths):
    paths.db.set_profile("__user__", banner_url="https://example.com/a.png?a=1&b=<2>")
    html = page(c)
    assert any("https://example.com/a.png?a=1&b=<2>" in s for s in H.style_attrs(html))
    assert "b=<2>" not in H.markup(html)


# ── 按钮与输入框（15）────────────────────────────────────────────────────────

def test_主页上每条用户动态各有一个删除按钮_bot动态没有(c, paths, backends):
    backends.add("claude", bots=[H.bot("anna")])
    H.user_moment(paths.db, "我的一")
    H.user_moment(paths.db, "我的二")
    H.bot_moment(paths.db, "anna", "安娜的", ts=int(time.time()) - 30)
    assert H.class_count(page(c), "moment-del") == 2


def test_切到bot页_bot动态卡片没有删除按钮(c, paths, backends):
    backends.add("claude", bots=[H.bot("anna")])
    H.bot_moment(paths.db, "anna", "安娜的一")
    H.bot_moment(paths.db, "anna", "安娜的二")
    html = page(c, "/moments?bot=anna")
    assert "安娜的一" in html and H.class_count(html, "moment-del") == 0


def test_加一条用户署名的评论_页面多一个删除叉(c, paths):
    mid = H.user_moment(paths.db, "看叉")
    H.add_comment(paths.db_path, mid, "anna", "bot 的评论")
    n0 = H.times_marks(page(c))
    H.add_comment(paths.db_path, mid, H.ME, "我的评论")
    assert H.times_marks(page(c)) == n0 + 1


def test_加一条bot的评论_页面删除叉不变(c, paths):
    mid = H.user_moment(paths.db, "看叉")
    H.add_comment(paths.db_path, mid, H.ME, "我的评论")
    n0 = H.times_marks(page(c))
    H.add_comment(paths.db_path, mid, "anna", "bot 的评论")
    assert H.times_marks(page(c)) == n0


def test_只有主页有发动态框(c, backends):
    backends.add("claude", bots=[H.bot("anna")])

    def boxes(html):
        return len(re.findall(r"<textarea\b", H.markup(html)))
    assert boxes(page(c)) > boxes(page(c, "/moments?bot=anna"))


# ── 图片与坏数据（16、D2）────────────────────────────────────────────────────

def test_bot动态只有metadata里的多张图_页面显示多张_src是本站相对地址(c, paths, backends):
    backends.add("claude", bots=[H.bot("anna")])
    imgs = [H.write_file(paths.media / "anna" / ("g%d.png" % i)) for i in range(3)]
    H.bot_moment(paths.db, "anna", "九宫格", metadata={"image_paths": [str(p) for p in imgs]})
    srcs = H.img_srcs(page(c, "/moments?bot=anna"))
    for i in range(3):
        assert "/image/anna/g%d.png" % i in srcs, srcs


def test_不在图片根下的配图不显示_页面照常_不出现绝对路径(c, paths, backends):
    backends.add("claude", bots=[H.bot("anna")])
    outside = H.write_file(paths.tmp / "outside" / "out-pic.png")
    H.bot_moment(paths.db, "anna", "根外配图", image_path=outside)
    html = page(c, "/moments?bot=anna")
    assert "根外配图" in html
    assert [s for s in H.img_srcs(html) if "out-pic" in s] == []
    assert str(paths.tmp) not in html


def test_配图src不含图片根的绝对路径(c, paths, backends):
    backends.add("claude", bots=[H.bot("anna")])
    H.bot_moment(paths.db, "anna", "单图", image_path=H.write_file(paths.media / "anna" / "one.png"))
    html = page(c, "/moments?bot=anna")
    assert "/image/anna/one.png" in H.img_srcs(html) and str(paths.media) not in html


@pytest.mark.parametrize("meta", ["{bad json", "", None, "[1,2]"], ids=["坏JSON", "空串", "null", "不是对象"])
def test_metadata_json是坏值_页面照常200_正文照显(c, paths, backends, meta):
    backends.add("claude", bots=[H.bot("anna")])
    _insert_raw_moment(paths, "anna", "坏元数据的动态", meta)
    assert "坏元数据的动态" in page(c, "/moments?bot=anna")


def test_visibility为空的动态照常显示(c, paths, backends):
    backends.add("claude", bots=[H.bot("anna")])
    _insert_raw_moment(paths, "anna", "可见性为空", "{}", visibility="")
    assert "可见性为空" in page(c, "/moments?bot=anna")


# ── 简介（生活设定）──────────────────────────────────────────────────────────

def test_切到某人_简介取生活设定里的bio(c, backends):
    backends.add("dsh", bots=[H.bot("coco")], caps={"bot.life_settings": True},
                 life={"coco": {"bio": "爱画画的哨兵", "persona_summary": "侧写"}})
    html = page(c, "/moments?bot=coco")
    assert "爱画画的哨兵" in html


def test_bio为空时简介用persona_summary(c, backends):
    backends.add("dsh", bots=[H.bot("coco")], caps={"bot.life_settings": True},
                 life={"coco": {"bio": "", "persona_summary": "侧写里的简介"}})
    assert "侧写里的简介" in page(c, "/moments?bot=coco")


def test_底层没报生活设定能力时不调get_life(c, backends):
    f = backends.add("dsh", bots=[H.bot("coco")], caps={"bot.life_settings": False},
                     life={"coco": {"bio": "不该出现的简介"}})
    html = page(c, "/moments?bot=coco")
    assert f.life_calls == [] and "不该出现的简介" not in html


def test_简介里的标签被转义(c, backends):
    backends.add("dsh", bots=[H.bot("coco")], caps={"bot.life_settings": True},
                 life={"coco": {"bio": "<b>BIO-pm</b>"}})
    html = page(c, "/moments?bot=coco")
    assert "<b>BIO-pm</b>" not in html and "&lt;b&gt;BIO-pm&lt;/b&gt;" in html
