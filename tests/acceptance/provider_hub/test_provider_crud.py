# -*- coding: utf-8 -*-
"""§3.2 列表 / §3.3 新增 / §3.4 改 / §3.5 删。错误契约逐行对表。"""
import pytest

from conftest import FAKE_KEY, provider_payload, write_settings, read_settings

ALIAS = "claude-sonnet-4-5-20250929"


def _create(api, **over):
    code, body = api.post("/hub/api/provider", provider_payload(**over))
    return code, body


def test_新增第三方provider成功返回201与完整对象(api):
    """BRIEF S1 第一步：填一个 DeepSeek(OpenAI 格式) key。"""
    code, body = _create(api)
    assert code == 201, body
    assert body["label"] == "DeepSeek"
    assert body["kind"] == "openai"
    assert body["base_url"] == "https://api.deepseek.com/v1"
    assert body["upstream_model"] == "deepseek-v4-flash"   # ⑰：deepseek-chat 已下线
    assert body["model_alias"] == ALIAS
    assert body["route"] == "cliproxy"
    assert body["active"] is False
    assert len(body["id"]) == 8


def test_新增响应不含api_key字段且key已脱敏(api):
    """§3.1：没有 api_key 字段；明文只在请求体里出现一次。"""
    code, body = _create(api)
    assert code == 201
    assert "api_key" not in body, "响应体里出现了 api_key 字段"
    assert body["key_masked"].startswith("****")
    assert FAKE_KEY not in str(body)


def test_新增provider不写settings_json(api, settings_path):
    """反向用例：只有 activate 才写 settings.json（§3.6 是"唯一写 settings 的端点之一"）。"""
    assert not settings_path.exists()
    assert _create(api)[0] == 201
    assert not settings_path.exists(), "新增 provider 就动了 settings.json → 会触发 worker 重启"


def test_新增后列表能查到它(api):
    code, created = _create(api)
    assert code == 201
    code, body = api.get("/hub/api/provider")
    assert code == 200
    ids = [p["id"] for p in body["providers"]]
    assert created["id"] in ids


@pytest.mark.parametrize("bad_kind", ["anthropic_native", "openai_compatible", "", "OPENAI",
                                      "gemini_pro", None, 123])
def test_非法kind返回400_bad_kind(api, bad_kind):
    """§3.3：kind 不在 KIND_SPEC 里（S0-B 前只有 openai）。"""
    code, body = _create(api, kind=bad_kind)
    assert code == 400, "kind=%r 被接受了" % bad_kind
    assert body["error"] == "bad_kind"


@pytest.mark.parametrize("label,err", [("", "bad_label"), ("x" * 41, "bad_label"),
                                       ("中" * 41, "bad_label")])
def test_label边界返回400_bad_label(api, label, err):
    code, body = _create(api, label=label)
    assert code == 400
    assert body["error"] == err


@pytest.mark.parametrize("label", ["x" * 40, "中" * 40, "DeepSeek 深度求索 🐋"])
def test_label边界内合法_含Unicode与emoji(api, label):
    """40 字符是上界不是下界；中文/emoji 按字符数算，不按字节。"""
    code, body = _create(api, label=label)
    assert code == 201, body
    assert body["label"] == label


@pytest.mark.parametrize("key", ["", None, "   "])
def test_api_key为空返回400_key_required(api, key):
    code, body = _create(api, api_key=key)
    assert code == 400
    assert body["error"] == "key_required"


@pytest.mark.parametrize("url", ["api.deepseek.com/v1", "ftp://x.example.com/",
                                 "", "javascript:alert(1)", "/v1", "http://",
                                 "file:///etc/passwd"])
def test_base_url必须是绝对http_url_否则400(api, url):
    code, body = _create(api, base_url=url)
    assert code == 400, "base_url=%r 被接受了" % url
    assert body["error"] in ("bad_base_url", "bad_base_url_private")


@pytest.mark.parametrize("url", ["http://127.0.0.1:8317/v1", "http://192.168.1.1/",
                                 "http://169.254.169.254/", "http://localhost:17801/v1",
                                 "http://10.0.0.5/v1", "http://[::1]:8317/v1"])
def test_SSRF_内网与回环base_url必须400(api, url):
    """§3.3 [R4D] I3：门户公网可达，不拦就等于送一个内网探测器。"""
    code, body = _create(api, base_url=url)
    assert code == 400, "SSRF 未拦住：%s" % url
    assert body["error"] == "bad_base_url_private"


@pytest.mark.parametrize("url", ["http://127.0.0.1:8317/v1", "http://192.168.1.1/"])
def test_SSRF逃生门开启后同样输入应201(make_client, url):
    """§3.3 逃生门：HUB_ALLOW_PRIVATE_BASE_URL=1 给真在内网自建网关的用户。"""
    from conftest import _Api
    c, _ = make_client(HUB_ALLOW_PRIVATE_BASE_URL="1")
    code, body = _Api(c).post("/hub/api/provider", provider_payload(base_url=url))
    assert code == 201, body


@pytest.mark.parametrize("alias", ["", "a/b", "has space", "\t", "claude sonnet",
                                   "org/claude-sonnet"])
def test_model_alias非法返回400_bad_alias(api, alias):
    code, body = _create(api, model_alias=alias)
    assert code == 400, "alias=%r 被接受了" % alias
    assert body["error"] == "bad_alias"


@pytest.mark.parametrize("m", ["", None, "  "])
def test_upstream_model为空返回400(api, m):
    code, body = _create(api, upstream_model=m)
    assert code == 400
    assert body["error"] == "bad_upstream_model"


def test_请求体不是JSON对象返回400_bad_body(client):
    """§0.1：禁用 force=True，Content-Type 不对按体缺失处理。"""
    r = client.post("/hub/api/provider", data="label=x",
                    content_type="application/x-www-form-urlencoded")
    assert r.status_code == 400
    assert r.get_json()["error"] == "bad_body"


@pytest.mark.parametrize("raw", ["[]", '"just a string"', "null", "123", ""])
def test_JSON体不是对象返回400_bad_body(client, raw):
    r = client.post("/hub/api/provider", data=raw, content_type="application/json")
    assert r.status_code == 400
    assert r.get_json()["error"] == "bad_body"


def test_校验顺序_kind先于label被命中(api):
    """§3.3「先命中先返回」：kind 非法 + label 也非法 → 必须回 bad_kind。"""
    code, body = _create(api, kind="nope", label="")
    assert code == 400
    assert body["error"] == "bad_kind", "校验顺序与 §3.3 表不一致"


def test_四元组完全相同返回409_duplicate_provider(api):
    assert _create(api)[0] == 201
    code, body = _create(api, label="换个名字也不行")
    assert code == 409
    assert body["error"] == "duplicate_provider"


def test_同alias不同上游是合法的_返回201且新建即disabled(api):
    """[RA] F2 裁决：唯一性只在运行时层保证；S1/S2 的来回切正需要多个来源争同一 alias。"""
    assert _create(api)[0] == 201
    code, body = _create(api, label="Gemini 兼容", base_url="https://gw.example.com/v1",
                         upstream_model="gemini-2.5-pro")
    assert code == 201, body
    assert body["disabled"] is True, "alias 已被 enabled 条目占用时，新建应即置 disabled"


def test_cliproxy不可达时新增返回503(api, stub):
    stub.set_down(True)
    code, body = _create(api)
    assert code == 503
    assert body["error"] == "cliproxy_down"


def test_cliproxy管理API非2xx时新增返回502(api, stub):
    stub.mgmt_status = 500
    code, body = _create(api)
    assert code == 502
    assert body["error"] == "cliproxy_reject"


def test_门户漏带管理密钥会被桩打成502(make_client, stub):
    """§10：桩校验 X-Management-Key —— 门户配错/漏带密钥必须暴露成 cliproxy_reject。"""
    from conftest import _Api
    c, _ = make_client(CLIPROXY_MGMT_KEY="wrong-mgmt-key")
    code, body = _Api(c).post("/hub/api/provider", provider_payload())
    assert code == 502
    assert body["error"] == "cliproxy_reject"


def _env(**kw):
    return {"env": dict(kw)}


def test_列表_settings四键全缺时active是claude_native(api, settings_path):
    write_settings(settings_path, {"model": "sonnet"})
    code, body = api.get("/hub/api/provider")
    assert code == 200
    assert body["active"]["kind"] == "claude_native"
    assert body["active"]["id"] is None


def test_列表_settings不可解析时active是unknown(api, settings_path):
    settings_path.write_text("{ 坏掉的 json", encoding="utf-8")
    code, body = api.get("/hub/api/provider")
    assert code == 200, "settings 坏了不该让列表接口挂掉"
    assert body["active"]["kind"] == "unknown"
    assert "settings_unparsable" in body.get("warnings", [])


def test_列表_用户手写的第三方直连算none(api, settings_path, stub):
    write_settings(settings_path, _env(ANTHROPIC_BASE_URL="https://other.example.com",
                                       ANTHROPIC_MODEL="whatever-model"))
    code, body = api.get("/hub/api/provider")
    assert body["active"]["kind"] == "none"
    assert body["active"]["id"] is None


def test_列表_alias在cliproxy里被disabled时给alias_disabled警告(api, settings_path, stub):
    stub.seed_openai_block(disabled=True)
    write_settings(settings_path, _env(
        ANTHROPIC_BASE_URL="http://127.0.0.1:%d" % stub.port, ANTHROPIC_MODEL=ALIAS))
    code, body = api.get("/hub/api/provider")
    assert body["active"]["kind"] == "cliproxy"
    assert body["active"]["id"] is not None
    assert "alias_disabled" in body.get("warnings", [])


def test_列表_alias在cliproxy里根本不存在时给active_unknown(api, settings_path, stub):
    write_settings(settings_path, _env(
        ANTHROPIC_BASE_URL="http://127.0.0.1:%d" % stub.port,
        ANTHROPIC_MODEL="alias-that-never-existed"))
    code, body = api.get("/hub/api/provider")
    assert body["active"]["kind"] == "cliproxy"
    assert body["active"]["id"] is None
    assert "active_unknown" in body.get("warnings", [])


def test_列表_正常生效时active带上id与alias(api, settings_path, stub):
    stub.seed_openai_block()
    write_settings(settings_path, _env(
        ANTHROPIC_BASE_URL="http://127.0.0.1:%d" % stub.port, ANTHROPIC_MODEL=ALIAS))
    code, body = api.get("/hub/api/provider")
    assert body["active"]["kind"] == "cliproxy"
    assert body["active"]["alias"] == ALIAS
    assert body["active"]["id"]
    assert body.get("warnings", []) == []
    hit = [p for p in body["providers"] if p["id"] == body["active"]["id"]]
    assert hit and hit[0]["active"] is True


def test_列表_cliproxy未运行时仍200但列表为空(api, stub):
    """§3.2：这是删掉 hub.json 台账的已知代价，写进了 §9.3。"""
    stub.set_down(True)
    code, body = api.get("/hub/api/provider")
    assert code == 200
    assert body["cliproxy"]["running"] is False
    assert body["providers"] == []
    assert "cliproxy_down" in body.get("warnings", [])


def test_列表_cliproxy运行时报告端口与版本(api, stub):
    code, body = api.get("/hub/api/provider")
    assert body["cliproxy"]["running"] is True
    assert body["cliproxy"]["port"] == stub.port


# ---------------- §3.4 PATCH ----------------

def test_改base_url会换id_旧id随即404(api):
    code, created = _create(api)
    old = created["id"]
    code, body = api.patch("/hub/api/provider/%s" % old,
                           {"base_url": "https://api2.deepseek.com/v1"})
    assert code == 200, body
    assert body["id"] != old, "§3.1：id 是四元组派生的，改 base_url 必须换 id"
    assert body["base_url"] == "https://api2.deepseek.com/v1"
    code2, _ = api.patch("/hub/api/provider/%s" % old, {"label": "x"})
    assert code2 == 404, "旧 id 仍然可用 → id 不是派生的"


def test_只改label不换id(api):
    code, created = _create(api)
    code, body = api.patch("/hub/api/provider/%s" % created["id"], {"label": "深度求索"})
    assert code == 200
    assert body["id"] == created["id"]
    assert body["label"] == "深度求索"


def test_省略api_key时原key保持不变(api, stub):
    code, created = _create(api)
    before = [e.get("api-key") for b in stub.openai_compat
              for e in (b.get("api-key-entries") or [])]
    api.patch("/hub/api/provider/%s" % created["id"], {"label": "改个名"})
    after = [e.get("api-key") for b in stub.openai_compat
             for e in (b.get("api-key-entries") or [])]
    assert after == before and before, "省略 api_key 应保持原 key，实际被改写/清空"


def test_api_key传空串返回400_key_required(api):
    code, created = _create(api)
    code, body = api.patch("/hub/api/provider/%s" % created["id"], {"api_key": ""})
    assert code == 400
    assert body["error"] == "key_required"


def test_改不存在的id返回404(api):
    code, body = api.patch("/hub/api/provider/deadbeef", {"label": "x"})
    assert code == 404
    assert body["error"] == "not_found"


def test_改动当前生效的provider会自动重新切换(api, settings_path):
    """§3.4 末条：被改的 provider 当前 active → 改完自动执行一次 activate（用新 id）。"""
    code, created = _create(api)
    assert api.post("/hub/api/provider/%s/activate" % created["id"])[0] == 200
    code, body = api.patch("/hub/api/provider/%s" % created["id"],
                           {"model_alias": "claude-opus-4-1-20250805"})
    assert code == 200, body
    env = (read_settings(settings_path) or {}).get("env", {})
    assert env.get("ANTHROPIC_MODEL") == "claude-opus-4-1-20250805", \
        "改了生效中的 provider 但 settings.json 没跟着走 → bot 会打到不存在的 alias"


def test_PATCH的校验与新增一致(api):
    code, created = _create(api)
    code, body = api.patch("/hub/api/provider/%s" % created["id"],
                           {"base_url": "http://127.0.0.1:8317/v1"})
    assert code == 400
    assert body["error"] == "bad_base_url_private"


# ---------------- §3.5 DELETE ----------------

def test_删除成功返回ok与id(api):
    code, created = _create(api)
    code, body = api.delete("/hub/api/provider/%s" % created["id"])
    assert code == 200
    assert body == {"ok": True, "id": created["id"]}
    assert created["id"] not in [p["id"] for p in api.get("/hub/api/provider")[1]["providers"]]


def test_删除不存在的id返回404(api):
    code, body = api.delete("/hub/api/provider/deadbeef")
    assert code == 404
    assert body["error"] == "not_found"


def test_删除当前生效的provider返回409(api):
    code, created = _create(api)
    assert api.post("/hub/api/provider/%s/activate" % created["id"])[0] == 200
    code, body = api.delete("/hub/api/provider/%s" % created["id"])
    assert code == 409
    assert body["error"] == "delete_active"


def test_删除时cliproxy不可达返回503(api, stub):
    code, created = _create(api)
    stub.set_down(True)
    code, body = api.delete("/hub/api/provider/%s" % created["id"])
    assert code == 503
    assert body["error"] == "cliproxy_down"


def test_删除时cliproxy管理API报错返回502(api, stub):
    code, created = _create(api)
    stub.mgmt_status = 503
    code, body = api.delete("/hub/api/provider/%s" % created["id"])
    assert code == 502
    assert body["error"] == "cliproxy_reject"


def test_删除不动settings_json(api, settings_path):
    """反向用例：删除只动 cliproxy 一处（§3.5），不该顺手改 settings。"""
    write_settings(settings_path, {"model": "sonnet", "env": {"KEEP": "1"}})
    code, created = _create(api)
    before = settings_path.read_text(encoding="utf-8")
    assert api.delete("/hub/api/provider/%s" % created["id"])[0] == 200
    assert settings_path.read_text(encoding="utf-8") == before


# ---------------- §3.0/§3.2 修订 3：supported_kinds 与 anthropic_compat ----------------

def test_supported_kinds_必含openai与anthropic(api):
    """§3.0 修订 3：kind 合法性只看接口自报的 supported_kinds，与 S0-B 无关。"""
    code, body = api.get("/hub/api/provider")
    assert code == 200, body
    kinds = body["cliproxy"]["supported_kinds"]
    assert "openai" in kinds and "anthropic" in kinds, "两者必在其中，实得 %s" % kinds


def test_gemini两条分支都是确定断言(api):
    """gemini 在不在由 S0-B 决定，但两种情况的期望都是确定的，不需要跳过。"""
    kinds = api.get("/hub/api/provider")[1]["cliproxy"]["supported_kinds"]
    code, body = _create(api, kind="gemini", label="Gemini 兼容",
                         base_url="https://gw.example.com/v1", upstream_model="gemini-2.5-pro")
    if "gemini" in kinds:
        assert code == 201, "gemini 在 supported_kinds 里却建不出来：%s" % body
        assert body["kind"] == "gemini"
    else:
        assert code == 400 and body["error"] == "bad_kind"


def test_anthropic恒可建_兼容为真时route是cliproxy(make_client):
    from conftest import _Api
    a = _Api(make_client(CLIPROXY_ANTHROPIC_COMPAT="1")[0])
    code, body = a.post("/hub/api/provider", provider_payload(
        kind="anthropic", base_url="https://gw.example.com", upstream_model="glm-4.6"))
    assert code == 201, body
    assert body["route"] == "cliproxy"


def test_route恒为cliproxy_direct分支当前不可达(api):
    """⑩ 修订 4：install 直接写 CLIPROXY_ANTHROPIC_COMPAT=1，探测已删。

    `route` 与 direct 分支作为回归接缝保留，但任何 kind 建出来都必须是 cliproxy。
    """
    for kind in ("openai", "anthropic"):
        code, body = _create(api, kind=kind, label="网关 %s" % kind,
                             base_url="https://gw-%s.example.com/v1" % kind,
                             upstream_model="m-%s" % kind)
        assert code == 201, body
        assert body["route"] == "cliproxy", "kind=%s 的 route 不该是 %s" % (kind, body["route"])


def test_anthropic_compat恒为真(api):
    code, body = api.get("/hub/api/provider")
    assert body["cliproxy"]["anthropic_compat"] is True, \
        "S0-B 已通过，该标志恒为 1（探测逻辑已删）"


def test_supported_kinds在cliproxy没跑时仍如实返回(api, stub):
    """§3.2：它是本地静态知识，不查 cliproxy。"""
    stub.set_down(True)
    code, body = api.get("/hub/api/provider")
    assert code == 200
    kinds = body["cliproxy"]["supported_kinds"]
    assert "openai" in kinds and "anthropic" in kinds


# ---------------- §0.4 修订 3：管理 API 非 2xx 在列表端点上恒 200 ----------------

@pytest.mark.parametrize("status", [401, 500])
def test_列表_跑着但管理API非2xx时200并给cliproxy_error(api, stub, status):
    """§3.2 第四态：running=true + providers[] + cliproxy_error + error_status。"""
    stub.mgmt_status = status
    code, body = api.get("/hub/api/provider")
    assert code == 200, "列表端点必须恒 200（§2 铁律），实得 %s" % code
    assert body["cliproxy"]["running"] is True, "拿到了 HTTP 响应就说明它在跑"
    assert body["providers"] == []
    assert "cliproxy_error" in body["warnings"]
    assert body["cliproxy"]["error_status"] == status


def test_列表_真的没有provider与读不出来可区分(api, stub):
    """§3.2：running=true + [] + 无 cliproxy_error = 真的一个都没有。"""
    code, body = api.get("/hub/api/provider")
    assert code == 200
    assert body["cliproxy"]["running"] is True
    assert body["providers"] == []
    assert "cliproxy_error" not in body.get("warnings", [])


@pytest.mark.parametrize("status", [401, 500])
def test_写端点遇管理API_4xx与5xx一视同仁都是502(api, stub, status):
    """§0.4：502 = 依赖回话了但拒绝我们，4xx 与 5xx 不区分。"""
    stub.mgmt_status = status
    code, body = _create(api)
    assert code == 502
    assert body["error"] == "cliproxy_reject"
    assert str(status) in str(body.get("detail", "")), "detail 必须含上游状态码"
    assert stub.mgmt_key not in str(body)


# ---------------- §3.0c（修订 4）：每个 provider 必须注册两条 alias ----------------

DEFAULT_HAIKU = "claude-3-5-haiku-20241022"


def _entry(stub, alias=ALIAS):
    """从三个承载块里找出含该主 alias 的那一条。"""
    hit = [b for b in stub.all_blocks
           if any(m.get("alias") == alias for m in (b.get("models") or []))]
    assert hit, "cliproxy 里没有主 alias 为 %s 的条目：%s" % (alias, stub.all_blocks)
    return hit[0]


def test_不传haiku_alias时服务端填默认值(api):
    """§3.0c：省略合法，默认 claude-3-5-haiku-20241022。"""
    code, body = _create(api)
    assert code == 201, body
    assert body["haiku_alias"] == DEFAULT_HAIKU


def test_落地的models必须是两条且name相同(api, stub):
    """§3.0c 必须存在的用例：长度为 2、两条 name 相同、alias 分别是主名与 haiku 名。

    少注册一条 = claude CLI 的后台摘要请求打到别的后端或直接 502。
    """
    code, body = _create(api)
    assert code == 201, body
    models = _entry(stub)["models"]
    assert len(models) == 2, "models 长度是 %d，haiku 请求会 502（§3.0c）" % len(models)
    assert models[0]["name"] == models[1]["name"] == "deepseek-v4-flash"
    assert {m["alias"] for m in models} == {ALIAS, DEFAULT_HAIKU}
    assert all(m.get("force-mapping") is True for m in models), "两条都要 force-mapping"


def test_桩没记到alias数违规(api, stub):
    """§10 修订 4 的记账开关：写进 cliproxy 的每个条目 models 都必须恰好 2 条。"""
    assert _create(api)[0] == 201
    assert stub.alias_count_violation == [], \
        "桩记到了 models 长度不等于 2 的写：%s" % stub.alias_count_violation


def test_自定义haiku_alias被如实采用(api, stub):
    code, body = _create(api, haiku_alias="claude-3-5-haiku-latest")
    assert code == 201, body
    assert body["haiku_alias"] == "claude-3-5-haiku-latest"
    assert {m["alias"] for m in _entry(stub)["models"]} == {ALIAS, "claude-3-5-haiku-latest"}


@pytest.mark.parametrize("bad", ["", "a/b", "has space", "\t", "   "])
def test_haiku_alias非法返回400_bad_haiku_alias(api, bad):
    """§3.3：传了但不合法 → 400（校验规则与 model_alias 完全相同）。"""
    code, body = _create(api, haiku_alias=bad)
    assert code == 400, "haiku_alias=%r 被接受了" % bad
    assert body["error"] == "bad_haiku_alias"


def test_haiku_alias等于主alias返回400(api):
    """§3.0c：同一条目里两条 model 撞名，cliproxy 里会是两条一模一样的映射。"""
    code, body = _create(api, haiku_alias=ALIAS)
    assert code == 400
    assert body["error"] == "bad_haiku_alias"


def test_改haiku_alias不换id(api):
    """§3.0c：id 派生四元组不含 haiku_alias。"""
    code, created = _create(api)
    code, body = api.patch("/hub/api/provider/%s" % created["id"],
                           {"haiku_alias": "claude-3-5-haiku-latest"})
    assert code == 200, body
    assert body["id"] == created["id"], "改 haiku 名不该换 id"
    assert body["haiku_alias"] == "claude-3-5-haiku-latest"


def test_haiku_alias与别的provider重复是合法的(api):
    """默认值人人相同，若对 haiku 也做全局唯一校验，第二个 provider 就永远建不出来。"""
    assert _create(api)[0] == 201
    code, body = _create(api, label="第二个", base_url="https://gw.example.com/v1",
                         upstream_model="glm-4.6")
    assert code == 201, body
    assert body["haiku_alias"] == DEFAULT_HAIKU


# ---------------- ⑨（修订 4）：三个 kind 的 v7 承载块 ----------------

def test_openai条目落在openai_compatibility块(api, stub):
    assert _create(api)[0] == 201
    assert len(stub.openai_compat) == 1
    assert stub.claude_api_key == [] and stub.gemini_api_key == []


def test_anthropic条目落在claude_api_key块而不是anthropic_compatibility(api, stub):
    """§10：`anthropic-compatibility` 在 v7 不存在，桩对它回 404。

    门户若映射到错块名，这条会以 502 cliproxy_reject 失败 —— 这正是要测出来的 bug。
    """
    code, body = _create(api, kind="anthropic", base_url="https://gw.example.com",
                         upstream_model="glm-4.6")
    assert code == 201, "映射到了不存在的块名？实得 %s %s" % (code, body)
    assert len(stub.claude_api_key) == 1
    assert stub.openai_compat == [] and stub.gemini_api_key == []


def test_gemini条目落在gemini_api_key块(api, stub):
    kinds = api.get("/hub/api/provider")[1]["cliproxy"]["supported_kinds"]
    code, body = _create(api, kind="gemini", label="Gemini 兼容",
                         base_url="https://gw.example.com/v1", upstream_model="gemini-2.5-pro")
    if "gemini" not in kinds:
        assert code == 400 and body["error"] == "bad_kind"
        return
    assert code == 201, body
    assert len(stub.gemini_api_key) == 1
    assert stub.openai_compat == [] and stub.claude_api_key == []


def test_门户不得访问v7里不存在的块名(api, stub):
    """反向用例：三个已知不存在的路径，一次都不该被打到。"""
    assert _create(api)[0] == 201
    assert _create(api, kind="anthropic", base_url="https://gw.example.com",
                   upstream_model="glm-4.6")[0] == 201, "前置条件不成立，断言会空转"
    assert api.get("/hub/api/provider")[0] == 200
    bad = [r["path"] for r in stub.requests
           if any(x in r["path"] for x in ("anthropic-compatibility", "gemini-compatibility",
                                           "anthropic-api-key"))]
    assert bad == [], "门户打了 v7 上不存在的管理端点：%s" % bad


# ---------------- §3.3 / §12.6（修订 8）：SSRF 用例 T4–T7，fake-IP 例外 ----------------

def _fake_resolver(monkeypatch, host, ips):
    """只替换指定域名的解析结果，其余照旧 —— 不能整体劫持，否则连桩都连不上。"""
    import socket
    real_gai, real_ghn = socket.getaddrinfo, socket.gethostbyname

    def gai(h, *a, **kw):
        if h == host:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]
        return real_gai(h, *a, **kw)

    def ghn(h):
        return ips[0] if h == host else real_ghn(h)

    monkeypatch.setattr(socket, "getaddrinfo", gai)
    monkeypatch.setattr(socket, "gethostbyname", ghn)


def test_T4_fakeIP段的字面量仍必须拦(api):
    """§12.6：例外只能开在第 2 层（DNS），第 1 层的字面量拦截一个都不许放。"""
    code, body = _create(api, base_url="http://198.18.0.1/")
    assert code == 400, "198.18.x.x 字面量被放行了 —— 例外滥用到了第 1 层"
    assert body["error"] == "bad_base_url_private"


def test_T5_解析到fakeIP的域名必须放行(api, monkeypatch):
    """TUN 增强模式把所有域名解析成 198.18.x.x —— 不放行则产品在中国用户机器上不可用。"""
    _fake_resolver(monkeypatch, "gw.example.com", ["198.18.42.7"])
    code, body = _create(api, base_url="https://gw.example.com/v1")
    assert code == 201, "解析结果是 fake-IP 噪声，应跳过第 2 层判定：%s" % body


def test_T6_解析到真私有地址的域名仍要拦(api, monkeypatch):
    """证明第 2 层没被整层删掉。"""
    _fake_resolver(monkeypatch, "intra.example.com", ["10.0.0.5"])
    code, body = _create(api, base_url="https://intra.example.com/v1")
    assert code == 400
    assert body["error"] == "bad_base_url_private"


def test_混合口径_fakeIP加公网放行(api, monkeypatch):
    """§12.6 追认口径：**逐条剔除** 198.18/15，剩下的照原规则判。"""
    _fake_resolver(monkeypatch, "mix1.example.com", ["198.18.42.7", "8.8.8.8"])
    code, body = _create(api, base_url="https://mix1.example.com/v1")
    assert code == 201, "剔掉 fake-IP 后只剩公网地址(8.8.8.8)，应放行：%s" % body


def test_混合口径_fakeIP加内网仍400(api, monkeypatch):
    """原稿"全部落在 198.18/15 才作废"的字面口径已作废 —— 只 fake A 记录时它永远不成立。"""
    _fake_resolver(monkeypatch, "mix2.example.com", ["198.18.42.7", "10.0.0.5"])
    code, body = _create(api, base_url="https://mix2.example.com/v1")
    assert code == 400, "剔掉 fake-IP 后还剩一个内网地址，必须拦"
    assert body["error"] == "bad_base_url_private"


def test_T7_逃生门下连真私有解析也放行(make_client, monkeypatch):
    from conftest import _Api
    _fake_resolver(monkeypatch, "intra2.example.com", ["10.0.0.5"])
    a = _Api(make_client(HUB_ALLOW_PRIVATE_BASE_URL="1")[0])
    code, body = a.post("/hub/api/provider",
                        provider_payload(base_url="https://intra2.example.com/v1"))
    assert code == 201, body


def test_bad_base_url_private的文案要给出出路(api):
    """§3.3：detail 不能只说"不允许"，必须写清逃生门怎么开。"""
    code, body = _create(api, base_url="http://192.168.1.1/")
    assert code == 400
    assert "HUB_ALLOW_PRIVATE_BASE_URL" in str(body.get("detail", "")), \
        "detail 没告诉用户内网自建网关该怎么办：%r" % body.get("detail")


# ---------------- §3.2（修订 5/6/10）：active.alias 规则、version 来源、haiku_conflict ----------------

HAIKU = "claude-3-5-haiku-20241022"


def test_active_alias在none态也要回原值(api, settings_path):
    """㉑：一条规则通吃 —— active.alias 恒等于 env.ANTHROPIC_MODEL 原值，与 kind 无关。

    none 态尤其重要：页面文案「当前生效的是一个不在台账里的配置（alias: xxx）」只能取自这里。
    """
    write_settings(settings_path, _env(ANTHROPIC_BASE_URL="https://other.example.com",
                                       ANTHROPIC_MODEL="某个手写的模型名"))
    code, body = api.get("/hub/api/provider")
    assert body["active"]["kind"] == "none"
    assert body["active"]["alias"] == "某个手写的模型名", "none 态把 alias 吞掉了，用户无从判断在用什么"


def test_active_alias在claude_native与unknown态是null(api, settings_path):
    write_settings(settings_path, {"model": "sonnet"})
    assert api.get("/hub/api/provider")[1]["active"]["alias"] is None
    settings_path.write_text("{ 坏掉的", encoding="utf-8")
    assert api.get("/hub/api/provider")[1]["active"]["alias"] is None


def test_active_alias非字符串时按null处理(api, settings_path, stub):
    write_settings(settings_path, _env(ANTHROPIC_BASE_URL="http://127.0.0.1:%d" % stub.port,
                                       ANTHROPIC_MODEL=123))
    code, body = api.get("/hub/api/provider")
    assert code == 200
    assert body["active"]["alias"] is None


def test_version读的是本地版本文件不是cliproxy(api, stub):
    """㉔：cliproxy 没有 version 端点。语义是"我们装了哪一版"。"""
    from conftest import REPO_ROOT
    f = REPO_ROOT / "configs" / "cliproxy.version"
    want = f.read_text(encoding="utf-8").strip() if f.exists() else None
    code, body = api.get("/hub/api/provider")
    assert body["cliproxy"]["version"] == want
    assert not [r for r in stub.requests if "version" in r["path"]], "跑去问 cliproxy 要版本了"


def test_haiku双抢时列表给haiku_conflict警告(api, settings_path, stub):
    """㊵：与 active.kind 正交的第六个 warning。判据 = active 条目的 haiku alias 下不带 prefix 的条目数 ≠ 1。"""
    stub.seed_openai_block(name="a", alias=ALIAS)
    stub.seed_openai_block(name="b", alias="claude-opus-4-1-20250805",
                           base_url="https://gw.example.com/v1", upstream="glm-4.6")
    write_settings(settings_path, _env(
        ANTHROPIC_BASE_URL="http://127.0.0.1:%d" % stub.port, ANTHROPIC_MODEL=ALIAS))
    code, body = api.get("/hub/api/provider")
    assert code == 200
    assert "haiku_conflict" in body["warnings"], \
        "两条都在抢 haiku，后台摘要会在两家之间轮询，必须报出来"
    assert body["active"]["kind"] == "cliproxy", "haiku_conflict 与 active.kind 正交，不该影响它"


def test_haiku只有一个来源时没有haiku_conflict(api, settings_path, stub):
    stub.seed_openai_block(alias=ALIAS)
    write_settings(settings_path, _env(
        ANTHROPIC_BASE_URL="http://127.0.0.1:%d" % stub.port, ANTHROPIC_MODEL=ALIAS))
    code, body = api.get("/hub/api/provider")
    assert "haiku_conflict" not in body.get("warnings", [])


def test_用户手改停用的条目读侧也算disabled(api, stub, settings_path):
    """§3.0d 读侧取或：prefix 非空 **or** 条目自身 disabled:true。

    纯 prefix 语义会把用户手动停用的条目谎报为可用 —— 页面显示可切换、切过去却不通。
    """
    stub.seed_openai_block(alias=ALIAS, disabled=True, prefix="")
    write_settings(settings_path, _env(
        ANTHROPIC_BASE_URL="http://127.0.0.1:%d" % stub.port, ANTHROPIC_MODEL=ALIAS))
    code, body = api.get("/hub/api/provider")
    assert code == 200
    assert "alias_disabled" in body["warnings"], "用户手改的 disabled:true 被无视了"


def test_禁止调用management_config整包端点(api, stub):
    """㉕ 红线：`GET /v0/management/config` 整包回明文 api-keys，一次都不许打。"""
    assert _create(api)[0] == 201
    assert api.get("/hub/api/provider")[0] == 200
    bad = [r["path"] for r in stub.requests if r["path"].rstrip("/").endswith("/management/config")]
    assert bad == [], "打了会整包吐明文 key 的端点：%s" % bad


# ---------------- ⑳（修订 5）：alias_occupied 检查范围 = 主 alias ∪ haiku alias ----------------

def test_主alias各不相同但haiku全默认_第二个起创建即停用(api, stub):
    """§3.3 的必然后果，**不是 bug**：haiku 默认值人人相同，第 2/3 个一律 disabled。

    放任 haiku 多条同时 enabled = 主模型走 A 家、后台摘要走 B 家，比串台更隐蔽。
    """
    p1 = _create(api, model_alias=ALIAS)[1]
    p2 = _create(api, label="第二个", base_url="https://gw2.example.com/v1",
                 upstream_model="glm-4.6", model_alias="claude-opus-4-1-20250805")[1]
    p3 = _create(api, label="第三个", base_url="https://gw3.example.com/v1",
                 upstream_model="qwen-max", model_alias="claude-haiku-4-5-20251001")[1]
    assert p1["disabled"] is False, "第一个应该是可用的"
    assert p2["disabled"] is True and p3["disabled"] is True, \
        "主 alias 不冲突但 haiku 撞了，第 2/3 个必须创建即停用"


def test_切换到第三个后前两个都被停用(api, stub):
    """§3.3 用例原文：activate 第 3 个 → 1/2 变 disabled、第 3 个变 enabled，prefix 写两组。"""
    p1 = _create(api, model_alias=ALIAS)[1]
    p2 = _create(api, label="第二个", base_url="https://gw2.example.com/v1",
                 upstream_model="glm-4.6", model_alias="claude-opus-4-1-20250805")[1]
    p3 = _create(api, label="第三个", base_url="https://gw3.example.com/v1",
                 upstream_model="qwen-max", model_alias="claude-haiku-4-5-20251001")[1]
    assert api.post("/hub/api/provider/%s/activate" % p3["id"])[0] == 200
    got = {p["id"]: p["disabled"] for p in api.get("/hub/api/provider")[1]["providers"]}
    assert got[p3["id"]] is False
    assert got[p1["id"]] is True and got[p2["id"]] is True
    assert stub.aliases_enabled(HAIKU) == 1, "haiku alias 下应恰剩一个不带 prefix"


def test_落选条目的prefix就是它自己的id(api, stub):
    """§3.0d：落选 prefix = 该 provider 的 id（8 位 hex，稳定可读），当选清空。"""
    p1 = _create(api, model_alias=ALIAS)[1]
    p2 = _create(api, label="第二个", base_url="https://gw2.example.com/v1",
                 upstream_model="glm-4.6", model_alias="claude-opus-4-1-20250805")[1]
    assert api.post("/hub/api/provider/%s/activate" % p2["id"])[0] == 200
    by_url = {b["base-url"]: b for b in stub.all_blocks}
    assert by_url["https://gw2.example.com/v1"].get("prefix", "") == "", "当选条目不该带 prefix"
    assert by_url["https://api.deepseek.com/v1"].get("prefix") == p1["id"], \
        "落选条目的 prefix 应是它自己的 id，实得 %r" % by_url["https://api.deepseek.com/v1"].get("prefix")
