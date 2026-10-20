# -*- coding: utf-8 -*-
"""§0.3 修订 3：cliproxy「压根没配」这一形态（区别于「装了没跑」）。

场景很真实：用户刚 git clone 就打开门户，还没跑 install。
铁律是页面与两个列表端点仍 200，写操作才 503 `cliproxy_unconfigured`。
"""
import pytest

from conftest import _Api, provider_payload

MISSING = {"CLIPROXY_PORT": None, "CLIPROXY_MGMT_KEY": None, "CLIPROXY_API_KEY": None}


def _unconfigured(make_client):
    return _Api(make_client(**MISSING)[0])


@pytest.mark.parametrize("path", ["/hub", "/hub/provider", "/hub/bots"])
def test_没配时三个页面仍200(make_client, path):
    c, _ = make_client(**MISSING)
    r = c.get(path, headers={"Accept": "text/html"})
    assert r.status_code == 200, "%s 在未配置时返回 %s（§2 铁律）" % (path, r.status_code)


def test_没配时provider列表200且给unconfigured警告(make_client):
    code, body = _unconfigured(make_client).get("/hub/api/provider")
    assert code == 200
    assert body["cliproxy"]["running"] is False
    assert body["providers"] == []
    assert "cliproxy_unconfigured" in body["warnings"]


def test_没配时supported_kinds照样返回(make_client):
    """§3.2：它是本地静态知识，没配也得给，否则页面下拉是空的。"""
    code, body = _unconfigured(make_client).get("/hub/api/provider")
    kinds = body["cliproxy"]["supported_kinds"]
    assert "openai" in kinds and "anthropic" in kinds


def test_没配时oauth账户列表200且给unconfigured警告(make_client):
    code, body = _unconfigured(make_client).get("/hub/api/oauth/accounts")
    assert code == 200
    assert body["accounts"] == []
    assert "cliproxy_unconfigured" in body["warnings"]


@pytest.mark.parametrize("missing", ["CLIPROXY_PORT", "CLIPROXY_MGMT_KEY", "CLIPROXY_API_KEY"])
def test_三个键任一缺失都算没配(make_client, missing):
    """§0.3：任一缺失即 unconfigured，不许"能连上就凑合跑"。"""
    a = _Api(make_client(**{missing: None})[0])
    code, body = a.post("/hub/api/provider", provider_payload())
    assert code == 503, "缺 %s 时实得 %s" % (missing, code)
    assert body["error"] == "cliproxy_unconfigured"


def test_没配与没跑是两个不同的码(make_client, stub):
    """§0.4：503 都是"依赖不可用"，但没配和没跑要能分开，否则用户不知道该跑 install 还是查进程。"""
    a1 = _unconfigured(make_client)
    c1, b1 = a1.post("/hub/api/provider", provider_payload())
    a2 = _Api(make_client()[0])
    stub.set_down(True)
    c2, b2 = a2.post("/hub/api/provider", provider_payload())
    assert (c1, c2) == (503, 503)
    assert b1["error"] == "cliproxy_unconfigured"
    assert b2["error"] == "cliproxy_down"


def test_没配时切回Claude官方订阅不受影响(make_client, settings_path):
    """§0.3：它不碰 cliproxy —— 这正是没配好时唯一还能用的逃生出口。"""
    code, body = _unconfigured(make_client).post("/hub/api/claude-native/activate")
    assert code == 200, body
    assert body["active_kind"] == "claude_native"


def test_没配时bot接口不受影响(make_client):
    code, body = _unconfigured(make_client).get("/hub/api/bots")
    assert code == 200, body
    assert isinstance(body["bots"], list)


def test_没配时写与自测端点全线503(make_client):
    """§0.3 表：其余 provider/oauth 的写与自测端点一律 cliproxy_unconfigured。"""
    a = _unconfigured(make_client)
    calls = [
        a.post("/hub/api/provider", provider_payload()),
        a.patch("/hub/api/provider/deadbeef", {"label": "x"}),
        a.delete("/hub/api/provider/deadbeef"),
        a.post("/hub/api/provider/deadbeef/activate"),
        a.post("/hub/api/provider/deadbeef/test"),
        a.post("/hub/api/oauth/start", {"provider": "codex"}),
        a.post("/hub/api/oauth/submit", {"provider": "codex",
                                         "redirect_url": "http://localhost:1455/cb?code=x"}),
        a.delete("/hub/api/oauth/accounts/codex/deadbeef"),
        a.post("/hub/api/oauth/accounts/codex/deadbeef/activate"),
    ]
    bad = [(c, b) for c, b in calls
           if c != 503 or b.get("error") != "cliproxy_unconfigured"]
    assert bad == [], "这些端点没按 §0.3 回 503 cliproxy_unconfigured：%s" % bad


# ---------------- §0.3 回落：环境变量没有时直读 hub.env ----------------

def test_环境变量缺失时回落读HUB_ENV_FILE(make_client, stub, tmp_path):
    """§0.3：为"用户手敲 python3 -m moments.web"存在的一条回落。"""
    from conftest import FAKE_MGMT_KEY, FAKE_DOWNSTREAM_KEY
    env_file = tmp_path / "手工.env"
    env_file.write_text(
        "# 注释行要被忽略\n\n"
        "CLIPROXY_PORT=%d\nCLIPROXY_MGMT_KEY=%s\nCLIPROXY_API_KEY=%s\n"
        "CLIPROXY_ANTHROPIC_COMPAT=1\n" % (stub.port, FAKE_MGMT_KEY, FAKE_DOWNSTREAM_KEY),
        encoding="utf-8")
    a = _Api(make_client(HUB_ENV_FILE=str(env_file), **MISSING)[0])
    code, body = a.get("/hub/api/provider")
    assert code == 200
    assert body["cliproxy"]["running"] is True, "没回落读 hub.env：%s" % body.get("warnings")
    assert body["cliproxy"]["port"] == stub.port
    code, body = a.post("/hub/api/provider", provider_payload())
    assert code == 201, "回落读到的管理密钥没生效：%s" % body


def test_环境变量优先于文件(make_client, stub, tmp_path):
    """§0.3：唯一来源是环境变量，文件只是回落 —— 冲突时环境变量赢。"""
    env_file = tmp_path / "stale.env"
    env_file.write_text("CLIPROXY_PORT=1\nCLIPROXY_MGMT_KEY=stale\nCLIPROXY_API_KEY=stale\n",
                        encoding="utf-8")
    a = _Api(make_client(HUB_ENV_FILE=str(env_file))[0])
    code, body = a.get("/hub/api/provider")
    assert body["cliproxy"]["port"] == stub.port, "过期的 hub.env 盖住了环境变量"
