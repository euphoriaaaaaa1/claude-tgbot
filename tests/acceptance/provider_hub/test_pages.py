# -*- coding: utf-8 -*-
"""§2 页面与操作入口 + §9.1 旧路径兼容。

页面级只断言"HTML 里有关键入口元素"（href / 关键词），不引 playwright：
项目零浏览器测试基建，为几条链接引一个新框架不划算。
渲染后的交互行为不在本批覆盖内（见 TEST-PLAN 的"没覆盖什么"）。
"""
import pytest


def _html(client, path):
    r = client.get(path, headers={"Accept": "text/html"})
    assert r.status_code == 200, "%s 期望 200 HTML，实得 %s" % (path, r.status_code)
    return r.get_data(as_text=True)


def test_门户首页四张卡片入口齐全(client):
    body = _html(client, "/hub")
    for href in ('"/hub/provider"', '"/hub/bots"', '"/styles"'):
        assert href in body, "门户首页缺卡片链接 %s" % href
    assert 'href="/"' in body


@pytest.mark.parametrize("href", ["/hub/provider", "/hub/bots"])
def test_门户首页链接可达(client, href):
    assert client.get(href, headers={"Accept": "text/html"}).status_code == 200


def test_模型来源页含新增表单的六个字段(client):
    """§3.3 请求体六个字段必须在页面上有对应输入位，否则用户填不出合法请求。"""
    body = _html(client, "/hub/provider")
    for field in ("label", "kind", "base_url", "api_key", "upstream_model", "model_alias"):
        assert field in body, "「模型来源」页缺字段 %s 的输入位" % field


def test_模型来源页含切回Claude官方订阅按钮与引导(client):
    """§2 入口⑥ + §5 页面文案（凭证在 keychain/DPAPI，本页不接触）。"""
    body = _html(client, "/hub/provider")
    assert "切回 Claude 官方订阅" in body
    assert "claude" in body.lower() and "keychain" in body.lower()


def test_模型来源页含OAuth两家入口与引导文案(client):
    """§2 入口⑤ + §4.7：授权页在有浏览器的机器开、回调大概率连不上是正常的、
    复制地址栏整串 URL、5 分钟内完成。"""
    body = _html(client, "/hub/provider")
    assert "ChatGPT" in body and ("Google" in body or "Antigravity" in body)
    assert "1455" in body or "callback" in body.lower()
    assert "地址栏" in body, "缺「复制地址栏整串 URL」这一步的说明，用户会卡在连不上的回调页"
    assert "5 分钟" in body or "5分钟" in body


def test_bot状态页有重启全部入口(client):
    body = _html(client, "/hub/bots")
    assert "重启" in body


def test_朋友圈页顶部新增管理台链接(client):
    """§2 表：`/` 唯一改动是顶部加一个「← 管理台」链接。"""
    body = _html(client, "/")
    assert 'href="/hub"' in body, "朋友圈页没有回管理台的入口"
    assert "管理台" in body


def test_生图设置页顶部新增管理台链接(client):
    body = _html(client, "/styles")
    assert 'href="/hub"' in body


def test_旧路径根路由不做重定向(client):
    """§9.1 / S5：`/` 仍渲染朋友圈，门户首页另开在 /hub —— 不许 301/302 走人。"""
    r = client.get("/", headers={"Accept": "text/html"})
    assert r.status_code == 200, "GET / 变成了 %s，S5 承诺的直达路径被破坏" % r.status_code


@pytest.mark.parametrize("path", ["/hub", "/hub/provider", "/hub/bots"])
def test_cliproxy挂掉时页面仍返回200(make_client, stub, path):
    """§2 末（[R4D] I1-3）：页面层与依赖进程解耦，禁止页面本身返回 503/500。"""
    c, _ = make_client()
    stub.set_down(True)
    r = c.get(path, headers={"Accept": "text/html"})
    assert r.status_code == 200, "%s 在 cliproxy 挂掉时返回了 %s" % (path, r.status_code)


def test_settings损坏时模型来源页仍返回200(make_client, settings_path):
    """依赖故障（settings.json 手写坏）不得把页面打成 500。"""
    settings_path.write_text("{ this is not json", encoding="utf-8")
    c, _ = make_client()
    r = c.get("/hub/provider", headers={"Accept": "text/html"})
    assert r.status_code == 200


def test_页面404仍可回HTML不强制JSON(client):
    """§0.1 例外：JSON 形状只对 /hub/api/* 强制，页面路由 404 允许 HTML。"""
    r = client.get("/hub/no-such-page", headers={"Accept": "text/html"})
    assert r.status_code == 404
