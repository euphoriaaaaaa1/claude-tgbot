# -*- coding: utf-8 -*-
"""M1 空壳骨架的骨架级自测（PLAN §5 M1 / INTERFACE §2）。

只测 M1 自己的产出：四段路由的方法绑定与 501 形状、三个页面的关键元素、
导航 include 三页一致、两个老页面的「← 管理台」链接。

不起 moments.web（那会拖进 db / config_loader / 鉴权门，那是 M0 与验收测试的地盘），
只把蓝图挂到一个裸 Flask app 上 —— 要验的正是蓝图和模板本身。
"""
from pathlib import Path

import pytest
from flask import Flask

from moments.hub_routes import hub_bp

TEMPLATES = Path(__file__).resolve().parents[2] / "moments" / "templates"

# 后三段的全部空壳端点（method, path）。M3/M5/M7 每填一个就把它从这里挪进各自的用例。
# 第 2 段（provider CRUD / activate / claude-native / 自测）已由 M3 填完，
# 用例挪到 tests/unit/test_hub_routes_provider.py，故不在此清单里。
SHELL_ENDPOINTS = [
    # 三段（provider/OAuth/bots）均已实现，空壳清单归空；各段契约见
    # test_hub_routes_provider.py / test_hub_oauth.py / test_bots_client.py
]


@pytest.fixture
def client():
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.register_blueprint(hub_bp)
    app.config["TESTING"] = True
    return app.test_client()


def page(client, path):
    r = client.get(path)
    assert r.status_code == 200, "%s 期望 200，实得 %s" % (path, r.status_code)
    return r.get_data(as_text=True)


@pytest.mark.parametrize("method,path", SHELL_ENDPOINTS)
def test_空壳端点回501合规JSON(client, method, path):
    r = client.open(path, method=method)
    assert r.status_code == 501, "%s %s 实得 %s" % (method, path, r.status_code)
    assert r.headers["Content-Type"].startswith("application/json")
    assert r.get_json() == {"error": "not_implemented"}


def test_四段都在_每段至少一个端点(client):
    rules = {r.rule for r in client.application.url_map.iter_rules()}
    for seg in ("/hub", "/hub/api/provider", "/hub/api/oauth/accounts", "/hub/api/bots"):
        assert seg in rules, "第 X 段缺端点：%s" % seg


def test_不重复注册healthz(client):
    """/hub/healthz 归 hub_auth.install()，蓝图再注册一遍会撞 endpoint。"""
    rules = {r.rule for r in client.application.url_map.iter_rules()}
    assert "/hub/healthz" not in rules


def test_activate只收POST(client):
    """§0.1 断言 B：该路径只允许 POST，GET 必须 405（JSON 化由 web.py 的 errorhandler 负责）。"""
    assert client.get("/hub/api/provider/abc12345/activate").status_code == 405


@pytest.mark.parametrize("path", ["/hub", "/hub/provider", "/hub/bots"])
def test_三个页面都带同一条导航(client, path):
    body = page(client, path)
    for href in ('href="/hub"', 'href="/hub/provider"', 'href="/hub/bots"',
                 'href="/"', 'href="/styles"'):
        assert href in body, "%s 的导航缺 %s" % (path, href)


def test_导航高亮各页不同(client):
    """nav_active 传错会让三页都高亮同一个 chip —— 这是 include 复用最容易出的错。"""
    marks = {p: page(client, p).count('class="chip on"')
             for p in ("/hub", "/hub/provider", "/hub/bots")}
    assert set(marks.values()) == {1}, "每页应恰好高亮一个导航项，实得 %s" % marks
    assert 'href="/hub/provider" class' not in page(client, "/hub")


def test_门户首页四张卡片(client):
    body = page(client, "/hub")
    for href in ('"/hub/provider"', '"/hub/bots"', '"/styles"', 'href="/"'):
        assert href in body, "门户首页缺卡片入口 %s" % href


def test_模型来源页六字段齐全(client):
    """§3.3 的六个请求字段必须有输入位，否则用户填不出合法请求。"""
    body = page(client, "/hub/provider")
    for f in ("label", "kind", "base_url", "api_key", "upstream_model", "model_alias"):
        assert 'name="%s"' % f in body or 'id="%s"' % f in body, "缺字段 %s 的输入位" % f


def test_模型来源页三块引导文案(client):
    body = page(client, "/hub/provider")
    assert "切回 Claude 官方订阅" in body and "keychain" in body.lower()   # §5
    assert "ChatGPT" in body and "Google" in body                          # §4 两家
    assert "1455" in body and "51121" in body                              # §4.2 回调端口表
    assert "地址栏" in body and "5 分钟" in body                            # §4.7


def test_模型来源页有列表容器与三个行内操作(client):
    body = page(client, "/hub/provider")
    assert 'id="provider-rows"' in body
    for act in ("test", "activate", "delete"):
        assert "'%s'" % act in body, "行内操作 %s 的入口没连上端点" % act


def test_bot状态页有重启入口与状态列(client):
    body = page(client, "/hub/bots")
    assert "重启" in body and 'id="restart-all"' in body
    for col in ("dispatcher", "phase", "排队"):
        assert col in body, "bot 状态表缺列 %s" % col


def test_kind下拉从KIND_SPEC派生(client):
    """[RA] 建议2：校验、互转、页面下拉三处同源，页面不许自己硬编码一份枚举。"""
    from moments import provider_model
    body = page(client, "/hub/provider")
    for k in provider_model.SUPPORTED_KINDS:
        assert '<option value="%s">' % k in body


@pytest.mark.parametrize("tpl", ["feed.html", "styles.html"])
def test_老页面顶部有管理台链接(tpl):
    """§2 表：`/` 与 `/styles` 各加一行 ← 管理台，是 M0 留给 M1 的白名单外交接。"""
    src = (TEMPLATES / tpl).read_text(encoding="utf-8")
    assert 'href="/hub"' in src and "管理台" in src


def test_回调端口表只此一份():
    """§4.2：端口不许在路由、文案、文档里各写一遍。页面上的数字必须来自这张表。"""
    from moments import hub_routes
    assert hub_routes.OAUTH_CALLBACK_PORTS == {"codex": 1455, "antigravity": 51121}
    src = (TEMPLATES / "hub_provider.html").read_text(encoding="utf-8")
    assert "1455" not in src, "页面模板里又硬编码了一遍回调端口"


def test_四段之间没有重名的顶层函数():
    """四段并行开发合到一个文件里，后定义的同名函数会**静默**顶掉前一个。

    真出过事：第 2 段与第 3 段各写了一个 `_compensate` / `_activate_lock`，
    合并后 provider activate 变 404，四段路由都没报错。
    规矩：段内辅助带段前缀（`_oauth_*`），共用的放模块公共区且只此一份。
    """
    import ast
    import collections

    from moments import hub_routes

    src = Path(hub_routes.__file__).read_text(encoding="utf-8")
    names = [n.name for n in ast.parse(src).body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    dup = [n for n, k in collections.Counter(names).items() if k > 1]
    assert dup == [], "hub_routes 顶层函数重名（后者顶掉前者）：%s" % dup


def test_请求体通则辅助三段共用而不是各段一份():
    """§0.1 是三段通用的通则，各段再造一个就是上一条要防的事故形状。

    直接验"用的是同一个"：第 2 段的 `_validated` 与第 3 段的 `_json_body`
    源码里都得引它，不能自己再写一遍 isinstance 判断。
    """
    import inspect

    from moments import hub_routes

    assert callable(hub_routes._require_json_object)
    for fn in (hub_routes._validated, hub_routes._json_body):
        assert "_require_json_object" in inspect.getsource(fn), fn.__name__
