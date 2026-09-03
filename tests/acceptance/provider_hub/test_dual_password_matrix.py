# -*- coding: utf-8 -*-
"""§1.0 双口令矩阵（卡点1 方案 B）：退化 / 同值合法 / 叠加不可互冒。

"盐串不同不可互冒"按可观测行为写：把一个 cookie 的值原样搬到另一个 cookie 名下，
两个方向都必须被拒 —— 这是"两把锁没塌成一把"在接口层唯一能看见的证据。
"""
from conftest import login

PW = "hub-access-pw-12345"
ADMIN_PW = "hub-admin-pw-67890"
SAME = "same-password-both-12345"
HTML = {"Accept": "text/html,application/xhtml+xml"}
FETCH = {"Accept": "*/*"}


def _cookie(client, name):
    """取测试客户端 cookie 罐里的值（werkzeug 3.x 的 get_cookie）。"""
    c = client.get_cookie(name)
    return None if c is None else c.value


def test_未设admin口令_单口令即可进hub(make_client):
    """规则1：退化为单口令，行为与只设一个口令时完全一致。"""
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    login(c, PW)
    assert c.get("/hub", headers=HTML).status_code == 200


def test_未设admin口令_永不出现admin_unauthorized(make_client):
    """§1.4 末句 / §7 总表：该码仅当 HUB_ADMIN_PASSWORD 已设时才可能出现。"""
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    assert login(c, PW).status_code == 303
    r = c.get("/hub/api/provider", headers=FETCH)
    assert r.status_code == 200, "退化形态下单口令应能直接访问，实得 %s" % r.status_code


def test_设了admin_只有全站cookie_hub_api返回admin_unauthorized(make_client):
    """规则3：管理锁是叠加的。§1.4：401 {"error":"admin_unauthorized"} + Hub-Gate: 1。"""
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW, HUB_ADMIN_PASSWORD=ADMIN_PW)
    login(c, PW)                       # 只填全站口令
    r = c.get("/hub/api/provider", headers=FETCH)
    assert r.status_code == 401
    assert r.get_json() == {"error": "admin_unauthorized"}
    assert r.headers.get("Hub-Gate") == "1"


def test_设了admin_只有全站cookie_hub页面导航302到login(make_client):
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW, HUB_ADMIN_PASSWORD=ADMIN_PW)
    login(c, PW)
    r = c.get("/hub", headers=HTML)
    assert r.status_code == 302
    assert r.headers.get("Location", "").endswith("/login")


def test_设了admin_只有全站cookie_朋友圈仍可看(make_client):
    """管理锁只管 /hub 与 /hub/api/*；家人拿全站口令仍能看朋友圈，这是正常路径。"""
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW, HUB_ADMIN_PASSWORD=ADMIN_PW)
    assert login(c, PW).status_code == 303
    assert _cookie(c, "hub_session"), "全站 cookie 没签发，后面的断言等于空转"
    assert c.get("/", headers=HTML).status_code == 200
    assert c.get("/styles", headers=HTML).status_code == 200


def test_设了admin_只填全站口令时登录跳根而不是hub(make_client):
    """§1.5 第二行：只有 password 对 → 303 到 `/`，且只签一个 cookie，不算失败。"""
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW, HUB_ADMIN_PASSWORD=ADMIN_PW)
    r = login(c, PW, admin_password="")
    assert r.status_code == 303
    assert r.headers.get("Location", "").endswith("/")
    assert _cookie(c, "hub_session") and _cookie(c, "hub_admin") is None


def test_两把口令都对_一次POST拿到两个cookie并跳hub(make_client):
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW, HUB_ADMIN_PASSWORD=ADMIN_PW)
    r = login(c, PW, admin_password=ADMIN_PW)
    assert r.status_code == 303
    assert r.headers.get("Location", "").endswith("/hub")
    assert _cookie(c, "hub_session") and _cookie(c, "hub_admin")
    assert c.get("/hub", headers=HTML).status_code == 200


def test_全站口令错但admin口令对_一个cookie都不签(make_client):
    """§1.5：password 错 → 不签任何 cookie（admin_password 对也不签）。"""
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW, HUB_ADMIN_PASSWORD=ADMIN_PW)
    r = login(c, "wrong-password-xxxx", admin_password=ADMIN_PW)
    assert r.status_code == 401
    assert not r.headers.getlist("Set-Cookie")


def test_两把口令同值是合法配置_不得报错或警告(make_client):
    """规则2：接口层禁止加"两口令必须不同"校验。走完登录后 /hub 必须 200。"""
    c, _ = make_client(HUB_ACCESS_PASSWORD=SAME, HUB_ADMIN_PASSWORD=SAME)
    r = login(c, SAME, admin_password=SAME)
    assert r.status_code == 303, "同值配置被拒 = 违反 §1.0 规则2"
    assert c.get("/hub", headers=HTML).status_code == 200


def test_只有admin_cookie而无全站cookie_同样被拒(make_client):
    """规则3 后半句：两个 cookie 缺一不可。"""
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW, HUB_ADMIN_PASSWORD=ADMIN_PW)
    login(c, PW, admin_password=ADMIN_PW)
    c.delete_cookie("hub_session")
    r = c.get("/hub/api/provider", headers=FETCH)
    assert r.status_code == 401


def test_同值口令下_全站cookie冒充管理cookie必须失败(make_client):
    """§1.2 盐串不同（hubv2| vs hubadminv2|）唯一的可观测证据：两把锁没塌成一把。

    把 hub_session 的值原样搬到 hub_admin 名下：盐串若相同，签名会照样验过 → /hub 放行。
    """
    c, _ = make_client(HUB_ACCESS_PASSWORD=SAME, HUB_ADMIN_PASSWORD=SAME)
    login(c, SAME, admin_password=SAME)
    sess = _cookie(c, "hub_session")
    assert sess, "登录未签发 hub_session，前置条件不成立"
    c.set_cookie("hub_admin", sess)
    r = c.get("/hub/api/provider", headers=FETCH)
    assert r.status_code == 401, "全站 cookie 冒充成了管理 cookie → 两把锁塌成一把（盐串没分开）"
    assert r.get_json().get("error") == "admin_unauthorized"


def test_同值口令下_管理cookie冒充全站cookie必须失败(make_client):
    """反向：hub_admin 的值搬到 hub_session 名下，访问非 hub 路径也必须被拒。"""
    c, _ = make_client(HUB_ACCESS_PASSWORD=SAME, HUB_ADMIN_PASSWORD=SAME)
    login(c, SAME, admin_password=SAME)
    admin = _cookie(c, "hub_admin")
    assert admin, "登录未签发 hub_admin，前置条件不成立"
    c.set_cookie("hub_session", admin)
    r = c.get("/api/moments", headers=FETCH)
    assert r.status_code == 401, "管理 cookie 冒充成了全站 cookie（盐串没分开）"
