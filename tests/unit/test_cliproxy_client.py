# -*- coding: utf-8 -*-
"""M2 cliproxy 客户端单测：对 §10 冻结的桩，不联网、不起真 cliproxy、不碰真 hub.env。

桩是 ``tests/acceptance/provider_hub/stub_cliproxy.py`` —— 锁定契约产物，只 import 不改。
凡是走 :func:`load_config` 的用例都必须给 ``HUB_ENV_FILE``，否则会回落读
真实 ``~/.claude-tgbot/hub.env``（红线）。
"""
import json
import sys
from pathlib import Path

import pytest

_STUB_DIR = Path(__file__).resolve().parents[1] / "acceptance" / "provider_hub"
if str(_STUB_DIR) not in sys.path:
    sys.path.insert(0, str(_STUB_DIR))

from stub_cliproxy import StubCliproxy            # noqa: E402

from moments import cliproxy_client as cc         # noqa: E402
from moments.provider_model import HubError, entry_api_key  # noqa: E402

MGMT = "mgmt-key-0000"
DOWN = "sk-test-downstream-0000"
ALIAS = "claude-sonnet-4-5-20250929"
HAIKU = "claude-3-5-haiku-20241022"
RANDOM_KEY = "a3f9c2e1b7d4508a6c1f2e9d7b3a4c5e6f8091a2"   # 40 位无前缀，正则挡不住
NO_ENV_FILE = "/nonexistent/never/hub.env"                # 绝不回落到真实文件


@pytest.fixture
def stub():
    s = StubCliproxy(mgmt_key=MGMT, downstream_key=DOWN)
    s.start()
    try:
        yield s
    finally:
        s.stop()


@pytest.fixture
def client(stub):
    return cc.CliproxyClient(stub.port, MGMT, DOWN)


def writes(stub):
    return [r for r in stub.requests if r["method"] in ("PUT", "PATCH", "DELETE")]


# ---------------- §0.3 配置来源与回落 ----------------

def _env(**kw):
    base = {"CLIPROXY_PORT": "8317", "CLIPROXY_MGMT_KEY": "m", "CLIPROXY_API_KEY": "a",
            "HUB_ENV_FILE": NO_ENV_FILE}
    base.update(kw)
    return {k: v for k, v in base.items() if v is not None}


def test_三键齐全即已配置():
    cfg = cc.load_config(_env())
    assert (cfg["configured"], cfg["port"]) == (True, 8317)


@pytest.mark.parametrize("k", cc.ENV_KEYS)
def test_任一键缺失即未配置(k):
    assert cc.load_config(_env(**{k: None}))["configured"] is False


@pytest.mark.parametrize("bad", ["", " ", "0", "70000", "-1", "abc", "80a"])
def test_端口非法一律当没配(bad):
    assert cc.load_config(_env(CLIPROXY_PORT=bad))["configured"] is False


def test_环境变量缺失时回落读HUB_ENV_FILE(tmp_path):
    f = tmp_path / "手工.env"
    f.write_text('# 注释行忽略\n\n乱七八糟没有等号\n'
                 'CLIPROXY_PORT=8317\nCLIPROXY_MGMT_KEY="mk"\n CLIPROXY_API_KEY = ak \n',
                 encoding="utf-8")
    cfg = cc.load_config({"HUB_ENV_FILE": str(f)})
    assert cfg["configured"] and cfg["mgmt_key"] == "mk" and cfg["api_key"] == "ak"


def test_环境变量优先于文件(tmp_path):
    f = tmp_path / "stale.env"
    f.write_text("CLIPROXY_PORT=1\nCLIPROXY_MGMT_KEY=stale\nCLIPROXY_API_KEY=stale\n",
                 encoding="utf-8")
    cfg = cc.load_config(_env(HUB_ENV_FILE=str(f)))
    assert (cfg["port"], cfg["mgmt_key"]) == (8317, "m")


def test_默认回落位置跟着CLAUDE_TGBOT_HOME走(tmp_path):
    (tmp_path / "hub.env").write_text(
        "CLIPROXY_PORT=8317\nCLIPROXY_MGMT_KEY=h\nCLIPROXY_API_KEY=h\n", encoding="utf-8")
    cfg = cc.load_config({"CLAUDE_TGBOT_HOME": str(tmp_path)})
    assert cfg["configured"] and cfg["mgmt_key"] == "h"


def test_回落文件读不出来不抛错(tmp_path):
    assert cc.load_config({"HUB_ENV_FILE": str(tmp_path)})["configured"] is False


def test_anthropic_compat缺失取真_显式0才假():
    """§12.2 ⑩：运行时探测已删、route 恒 cliproxy，缺失报 false 会自相矛盾。"""
    assert cc.load_config(_env())["anthropic_compat"] is True
    assert cc.load_config(_env(CLIPROXY_ANTHROPIC_COMPAT="0"))["anthropic_compat"] is False
    assert cc.load_config(_env(CLIPROXY_ANTHROPIC_COMPAT="1"))["anthropic_compat"] is True


def test_from_env未配置抛503_cliproxy_unconfigured():
    with pytest.raises(HubError) as ei:
        cc.from_env(_env(CLIPROXY_MGMT_KEY=None))
    assert (ei.value.status, ei.value.error) == (503, "cliproxy_unconfigured")


def test_from_env配齐时造得出客户端(stub):
    c = cc.from_env({"CLIPROXY_PORT": str(stub.port), "CLIPROXY_MGMT_KEY": MGMT,
                     "CLIPROXY_API_KEY": DOWN, "HUB_ENV_FILE": NO_ENV_FILE})
    assert c.healthz() == {"status": "ok"}


# ---------------- §0.4 三种故障分型 ----------------

def test_没跑时每个封装都抛503_cliproxy_down(stub, client):
    stub.stop()
    calls = [lambda: client.healthz(),
             lambda: client.mgmt_get("openai-compatibility"),
             lambda: client.mgmt_put("openai-compatibility", []),
             lambda: client.mgmt_patch("openai-compatibility", {"index": 0, "value": {}}),
             lambda: client.mgmt_post("oauth-callback", {}),
             lambda: client.mgmt_delete("auth-files"),
             lambda: client.entries(),
             lambda: client.providers(),
             lambda: client.add_entry("openai", {}),
             lambda: client.delete_entry("openai", 0),
             lambda: client.set_alias_exclusive(ALIAS, "deadbeef"),
             lambda: client.restore_prefixes([{"kind": "openai", "index": 0, "prefix": ""}]),
             lambda: client.probe_messages(ALIAS)]
    for f in calls:
        with pytest.raises(HubError) as ei:
            f()
        assert (ei.value.status, ei.value.error) == (503, "cliproxy_down")


@pytest.mark.parametrize("status", [400, 401, 404, 429, 500, 503])
def test_管理API非2xx一律502_cliproxy_reject(stub, client, status):
    """§0.4：4xx 与 5xx 一视同仁；detail 必须含上游状态码。"""
    stub.mgmt_status = status
    with pytest.raises(cc.CliproxyError) as ei:
        client.mgmt_get("openai-compatibility")
    assert (ei.value.status, ei.value.error) == (502, "cliproxy_reject")
    assert ei.value.upstream_status == status
    assert str(status) in ei.value.detail


def test_管理密钥不对被桩打成502(stub):
    """§10：桩校验 X-Management-Key —— 门户配错/漏带必须暴露成 cliproxy_reject。"""
    c = cc.CliproxyClient(stub.port, "wrong-mgmt-key", DOWN)
    with pytest.raises(cc.CliproxyError) as ei:
        c.mgmt_get("openai-compatibility")
    assert ei.value.error == "cliproxy_reject" and ei.value.upstream_status == 401


def test_每个管理请求都带X_Management_Key(stub, client):
    client.entries()
    mgmt = [r for r in stub.requests if r["path"].startswith("/v0/management/")]
    assert mgmt and all(r["headers"].get("x-management-key") == MGMT for r in mgmt)


@pytest.mark.parametrize("block", ["anthropic-compatibility", "gemini-compatibility",
                                   "anthropic-api-key"])
def test_v7不存在的块名回404并映射成502(client, block):
    """块名映射错了要炸得出来，不能静默当成"空列表"。"""
    with pytest.raises(cc.CliproxyError) as ei:
        client.mgmt_get(block)
    assert ei.value.upstream_status == 404


def test_异常里既没有上游正文也没有管理密钥(stub, client):
    """M0 交接：traceback 会进日志，异常消息不许携带响应体。"""
    stub.mgmt_status = 500
    with pytest.raises(cc.CliproxyError) as ei:
        client.mgmt_get("openai-compatibility")
    blob = "%r|%s|%s" % (ei.value.args, ei.value, ei.value.detail)
    assert "stub forced management failure" not in blob
    assert MGMT not in blob


def test_不吃http_proxy环境变量(client, monkeypatch):
    """本机挂 Clash 之类透明代理时，走代理会把 127.0.0.1 请求送出去 → 误判成 down。"""
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:9")
    assert client.healthz() == {"status": "ok"}


# ---------------- §0.2 投影 / §3.1 条目读写 ----------------

def test_providers只带安全字段_不带明文key(stub, client):
    blk = stub.seed_openai_block(api_key=RANDOM_KEY)
    blk["secret-key"] = "top-secret-value-9999"
    blk["internal-note"] = "cliproxy 内部字段"
    ps = client.providers()
    text = json.dumps(ps, ensure_ascii=False)
    assert RANDOM_KEY not in text
    assert "top-secret-value-9999" not in text and "内部字段" not in text
    assert len(ps) == 1 and ps[0]["key_masked"] == "****91a2"
    assert (ps[0]["model_alias"], ps[0]["haiku_alias"]) == (ALIAS, HAIKU)
    assert ps[0]["disabled"] is False and ps[0]["route"] == "cliproxy"


def test_raw保留明文key供PATCH保原key用(stub, client):
    """§3.1 允许的单向流出：写路径要拿它原样写回，读路径一律用 view。"""
    stub.seed_openai_block(api_key=RANDOM_KEY)
    e = client.entries()[0]
    assert entry_api_key(e.kind, e.raw) == RANDOM_KEY
    assert RANDOM_KEY not in json.dumps(e.view, ensure_ascii=False)


def test_带prefix的条目读侧派生成disabled(stub, client):
    """§3.0d 读侧取或：prefix 非空 or 条目自己 disabled:true。"""
    blk = stub.seed_openai_block()
    blk["prefix"] = "a1b2c3d4"
    assert client.providers()[0]["disabled"] is True
    blk["prefix"] = ""
    blk["disabled"] = True                      # 用户手改的原生 disabled，只读不写
    assert client.providers()[0]["disabled"] is True


def test_三块都读_块名从KIND_SPEC派生(stub, client):
    stub.seed_openai_block()
    stub.claude_api_key.append({"base-url": "https://a.example", "api-key": "k1",
                                "models": [{"name": "m", "alias": "x"},
                                           {"name": "m", "alias": "y"}]})
    stub.gemini_api_key.append({"base-url": "https://g.example", "api-key": "k2",
                                "models": [{"name": "m", "alias": "p"},
                                           {"name": "m", "alias": "q"}]})
    kinds = sorted(e.kind for e in client.entries())
    assert kinds == ["anthropic", "gemini", "openai"]
    paths = sorted({r["path"] for r in stub.requests if r["method"] == "GET"})
    assert paths == ["/v0/management/claude-api-key", "/v0/management/gemini-api-key",
                     "/v0/management/openai-compatibility"]


def test_新增恰好一次写且落进对应块(stub, client):
    entry = {"base-url": "https://api.example/v1", "api-key": "sk-test-x",
             "models": [{"name": "m", "alias": ALIAS, "force-mapping": True},
                        {"name": "m", "alias": HAIKU, "force-mapping": True}],
             "prefix": ""}
    client.add_entry("anthropic", entry)
    assert len(stub.claude_api_key) == 1
    assert len(writes(stub)) == 1
    assert writes(stub)[0]["path"] == "/v0/management/claude-api-key"
    assert stub.alias_count_violation == []          # §3.0c：models 必须两条


def test_删除只留下其余条目_恰好一次写(stub, client):
    stub.seed_openai_block(name="a", base_url="https://a.example/v1", alias="alias-a")
    stub.seed_openai_block(name="b", base_url="https://b.example/v1", alias="alias-b")
    client.delete_entry("openai", 0)
    assert [b["models"][0]["alias"] for b in stub.openai_compat] == ["alias-b"]
    assert len(writes(stub)) == 1


def test_按id找条目_找不到回None(stub, client):
    stub.seed_openai_block()
    e = client.entries()[0]
    assert client.find(e.view["id"]).index == 0
    assert client.find("deadbeef") is None


# ---------------- §3.0d/§3.6 B alias 互斥 ----------------

def _seed_two(stub):
    stub.seed_openai_block(name="a", base_url="https://a.example/v1", display_name="A")
    stub.seed_openai_block(name="b", base_url="https://b.example/v1", display_name="B")


def _prefix(blk):
    return blk.get("prefix", "")


def test_互斥_落选写自己的id_当选清空(stub, client):
    _seed_two(stub)
    ids = [p["id"] for p in client.providers()]
    stub.openai_compat[1]["prefix"] = ids[1]        # 目标当前是新建即停用态（§3.3）
    recs = client.set_alias_exclusive(ALIAS, ids[1])
    assert _prefix(stub.openai_compat[0]) == ids[0]
    assert _prefix(stub.openai_compat[1]) == ""
    assert [r["prefix"] for r in recs] == ["", ids[1]]          # 记的是旧值
    assert [json.loads(w["body"])["index"] for w in writes(stub)] == [0, 1], "必须先停落选再启当选"
    # 桩铺的 openai 块自带 disabled:False；互斥不许去翻它（另两块根本没有这个键）
    assert stub.openai_compat[0].get("disabled") is False, "写侧只许动 prefix"


def test_互斥不动key与models(stub, client):
    """§3.0d 的非破坏性：id 派生不漂移、明文 key 原样保留。"""
    stub.seed_openai_block(api_key=RANDOM_KEY)
    before = client.providers()[0]
    client.set_alias_exclusive(HAIKU, before["id"])   # 只清自己 → 0 次写
    stub.openai_compat[0]["prefix"] = "zzzz"
    client.set_alias_exclusive(ALIAS, before["id"])
    after = client.entries()[0]
    assert after.view["id"] == before["id"]
    assert entry_api_key("openai", after.raw) == RANDOM_KEY
    assert [m["alias"] for m in after.raw["models"]] == [ALIAS, HAIKU]


def test_互斥幂等_第二次零写(stub, client):
    _seed_two(stub)
    ids = [p["id"] for p in client.providers()]
    stub.openai_compat[1]["prefix"] = ids[1]
    client.set_alias_exclusive(ALIAS, ids[1])
    n = len(writes(stub))
    assert client.set_alias_exclusive(ALIAS, ids[1]) == []
    assert len(writes(stub)) == n


def test_haiku_alias要单独跑一次才被互斥(stub, client):
    """§3.0c：主 alias 各不相同、haiku 全默认 —— 只跑主 alias 的话 haiku 仍是两个 enabled。"""
    stub.seed_openai_block(name="a", base_url="https://a.example/v1", alias="alias-a")
    stub.seed_openai_block(name="b", base_url="https://b.example/v1", alias="alias-b")
    ids = [p["id"] for p in client.providers()]
    client.set_alias_exclusive("alias-b", ids[1])
    assert _prefix(stub.openai_compat[0]) == "", "主 alias 不冲突时不该动别人"
    client.set_alias_exclusive(HAIKU, ids[1])
    assert _prefix(stub.openai_compat[0]) == ids[0]
    assert _prefix(stub.openai_compat[1]) == ""


def test_跨块互斥_openai与claude争同一个alias(stub, client):
    """三块统一走 prefix：alias 抢占不分块（openai 有原生 disabled，另两块没有）。"""
    stub.seed_openai_block()
    stub.claude_api_key.append(
        {"base-url": "https://c.example", "api-key": "k",
         "models": [{"name": "m", "alias": ALIAS, "force-mapping": True},
                    {"name": "m", "alias": HAIKU, "force-mapping": True}]})
    target = [p for p in client.providers() if p["kind"] == "anthropic"][0]
    client.set_alias_exclusive(ALIAS, target["id"])
    assert _prefix(stub.openai_compat[0]) != ""
    assert _prefix(stub.claude_api_key[0]) == ""
    assert "disabled" not in stub.claude_api_key[0]


def test_目标id不存在时404且一次都不写(stub, client):
    _seed_two(stub)
    with pytest.raises(HubError) as ei:
        client.set_alias_exclusive(ALIAS, "deadbeef")
    assert (ei.value.status, ei.value.error) == (404, "not_found")
    assert writes(stub) == []


def test_补偿把prefix原样写回(stub, client):
    _seed_two(stub)
    ids = [p["id"] for p in client.providers()]
    stub.openai_compat[1]["prefix"] = ids[1]
    recs = client.set_alias_exclusive(ALIAS, ids[1])
    client.restore_prefixes(recs)
    assert _prefix(stub.openai_compat[0]) == ""
    assert _prefix(stub.openai_compat[1]) == ids[1]


def test_patch_fail_after_n复现activate_partial时序(stub, client):
    """§10 的三步：B1 成功 / B2 500 / 补偿 500 —— M3 据此回 500 activate_partial。"""
    _seed_two(stub)
    ids = [p["id"] for p in client.providers()]
    stub.openai_compat[1]["prefix"] = ids[1]
    stub.patch_fail_after_n = 1
    with pytest.raises(cc.CliproxyError) as ei:
        client.set_alias_exclusive(ALIAS, ids[1])
    assert ei.value.error == "cliproxy_reject" and ei.value.upstream_status == 500
    undo = ei.value.undo
    assert [(u["kind"], u["index"], u["prefix"]) for u in undo] == [("openai", 0, "")]
    with pytest.raises(cc.CliproxyError):
        client.restore_prefixes(undo)                 # 第 3 次写 → 500 → 补偿失败
    assert _prefix(stub.openai_compat[0]) == ids[0], "此刻正是 alias_disabled 的不一致态"
    stub.patch_fail_after_n = None                    # 重放同一次切换 → 收敛
    client.set_alias_exclusive(ALIAS, ids[1])
    assert (_prefix(stub.openai_compat[0]), _prefix(stub.openai_compat[1])) == (ids[0], "")


def test_写失败时undo只含已成功的部分(stub, client):
    _seed_two(stub)
    ids = [p["id"] for p in client.providers()]
    stub.openai_compat[1]["prefix"] = ids[1]
    stub.patch_fail_after_n = 0                       # 第一次写就 500
    with pytest.raises(cc.CliproxyError) as ei:
        client.set_alias_exclusive(ALIAS, ids[1])
    assert ei.value.undo == [], "B1 就失败 → 无残留、无可补偿"


# ---------------- §3.7 连通性自测 ----------------

def test_自测成功回模型回显与文本头(client):
    r = client.probe_messages(ALIAS)
    assert (r["ok"], r["status"], r["timeout"]) == (True, 200, False)
    assert r["model_echo"] == ALIAS and r["text_head"] == "stub reply ok"
    assert r["latency_ms"] >= 0


def test_自测遇回显请求头的4xx不带出下游key(stub, client):
    """[R4D] I3-3：上游把 Authorization 原样回显时，只许带出 error.type/message。"""
    stub.messages_mode = "http4xx_echo"
    r = client.probe_messages(ALIAS)
    assert (r["ok"], r["status"]) == (False, 401)
    assert r["error_type"] == "authentication_error"
    text = json.dumps(r, ensure_ascii=False)
    assert DOWN not in text and "echoed_request_headers" not in text


def test_自测遇unknown_model给出可判定的message(stub, client):
    """§3.7 stage:"routing" 的判据 —— message 必须原样保留这句话。"""
    stub.messages_mode = "unknown_model"
    r = client.probe_messages(ALIAS)
    assert r["status"] == 502 and "unknown provider for model" in r["error_message"]


def test_自测超时是业务结果不是down(stub, client):
    stub.messages_mode = "timeout"
    stub.timeout_sleep = 2
    r = client.probe_messages(ALIAS, timeout=0.3)
    assert (r["timeout"], r["ok"], r["status"]) == (True, False, None)


def test_自测发的是下游api_key与alias(stub, client):
    client.probe_messages(ALIAS)
    hit = [r for r in stub.requests if r["path"] == "/v1/messages"][-1]
    assert hit["headers"]["authorization"] == "Bearer " + DOWN
    assert json.loads(hit["body"])["model"] == ALIAS


def test_自测遇非JSON正文不炸也不外带(stub, client, monkeypatch):
    monkeypatch.setattr(cc.CliproxyClient, "_request",
                        lambda *a, **k: (200, b"<html>sk-test-leak-9999</html>"))
    r = client.probe_messages(ALIAS)
    assert r["ok"] is True and r["model_echo"] is None
    assert "sk-test-leak-9999" not in json.dumps(r)
