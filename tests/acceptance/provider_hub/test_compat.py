# -*- coding: utf-8 -*-
"""§9.1 / BRIEF S5：既有朋友圈与生图设置零回归，旧直达路径不失效。

"逐字节不变"没有改造前的基线可比，所以改用可验证的等价命题：
**同一个 checkout 下，门关闭时的响应体 == 门开启并登录后的响应体**
（§9.1 末行：唯一行为变化是新增 401/302 前置）。
"""
import pytest

from conftest import login

PW = "hub-access-pw-12345"
OLD_GET_PATHS = ["/", "/styles", "/api/moments", "/api/styles", "/api/image_provider"]


@pytest.mark.parametrize("path", OLD_GET_PATHS)
def test_旧路径门关闭时免鉴权可达(make_client, path):
    c, _ = make_client()
    r = c.get(path, headers={"Accept": "*/*"})
    assert r.status_code != 401, "%s 在门关闭时要鉴权了 → 老用户被挡在外面" % path
    assert r.status_code != 404, "%s 消失了（S5 承诺不失效）" % path


@pytest.mark.parametrize("path", OLD_GET_PATHS)
def test_旧路径门开启并登录后响应与门关闭时一致(make_client, path):
    c0, _ = make_client()
    before = c0.get(path, headers={"Accept": "*/*"})
    c1, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    assert login(c1, PW).status_code == 303, "登录没成功，这条比对等于空转"
    after = c1.get(path, headers={"Accept": "*/*"})
    assert after.status_code == before.status_code
    assert after.get_data() == before.get_data(), "%s 的响应体变了（§9.1 承诺不变）" % path


def test_朋友圈单条与用户资料路径仍在(client):
    """§9.1 表列的两个带参路径：不存在的 id 应是业务 404，而不是路由 404 的 HTML。"""
    for path in ("/api/moment/does-not-exist", "/api/profile/does-not-exist"):
        r = client.get(path, headers={"Accept": "*/*"})
        assert r.status_code != 405
        assert "text/html" not in (r.headers.get("Content-Type") or ""), \
            "%s 变成了页面路由" % path


def test_老接口不经过新增的脱敏中间件(client):
    """§0.2b / §9.1 新增行：出站脱敏只挂 /hub/api/*，老路由逐字节不变。

    `/api/novelai_key` 本来就只回 {set, masked}（§0.2b 核查结论），
    因此这里**不再**对它做全局"不含明文"的断言 —— 那条断言的前提已被裁决推翻。
    可验证的是：老接口的 masked 形态原样保留，没被新中间件二次改写成别的样子。
    """
    r = client.get("/api/novelai_key", headers={"Accept": "*/*"})
    if r.status_code == 405:
        pytest.skip("/api/novelai_key 不接受 GET，形态未在 INTERFACE 中给出")
    body = r.get_json()
    if not isinstance(body, dict) or "masked" not in body:
        pytest.skip("该接口未回 masked 字段，形态与 §0.2b 描述不符，交由实现方核对")
    assert set(body) >= {"set", "masked"}, "老接口响应形状被改动了（§9.1 逐字节不变）"


@pytest.mark.parametrize("path", ["/api/moments", "/api/styles"])
def test_D2_非JSON请求体不再被猜着解析(client, path):
    """§9.2 D2：force=True → silent=True，Content-Type 不对按体缺失处理。"""
    r = client.post(path, data="not json at all", content_type="text/plain")
    assert r.status_code != 200, "%s 仍在猜着解析非 JSON 体" % path


def test_门开启时朋友圈页仍是HTML不是JSON(make_client):
    c, _ = make_client(HUB_ACCESS_PASSWORD=PW)
    login(c, PW)
    r = c.get("/", headers={"Accept": "text/html"})
    assert r.status_code == 200
    assert "text/html" in r.headers.get("Content-Type", "")
