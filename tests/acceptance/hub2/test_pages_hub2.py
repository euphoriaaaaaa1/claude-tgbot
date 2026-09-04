# -*- coding: utf-8 -*-
"""§1 页面与入口 + 继承一期的两把锁、框架级错误形状。

黑盒依据：INTERFACE-hub2 §1、§0（通则表）。
"""
import pytest

pytestmark = pytest.mark.usefixtures("global_yml")


def test_参数页_返回200_HTML(client):
    r = client.get("/hub/params")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("Content-Type", "")


def test_加bot页_返回200_HTML(client):
    r = client.get("/hub/addbot")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("Content-Type", "")


def test_参数页_配置文件不存在时仍返回200(configs_dir, client):
    """§2 铁律：页面层与依赖解耦，状态由前端调 API 呈现。"""
    (configs_dir / "_global.yml").unlink(missing_ok=True)
    assert client.get("/hub/params").status_code == 200


def test_加bot页_配置目录为空时仍返回200(configs_dir, client):
    for p in configs_dir.iterdir():
        p.unlink()
    assert client.get("/hub/addbot").status_code == 200


def test_hub首页_导航含两个新入口(client):
    html = client.get("/hub").get_data(as_text=True)
    assert "/hub/params" in html, "_nav 缺「系统参数」入口"
    assert "/hub/addbot" in html, "_nav 缺「添加 bot」入口"


def test_开了访问门_未登录访问参数页_302到login(make_client):
    # §13 A10：浏览器导航必带 Accept: text/html；一期 §1.4 冻结契约是
    # 「非 HTML 导航一律 401」，用例补头还原真实场景，不松安全语义。
    # Hub-Gate 头是 401 JSON 分支的 fetch 判别位，302 分支从不带 —— 断言删除。
    c, _ = make_client(HUB_ACCESS_PASSWORD="pw-access")
    r = c.get("/hub/params", headers={"Accept": "text/html"})
    assert r.status_code == 302
    assert "/login" in r.headers.get("Location", "")


def test_开了访问门_未登录访问加bot页_302到login(make_client):
    c, _ = make_client(HUB_ACCESS_PASSWORD="pw-access")
    r = c.get("/hub/addbot", headers={"Accept": "text/html"})
    assert r.status_code == 302
    assert "/login" in r.headers.get("Location", "")


def test_开了访问门_未登录调参数API_401_unauthorized(make_client, as_api):
    c, _ = make_client(HUB_ACCESS_PASSWORD="pw-access")
    code, body = as_api(c).get("/hub/api/params")
    assert code == 401
    assert body.get("error") == "unauthorized"


def test_开了管理锁_只过访问门_加bot接口_401_admin_unauthorized(make_client, as_api, do_login):
    c, _ = make_client(HUB_ACCESS_PASSWORD="pw-access", HUB_ADMIN_PASSWORD="pw-admin")
    do_login(c, password="pw-access")
    code, body = as_api(c).post("/hub/api/bots", {"bot_id": "x"})
    assert code == 401
    assert body.get("error") == "admin_unauthorized"


def test_开了管理锁_只过访问门_参数页不出内容(make_client, do_login):
    """管理锁下 HTML 的确切码 [I1] §1.4 没在二期文档里复述，只断言"页面没被端出来"。"""
    c, _ = make_client(HUB_ACCESS_PASSWORD="pw-access", HUB_ADMIN_PASSWORD="pw-admin")
    do_login(c, password="pw-access")
    r = c.get("/hub/params")
    assert r.status_code in (302, 401, 403), f"管理锁没挡住参数页：{r.status_code}"


def test_不存在的hubAPI_404同形状JSON(api):
    code, body = api.get("/hub/api/params/nope-not-a-route")
    assert code == 404
    assert body.get("error") == "not_found"


def test_测token端点_用GET调_405同形状JSON(api):
    code, body = api.get("/hub/api/bots/test_token")
    assert code == 405
    assert isinstance(body.get("error"), str) and body.get("error") != ""
