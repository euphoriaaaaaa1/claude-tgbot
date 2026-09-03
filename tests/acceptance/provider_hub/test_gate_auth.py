# -*- coding: utf-8 -*-
"""§1 鉴权门：开关、放行清单、未鉴权两种形状、cookie 结构、双口令矩阵。

黑盒：只按 INTERFACE §1 的表格断言状态码 / error 码 / 响应头 / cookie 属性。
"""
import hashlib
import hmac
import time

import pytest

from conftest import login

PW = "hub-access-pw-12345"      # ≥12，避开 §1.1 的 WARN 口径
ADMIN_PW = "hub-admin-pw-67890"
HTML = {"Accept": "text/html,application/xhtml+xml"}
FETCH = {"Accept": "*/*"}


def _sign(pw, salt, exp, nonce):
    """§1.2 的 cookie 值结构：<exp>.<16hex nonce>.<hmac_sha256(pw, salt+exp+"|"+nonce)>"""
    msg = "%s%d|%s" % (salt, exp, nonce)
    sig = hmac.new(pw.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return "%d.%s.%s" % (exp, nonce, sig)


def test_门关闭时朋友圈首页直达_不跳登录(make_client):
    """未设 HUB_ACCESS_PASSWORD 且监听回环 → 门关闭，老用户零改造。"""
    c, _ = make_client()
    r = c.get("/", headers=HTML)
    assert r.status_code == 200, "门关闭时 GET / 必须直出朋友圈，不得跳登录"


def test_门开启_未登录_HTML导航_302到login(make_client):
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    r = c.get("/hub", headers=HTML)
    assert r.status_code == 302
    assert r.headers.get("Location", "").endswith("/login")


def test_门开启_未登录_fetch请求_401且带HubGate头(make_client):
    """§1.4：非 HTML 导航一律 401 + {"error":"unauthorized"} + Hub-Gate: 1。"""
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    r = c.get("/hub/api/provider", headers=FETCH)
    assert r.status_code == 401
    assert r.get_json() == {"error": "unauthorized"}
    assert r.headers.get("Hub-Gate") == "1"


def test_门开启_未登录_POST不重定向而是401(make_client):
    """POST 即使 Accept 含 text/html 也必须 401（表里"其余（含所有 POST）"）。"""
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    r = c.post("/hub/api/provider", json={}, headers=HTML)
    assert r.status_code == 401
    assert r.get_json().get("error") == "unauthorized"
    assert r.headers.get("Hub-Gate") == "1"


def test_放行清单_hub_healthz免鉴权且不泄配置(make_client):
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    r = c.get("/hub/healthz", headers=FETCH)
    assert r.status_code == 200
    assert r.get_json() == {"status": "ok"}, "healthz 只能回 status，多一个字段都是泄配置"


def test_放行清单_login页免鉴权(make_client):
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    r = c.get("/login", headers=HTML)
    assert r.status_code == 200


def test_放行清单是精确匹配_login_bypass不得被放行(make_client):
    """§1.3：禁止 startswith。被误放行的表现是走到路由拿 404；正确实现应在门上被拦。"""
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    r = c.get("/login-bypass", headers=FETCH)
    assert r.status_code == 401, "前缀匹配漏洞：/login-bypass 绕过了鉴权门（正确应 401）"


def test_放行清单是精确匹配_healthz前缀变体不得被放行(make_client):
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    r = c.get("/hub/healthz-secret", headers=FETCH)
    assert r.status_code == 401


@pytest.mark.parametrize("path", ["/", "/styles", "/api/moments", "/image/x.png", "/hub"])
def test_门开启后既有路径一律要cookie(make_client, path):
    """§1.3：改造后 `/`、`/styles`、`/api/*`、`/image/*` 与 /hub* 全部进门。"""
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    r = c.get(path, headers=FETCH)
    assert r.status_code == 401, "%s 未被鉴权门覆盖" % path


def test_没有裸healthz路由(make_client):
    """§0：禁止出现不带前缀的裸 /healthz（两个 healthz 主体不同，名字必须分开）。"""
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    r = c.get("/healthz", headers=FETCH)
    assert r.status_code != 200, "裸 /healthz 不该存在（§0 明令禁止）"


def test_过期cookie即使签名正确也401(make_client):
    """§1.2 可断言项：exp 为过去时刻但签名正确 → 401。"""
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    expired = _sign(PW, "hubv2|", int(time.time()) - 60, "0123456789abcdef")
    c.set_cookie("hub_session", expired)
    r = c.get("/hub/api/provider", headers=FETCH)
    assert r.status_code == 401
    assert r.get_json() == {"error": "unauthorized"}


def test_签名错误的cookie与缺cookie响应完全一致(make_client):
    """§1.2：cookie 不合法与 cookie 缺失必须不可区分（否则给爆破者一个 oracle）。"""
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    missing = c.get("/hub/api/provider", headers=FETCH)
    assert missing.status_code == 401, "门没拦住，不可区分性无从谈起"
    c.set_cookie("hub_session", "%d.deadbeefdeadbeef.%s" % (int(time.time()) + 600, "ff" * 32))
    bad = c.get("/hub/api/provider", headers=FETCH)
    assert (bad.status_code, bad.get_json()) == (missing.status_code, missing.get_json())
    assert bad.headers.get("Hub-Gate") == missing.headers.get("Hub-Gate")


def test_两次登录的cookie值不同_nonce生效(make_client):
    """§1.2 可断言项：cookie 不是口令的确定性函数。"""
    c1, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    c2, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    v1 = login(c1, PW).headers.get("Set-Cookie", "")
    v2 = login(c2, PW).headers.get("Set-Cookie", "")
    assert v1 and v2
    assert v1 != v2, "两次登录 cookie 相同 → nonce 未生效，泄漏一次等于永久凭据"


def test_cookie属性_HttpOnly与SameSiteLax且http下不设Secure(make_client):
    """§1.2 属性表：http://127.0.0.1 下设 Secure 浏览器不会存 —— [SEC] 2.3-4 点名的坑。"""
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    raw = ";".join(login(c, PW).headers.getlist("Set-Cookie"))
    assert "HttpOnly" in raw
    assert "SameSite=Lax" in raw
    assert "Secure" not in raw, "http 下不得设 Secure，否则浏览器丢弃 cookie，用户永远登不上"


def test_登录成功后能访问受保护路径(make_client):
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    assert login(c, PW).status_code == 303
    assert c.get("/", headers=HTML).status_code == 200


def test_登录口令错误_401且不签发cookie(make_client):
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    r = login(c, "wrong-password-xxxx")
    assert r.status_code == 401
    assert not r.headers.getlist("Set-Cookie"), "口令错还签 cookie = 门形同虚设"


def test_登录表单缺password字段_与口令错误响应完全一致(make_client):
    """§1.5：不得 KeyError→500，也不得用 400 泄漏"字段格式对不对"这一位信息。"""
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    wrong = login(c, "wrong-password-xxxx")
    c2, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    missing = c2.post("/login", data={}, content_type="application/x-www-form-urlencoded")
    assert missing.status_code == wrong.status_code == 401
    assert missing.get_data() == wrong.get_data()


def test_登录ContentType不是表单编码_与口令错误响应一致(make_client):
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    wrong = login(c, "wrong-password-xxxx")
    c2, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    r = c2.post("/login", json={"password": PW})
    assert r.status_code == 401, "非表单编码提交必须按口令错误处理，不得 400/500/放行"
    assert r.get_data() == wrong.get_data()
    assert not r.headers.getlist("Set-Cookie")


def test_门未开启时GET_login是303到根(make_client):
    c, _ = make_client()
    r = c.get("/login", headers=HTML)
    assert r.status_code == 303
    assert r.headers.get("Location", "").endswith("/")


def test_门未开启时POST_login是303到根(make_client):
    """§1.5 [R4D] I1-2：门关时登录无意义，不得 401/500。"""
    c, _ = make_client()
    r = login(c, "whatever")
    assert r.status_code == 303
    assert r.headers.get("Location", "").endswith("/")


def test_未设admin口令时登录成功跳hub(make_client):
    """§1.5 表首行：admin 未设、password 对 → 303 Location: /hub。"""
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    r = login(c, PW)
    assert r.status_code == 303
    assert r.headers.get("Location", "").endswith("/hub")


def test_连续失败会逐次变慢_延迟随窗口增长(make_client):
    """§1.5：每次失败响应前 sleep(min(0.5*n,5))。第 3 次失败应明显慢于第 1 次。"""
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    t0 = time.time(); login(c, "bad-password-0001"); first = time.time() - t0
    login(c, "bad-password-0002")
    t0 = time.time(); login(c, "bad-password-0003"); third = time.time() - t0
    assert third >= 1.0, "第 3 次失败未按 0.5*n 延迟（n=3 应约 1.5s），爆破成本为零"
    assert third > first


def test_成功登录清空失败窗口(make_client):
    """§1.5：成功登录清空该窗口 → 之后再失败一次，延迟应回到最小档。"""
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    for i in range(3):
        login(c, "bad-password-%04d" % i)
    assert login(c, PW).status_code == 303
    t0 = time.time(); login(c, "bad-password-9999"); after = time.time() - t0
    assert after < 1.0, "成功登录后窗口未清空（下一次失败仍按老 n 延迟）"


@pytest.mark.slow
@pytest.mark.skipif(not __import__("os").environ.get("HUB_TEST_BRUTEFORCE"),
                    reason="全量爆破用例含约 70 秒累计 sleep，设 HUB_TEST_BRUTEFORCE=1 才跑")
def test_失败20次后所有登录请求直接429带RetryAfter(make_client):
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    for i in range(20):
        login(c, "bad-password-%04d" % i)
    r = login(c, PW)          # 正确口令此刻也应被限流挡住（"所有登录请求"）
    assert r.status_code == 429
    assert r.get_json().get("error") == "too_many_attempts"
    assert r.headers.get("Retry-After")
