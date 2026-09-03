# -*- coding: utf-8 -*-
"""M3 第 2 段（provider CRUD + activate 编排 + claude-native + 自测 + 启动 reconcile）单测。

与验收测试的分工：验收按 INTERFACE 黑盒断言；这里补两类它测不到的东西 ——
1. **prefix 语义的互斥**（§3.0d 修订 5）。冻结的桩 `aliases_enabled()` 仍读 `disabled` 键，
   §10 已注明"与本定案冲突、归出题人待改批次，改前用例请按 prefix 判互斥"，
   所以运行时唯一性只能在这里按 prefix 断言。
2. **B 阶段失败但补偿成功**这条分支：桩的 `patch_fail_after_n` 一旦打开连补偿写也会失败
   （那是 `activate_partial` 的构造），补偿成功的路径只能在这层注入。

桩是 `tests/acceptance/provider_hub/stub_cliproxy.py`（§10 冻结契约），**只读复用**。
不起 moments.web（那会拖进 db / 鉴权门），只把蓝图挂到裸 Flask 上 —— 要验的就是蓝图本身。
settings.json 一律在 tmp 下（conftest 的 autouse fixture 钉死 CLAUDE_SETTINGS_PATH）。
"""
import json
import sys
from pathlib import Path

import pytest
from flask import Flask

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "tests" / "acceptance" / "provider_hub"))
from stub_cliproxy import StubCliproxy  # noqa: E402

from moments import cliproxy_client, hub_routes, provider_model, settings_io  # noqa: E402
from moments.provider_model import HubError  # noqa: E402

ALIAS = "claude-sonnet-4-5-20250929"
HAIKU = "claude-3-5-haiku-20241022"
MGMT = "sk-test-mgmt-0000"
DOWNSTREAM = "sk-test-downstream-0000"
# 公网 IP 字面量：不走 DNS，单测既不联网也不受本机 fake-IP DNS 影响
PUBLIC = "http://93.184.216.34/v1"
PUBLIC2 = "http://93.184.216.35/v1"


@pytest.fixture
def stub():
    s = StubCliproxy(mgmt_key=MGMT, downstream_key=DOWNSTREAM).start()
    try:
        yield s
    finally:
        s.stop()


@pytest.fixture
def env(monkeypatch, tmp_path, stub):
    """把 cliproxy 三键与回落文件都钉在 tmp/桩上，绝不继承外部真实凭据。"""
    monkeypatch.setenv("CLIPROXY_PORT", str(stub.port))
    monkeypatch.setenv("CLIPROXY_MGMT_KEY", MGMT)
    monkeypatch.setenv("CLIPROXY_API_KEY", DOWNSTREAM)
    monkeypatch.setenv("CLIPROXY_ANTHROPIC_COMPAT", "1")
    monkeypatch.setenv("HUB_ENV_FILE", str(tmp_path / "没有这个文件.env"))
    monkeypatch.delenv("HUB_ALLOW_PRIVATE_BASE_URL", raising=False)
    return monkeypatch


def make_app():
    """新建一个 app = 一次"门户启动"（蓝图注册时跑一次启动 reconcile）。"""
    app = Flask(__name__, template_folder=str(_ROOT / "moments" / "templates"))
    app.register_blueprint(hub_routes.hub_bp)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def c(env):
    return make_app()


def payload(**over):
    body = {"label": "DeepSeek", "kind": "openai", "base_url": PUBLIC,
            "api_key": "sk-test-aaaabbbbcccc", "upstream_model": "deepseek-v4-flash",
            "model_alias": ALIAS}
    body.update(over)
    return {k: v for k, v in body.items() if v is not OMIT}


OMIT = object()


def post(c, path, body=None):
    r = c.post(path, json={} if body is None else body)
    return r.status_code, r.get_json()


def create(c, **over):
    return post(c, "/hub/api/provider", payload(**over))


def prefixes(stub):
    """{provider id: prefix} —— §3.0d 的运行时真相：prefix 非空 = 落选（不接裸模型名）。"""
    out = {}
    for kind, blocks in (("openai", stub.openai_compat), ("anthropic", stub.claude_api_key),
                         ("gemini", stub.gemini_api_key)):
        for blk in blocks:
            out[provider_model.from_block_entry(kind, blk)["id"]] = blk.get("prefix")
    return out


def enabled_for(stub, alias):
    """按 prefix 语义数：注册了该 alias 且没带 prefix 的条目个数（运行时唯一性判据）。"""
    n = 0
    for blk in stub.all_blocks:
        if blk.get("prefix"):
            continue
        n += sum(1 for m in blk.get("models") or [] if m.get("alias") == alias)
    return n


def settings_now():
    p = provider_model.settings_path()
    return json.loads(Path(p).read_text(encoding="utf-8")) if Path(p).exists() else None


def write_settings(obj):
    p = Path(provider_model.settings_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


# ---------------- §3.3/§3.4/§3.5 错误码矩阵 ----------------

@pytest.mark.parametrize("over,err", [
    ({"kind": "nope"}, "bad_kind"),
    ({"kind": "nope", "label": ""}, "bad_kind"),          # 先命中先返回：kind 先于 label
    ({"label": ""}, "bad_label"),
    ({"label": "x" * 41}, "bad_label"),
    ({"api_key": ""}, "key_required"),
    ({"api_key": OMIT}, "key_required"),
    ({"base_url": "api.example.com"}, "bad_base_url"),
    ({"base_url": "ftp://93.184.216.34/"}, "bad_base_url"),
    ({"base_url": "http://127.0.0.1:8317/v1"}, "bad_base_url_private"),
    ({"base_url": "http://192.168.1.1/"}, "bad_base_url_private"),
    ({"base_url": "http://[::1]:8317/v1"}, "bad_base_url_private"),
    ({"model_alias": "a/b"}, "bad_alias"),
    ({"model_alias": " "}, "bad_alias"),
    ({"upstream_model": ""}, "bad_upstream_model"),
    ({"haiku_alias": ""}, "bad_haiku_alias"),
    ({"haiku_alias": ALIAS}, "bad_haiku_alias"),
])
def test_新增的错误码矩阵(c, over, err):
    code, body = create(c, **over)
    assert (code, body["error"]) == (400, err), body


@pytest.mark.parametrize("raw", ["[]", '"s"', "null", "123", ""])
def test_请求体不是JSON对象一律bad_body(c, raw):
    r = c.post("/hub/api/provider", data=raw, content_type="application/json")
    assert (r.status_code, r.get_json()["error"]) == (400, "bad_body")


def test_非json的Content_Type按体缺失处理(c):
    """§0.1：禁用 force=True。"""
    r = c.post("/hub/api/provider", data="label=x",
               content_type="application/x-www-form-urlencoded")
    assert (r.status_code, r.get_json()["error"]) == (400, "bad_body")


def test_逃生门开启后内网base_url可建(env):
    env.setenv("HUB_ALLOW_PRIVATE_BASE_URL", "1")
    code, body = create(make_app(), base_url="http://192.168.1.1/v1")
    assert code == 201, body


def test_新增落地两条models且当选条目prefix为空(c, stub):
    code, body = create(c)
    assert code == 201, body
    assert body["haiku_alias"] == HAIKU and body["disabled"] is False
    assert "api_key" not in body and body["key_masked"] == "****cccc"
    models = stub.openai_compat[0]["models"]
    assert [m["alias"] for m in models] == [ALIAS, HAIKU]
    assert {m["name"] for m in models} == {"deepseek-v4-flash"}
    assert stub.openai_compat[0]["prefix"] == ""
    assert stub.alias_count_violation == []


def test_第二个provider因haiku撞名而创建即带prefix(c, stub):
    a = create(c)[1]
    code, b = create(c, base_url=PUBLIC2, upstream_model="glm-4.6",
                     model_alias="claude-opus-4-1-20250805")
    assert code == 201 and b["disabled"] is True, b     # 主 alias 不撞，haiku 撞
    assert prefixes(stub)[b["id"]] == b["id"] and prefixes(stub)[a["id"]] == ""


def test_四元组重复返回409(c):
    assert create(c)[0] == 201
    assert create(c, label="换个名字")[:2][0] == 409
    assert create(c, label="换个名字")[1]["error"] == "duplicate_provider"


def test_三个kind各自落进自己的承载块(c, stub):
    assert create(c)[0] == 201
    assert create(c, kind="anthropic", base_url=PUBLIC2, upstream_model="glm-4.6")[0] == 201
    assert create(c, kind="gemini", base_url="http://93.184.216.36",
                  upstream_model="gemini-2.5-pro")[0] == 201
    assert len(stub.openai_compat) == len(stub.claude_api_key) == len(stub.gemini_api_key) == 1
    assert [r for r in stub.requests if "compatibility" in r["path"]
            and "openai" not in r["path"]] == []


def test_改与删的404与校验继承(c):
    assert c.patch("/hub/api/provider/deadbeef", json={"label": "x"}).status_code == 404
    assert c.delete("/hub/api/provider/deadbeef").status_code == 404
    pid = create(c)[1]["id"]
    r = c.patch("/hub/api/provider/%s" % pid, json={"base_url": "http://10.0.0.5/v1"})
    assert (r.status_code, r.get_json()["error"]) == (400, "bad_base_url_private")
    r = c.patch("/hub/api/provider/%s" % pid, json={"api_key": ""})
    assert (r.status_code, r.get_json()["error"]) == (400, "key_required")


def test_改四元组换id_旧id失效_且原key与手加字段保留(c, stub):
    pid = create(c)[1]["id"]
    stub.openai_compat[0]["weight"] = 7          # 用户手加的字段：整条替换时不该被抹掉
    r = c.patch("/hub/api/provider/%s" % pid, json={"upstream_model": "deepseek-v4-pro"})
    new = r.get_json()
    assert r.status_code == 200 and new["id"] != pid
    assert c.patch("/hub/api/provider/%s" % pid, json={"label": "x"}).status_code == 404
    blk = stub.openai_compat[0]
    assert blk["weight"] == 7
    assert blk["api-key-entries"][0]["api-key"] == "sk-test-aaaabbbbcccc"


def test_改haiku_alias不换id(c):
    pid = create(c)[1]["id"]
    r = c.patch("/hub/api/provider/%s" % pid, json={"haiku_alias": "claude-3-5-haiku-latest"})
    assert r.status_code == 200 and r.get_json()["id"] == pid


def test_删除生效中的provider返回409_其余可删(c):
    a = create(c)[1]
    b = create(c, base_url=PUBLIC2, upstream_model="glm-4.6")[1]
    assert post(c, "/hub/api/provider/%s/activate" % a["id"])[0] == 200
    r = c.delete("/hub/api/provider/%s" % a["id"])
    assert (r.status_code, r.get_json()["error"]) == (409, "delete_active")
    assert c.delete("/hub/api/provider/%s" % b["id"]).status_code == 200


def test_删除不动settings(c):
    write_settings({"model": "sonnet", "env": {"KEEP": "1"}})
    before = Path(provider_model.settings_path()).read_text(encoding="utf-8")
    pid = create(c)[1]["id"]
    assert c.delete("/hub/api/provider/%s" % pid).status_code == 200
    assert Path(provider_model.settings_path()).read_text(encoding="utf-8") == before


# ---------------- §0.3/§0.4 三种依赖故障 ----------------

@pytest.mark.parametrize("missing", ["CLIPROXY_PORT", "CLIPROXY_MGMT_KEY", "CLIPROXY_API_KEY"])
def test_三键任一缺失时写与自测端点503_列表仍200(env, missing):
    env.delenv(missing)
    c = make_app()
    calls = [post(c, "/hub/api/provider", payload()),
             post(c, "/hub/api/provider/deadbeef/activate"),
             post(c, "/hub/api/provider/deadbeef/test")]
    r = c.patch("/hub/api/provider/deadbeef", json={"label": "x"})
    calls.append((r.status_code, r.get_json()))
    r = c.delete("/hub/api/provider/deadbeef")
    calls.append((r.status_code, r.get_json()))
    for code, body in calls:
        assert (code, body["error"]) == (503, "cliproxy_unconfigured")
    code, body = c.get("/hub/api/provider").status_code, c.get("/hub/api/provider").get_json()
    assert code == 200 and body["providers"] == []
    assert body["cliproxy"]["running"] is False
    assert "cliproxy_unconfigured" in body["warnings"]
    assert "openai" in body["cliproxy"]["supported_kinds"]


def test_没跑时写端点503_列表200(c, stub):
    stub.set_down(True)
    code, body = create(c)
    assert (code, body["error"]) == (503, "cliproxy_down")
    body = c.get("/hub/api/provider").get_json()
    assert body["cliproxy"]["running"] is False and "cliproxy_down" in body["warnings"]


@pytest.mark.parametrize("status", [401, 500])
def test_管理API非2xx时写端点502_列表200带error_status(c, stub, status):
    stub.mgmt_status = status
    code, body = create(c)
    assert (code, body["error"]) == (502, "cliproxy_reject")
    assert str(status) in body["detail"] and MGMT not in json.dumps(body)
    body = c.get("/hub/api/provider").get_json()
    assert body["cliproxy"]["running"] is True          # 拿到响应就算跑着
    assert body["cliproxy"]["error_status"] == status
    assert body["providers"] == [] and "cliproxy_error" in body["warnings"]


# ---------------- §3.6 activate：A 只读 → B 可补偿 → C 不可回退 ----------------

def two(c):
    """a 已生效、b 落选（两条抢同一主 alias + 同一默认 haiku）。"""
    a = create(c)[1]
    assert post(c, "/hub/api/provider/%s/activate" % a["id"])[0] == 200
    b = create(c, label="备用网关", base_url=PUBLIC2, upstream_model="glm-4.6")[1]
    assert b["disabled"] is True
    return a, b


def writes(stub):
    return [r for r in stub.requests if r["method"] in ("PUT", "PATCH", "DELETE")]


def fail_nth_write(monkeypatch, n):
    """让第 n 次条目写抛 502。桩的 patch_fail_after_n 是"第 N+1 次起全失败"，
    构造不出"B 失败但补偿成功"这条分支，只能在这层注入。"""
    real = cliproxy_client.CliproxyClient.replace_entry
    seen = {"n": 0}

    def fake(self, kind, index, entry):
        seen["n"] += 1
        if seen["n"] == n:
            raise cliproxy_client.CliproxyError(502, "cliproxy_reject",
                                                "management API 返回 500", upstream_status=500)
        return real(self, kind, index, entry)
    monkeypatch.setattr(cliproxy_client.CliproxyClient, "replace_entry", fake)
    return seen


def test_切换写三键_BASE_URL不带v1_并删掉API_KEY(c, stub):
    write_settings({"model": "sonnet", "env": {"KEEP": "1", "ANTHROPIC_API_KEY": "sk-old"}})
    a = create(c)[1]
    assert post(c, "/hub/api/provider/%s/activate" % a["id"])[0] == 200
    data = settings_now()
    assert data["env"] == {"KEEP": "1", "ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % stub.port,
                           "ANTHROPIC_AUTH_TOKEN": DOWNSTREAM, "ANTHROPIC_MODEL": ALIAS}
    assert data["model"] == "sonnet"


def test_B阶段对主alias与haiku各做一次互斥_按prefix判(c, stub):
    """§3.0c/§3.0d：两个 alias 各一组 prefix 置/清。少一组 = 后台摘要请求打到旧后端。"""
    a, b = two(c)
    assert post(c, "/hub/api/provider/%s/activate" % b["id"])[0] == 200
    got = prefixes(stub)
    assert got[b["id"]] == "" and got[a["id"]] == a["id"]
    assert enabled_for(stub, ALIAS) == 1 and enabled_for(stub, HAIKU) == 1
    assert stub.alias_count_violation == []          # 互斥写回去的仍是两条 models


def test_幂等重放不再写cliproxy且结果一致(c, stub):
    a = create(c)[1]
    assert post(c, "/hub/api/provider/%s/activate" % a["id"])[0] == 200
    first, n = Path(provider_model.settings_path()).read_text(encoding="utf-8"), len(writes(stub))
    assert post(c, "/hub/api/provider/%s/activate" % a["id"])[0] == 200
    assert Path(provider_model.settings_path()).read_text(encoding="utf-8") == first
    assert len(writes(stub)) == n, "同目标重放又写了一次 cliproxy（不是幂等）"


def test_A阶段各失败点都无副作用(c, stub):
    a = create(c)[1]
    n = len(writes(stub))
    assert post(c, "/hub/api/provider/deadbeef/activate") == (404, {
        "error": "not_found", "detail": "provider 不存在"})
    for raw in ("{ 坏掉的", '"a string"', '{"env": "x"}', '{"env": 3}'):
        Path(provider_model.settings_path()).write_text(raw, encoding="utf-8")
        code, body = post(c, "/hub/api/provider/%s/activate" % a["id"])
        assert (code, body["error"]) == (409, "settings_unparsable"), raw
        assert Path(provider_model.settings_path()).read_text(encoding="utf-8") == raw
    Path(provider_model.settings_path()).unlink()
    stub.set_down(True)
    code, body = post(c, "/hub/api/provider/%s/activate" % a["id"])
    assert (code, body["error"]) == (503, "cliproxy_down")
    stub.set_down(False)
    assert len(writes(stub)) == n, "A 阶段失败却已经改了 cliproxy（步骤顺序错）"


def test_A3目录不可写返回500且不动cliproxy(c, stub, monkeypatch):
    a = create(c)[1]
    n = len(writes(stub))
    monkeypatch.setattr(settings_io, "dir_writable", lambda _p: False)
    code, body = post(c, "/hub/api/provider/%s/activate" % a["id"])
    assert (code, body["error"]) == (500, "settings_write_failed")
    assert len(writes(stub)) == n


def test_B阶段失败_补偿成功_502且prefix原样(c, stub, monkeypatch):
    """§3.6 三态表第二行：B1/B2 失败 → 502 cliproxy_reject，已补偿，无副作用。"""
    a, b = two(c)
    write_settings({"env": {"ANTHROPIC_MODEL": "old-alias"}})   # two() 里的 activate 已写过一次
    before = prefixes(stub)
    fail_nth_write(monkeypatch, 2)                    # B1 成功、B2 失败、补偿（第 3 次）成功
    code, body = post(c, "/hub/api/provider/%s/activate" % b["id"])
    assert (code, body["error"]) == (502, "cliproxy_reject"), body
    assert prefixes(stub) == before, "补偿没把 B1 改过的 prefix 还原"
    assert settings_now()["env"]["ANTHROPIC_MODEL"] == "old-alias"


def test_C阶段落盘失败_补偿成功_500settings_write_failed(c, stub, monkeypatch):
    a, b = two(c)
    write_settings({"env": {"ANTHROPIC_MODEL": "old-alias"}})
    before = prefixes(stub)

    def boom(_path, _data):
        raise HubError(500, "settings_write_failed", "写 settings.json 失败：OSError")
    monkeypatch.setattr(settings_io, "write_json_atomic", boom)
    code, body = post(c, "/hub/api/provider/%s/activate" % b["id"])
    assert (code, body["error"]) == (500, "settings_write_failed"), body
    assert prefixes(stub) == before, "C 段失败没执行补偿 → cliproxy 已切、settings 未切"
    assert settings_now()["env"]["ANTHROPIC_MODEL"] == "old-alias"


def test_补偿本身失败返回500_activate_partial并可重试收敛(c, stub):
    """§10 时序表：写1成功(B1) → 写2失败(B2) → 写3失败(补偿) = activate_partial。"""
    write_settings({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % stub.port,
                            "ANTHROPIC_MODEL": ALIAS}})
    a, b = two(c)
    before = Path(provider_model.settings_path()).read_text(encoding="utf-8")
    stub.patch_fail_after_n = 1
    code, body = post(c, "/hub/api/provider/%s/activate" % b["id"])
    assert (code, body["error"]) == (500, "activate_partial"), body
    assert body["detail"], "detail 必须明写请重试或手动检查"
    assert Path(provider_model.settings_path()).read_text(encoding="utf-8") == before
    # 不一致态在列表里如实暴露成 alias_disabled（页面提示"点一次切换即可修复"）
    assert "alias_disabled" in c.get("/hub/api/provider").get_json()["warnings"]
    stub.patch_fail_after_n = None                    # 故障排除后重放同一次 activate
    assert post(c, "/hub/api/provider/%s/activate" % b["id"])[0] == 200
    assert enabled_for(stub, ALIAS) == 1 and enabled_for(stub, HAIKU) == 1
    assert settings_now()["env"]["ANTHROPIC_MODEL"] == ALIAS


def test_activate响应与settings里的明文互不串味(c, stub):
    a = create(c)[1]
    code, body = post(c, "/hub/api/provider/%s/activate" % a["id"])
    assert code == 200
    assert DOWNSTREAM not in json.dumps(body) and "sk-test" not in json.dumps(body)
    assert settings_now()["env"]["ANTHROPIC_AUTH_TOKEN"] == DOWNSTREAM


def test_改动生效中的provider会自动重新切换(c, stub):
    a = create(c)[1]
    assert post(c, "/hub/api/provider/%s/activate" % a["id"])[0] == 200
    r = c.patch("/hub/api/provider/%s" % a["id"], json={"model_alias": "claude-opus-4-1-20250805"})
    assert r.status_code == 200 and r.get_json()["active"] is True
    assert settings_now()["env"]["ANTHROPIC_MODEL"] == "claude-opus-4-1-20250805"


# ---------------- §5 Claude 原生订阅 ----------------

def test_切回官方订阅四键全删且不碰cliproxy(c, stub):
    write_settings({"model": "sonnet", "env": {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:8317", "ANTHROPIC_AUTH_TOKEN": "sk-test-x",
        "ANTHROPIC_MODEL": ALIAS, "ANTHROPIC_API_KEY": "sk-test-y", "OTHER": "keep"}})
    n = len(stub.requests)
    assert post(c, "/hub/api/claude-native/activate") == (
        200, {"ok": True, "active_kind": "claude_native"})
    assert settings_now()["env"] == {"OTHER": "keep"}
    assert len(stub.requests) == n, "§5 明写不经过 cliproxy"


def test_切回官方订阅在没配与没跑时都可用_且幂等(env, stub):
    stub.set_down(True)
    write_settings({"env": {"ANTHROPIC_MODEL": "x"}})
    c = make_app()
    assert post(c, "/hub/api/claude-native/activate")[0] == 200
    first = Path(provider_model.settings_path()).read_text(encoding="utf-8")
    assert post(c, "/hub/api/claude-native/activate")[0] == 200
    assert Path(provider_model.settings_path()).read_text(encoding="utf-8") == first
    env.delenv("CLIPROXY_PORT")
    assert post(make_app(), "/hub/api/claude-native/activate")[0] == 200


@pytest.mark.parametrize("raw", ["{ 坏", '{"env": "x"}', "[]"])
def test_切回官方订阅遇坏settings返回409且原样保留(c, raw):
    Path(provider_model.settings_path()).write_text(raw, encoding="utf-8")
    code, body = post(c, "/hub/api/claude-native/activate")
    assert (code, body["error"]) == (409, "settings_unparsable")
    assert Path(provider_model.settings_path()).read_text(encoding="utf-8") == raw


# ---------------- §3.6 启动 reconcile（[RA] F4：全方案唯一收住漂移的东西）----------------

def test_reconcile把多个enabled收敛成一个并打日志(env, stub, capfd):
    """漂移态：两条都没带 prefix（用户手改 config.yaml / 上一次补偿失败）。"""
    stub.seed_openai_block(name="a", alias=ALIAS)
    stub.seed_openai_block(name="b", base_url="https://gw.example.com/v1",
                           upstream="glm-4.6", alias=ALIAS)
    write_settings({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % stub.port,
                            "ANTHROPIC_MODEL": ALIAS}})
    make_app()                                        # 一次 app 注册 = 一次门户启动
    assert enabled_for(stub, ALIAS) == 1, "启动 reconcile 没把 alias 收敛成唯一"
    assert "hub_reconcile alias=%s haiku=%s fixed=true" % (ALIAS, HAIKU) in capfd.readouterr()[1]


def test_reconcile把0个enabled的alias自愈(env, stub):
    """§3.6 断言点：alias 下 0 个 enabled（都带着 prefix）→ 重启门户 → 恢复 1 个。"""
    blk = stub.seed_openai_block(alias=ALIAS)
    blk["prefix"] = "deadbeef"
    write_settings({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % stub.port,
                            "ANTHROPIC_MODEL": ALIAS}})
    body = make_app().get("/hub/api/provider").get_json()
    assert "alias_disabled" not in body["warnings"], "漂移无人收敛"
    assert enabled_for(stub, ALIAS) == 1


def test_reconcile在没配_没跑_坏settings_无alias时一律跳过(env, stub, capfd):
    stub.seed_openai_block(alias=ALIAS)
    write_settings({"model": "sonnet"})               # 没有 ANTHROPIC_MODEL
    make_app()
    Path(provider_model.settings_path()).write_text("{ 坏", encoding="utf-8")
    make_app()
    stub.set_down(True)
    make_app()
    env.delenv("CLIPROXY_PORT")
    make_app()
    assert capfd.readouterr()[1].count("hub_reconcile skipped") == 4
    assert stub.openai_compat[0].get("prefix") is None, "跳过的路径不该改 cliproxy"


def test_reconcile在alias根本不存在时报fixed_false(env, stub, capfd):
    write_settings({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % stub.port,
                            "ANTHROPIC_MODEL": "alias-that-never-existed"}})
    make_app()
    assert "fixed=false" in capfd.readouterr()[1]


def test_reconcile不写settings也不因异常拦住启动(env, stub, monkeypatch, capfd):
    stub.seed_openai_block(name="a", alias=ALIAS)     # 两条抢同一 alias = 需要自愈的漂移态
    stub.seed_openai_block(name="b", base_url="https://gw.example.com/v1", alias=ALIAS)
    write_settings({"env": {"ANTHROPIC_MODEL": ALIAS}})
    before = Path(provider_model.settings_path()).read_text(encoding="utf-8")

    def boom(self, *a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(cliproxy_client.CliproxyClient, "set_alias_exclusive", boom)
    assert make_app() is not None, "自愈失败把门户启动拦死了"
    assert Path(provider_model.settings_path()).read_text(encoding="utf-8") == before
    assert "hub_reconcile skipped reason=RuntimeError" in capfd.readouterr()[1]


# ---------------- §3.7 连通性自测 ----------------

def test_自测成功回ok与延迟与模型回显(c, stub):
    a = create(c)[1]
    assert post(c, "/hub/api/provider/%s/activate" % a["id"])[0] == 200
    code, body = post(c, "/hub/api/provider/%s/test" % a["id"])
    assert code == 200 and body["ok"] is True
    assert body["model_echo"] == ALIAS and len(body["text_head"]) <= 40
    assert isinstance(body["latency_ms"], int)
    sent = json.loads([r for r in stub.requests if r["path"] == "/v1/messages"][-1]["body"])
    assert sent == {"model": ALIAS, "max_tokens": 16,
                    "messages": [{"role": "user", "content": "hi"}]}


def test_自测遇上游4xx回显请求头时只回结构化字段(c, stub):
    a = create(c)[1]
    post(c, "/hub/api/provider/%s/activate" % a["id"])
    stub.messages_mode = "http4xx_echo"
    code, body = post(c, "/hub/api/provider/%s/test" % a["id"])
    assert (code, body["ok"], body["stage"]) == (200, False, "upstream")
    assert body["upstream_status"] == 401 and body["error_type"] == "authentication_error"
    text = json.dumps(body, ensure_ascii=False)
    assert DOWNSTREAM not in text and "Bearer" not in text and "echoed" not in text


def test_自测按message文本判routing而不是按状态码(c, stub):
    """§3.7 修正：真机回 400、[CPX] 写 502，按码归类会把"配置缺失"说成"上游报错"。"""
    a = create(c)[1]
    post(c, "/hub/api/provider/%s/activate" % a["id"])
    stub.messages_mode = "unknown_model"
    code, body = post(c, "/hub/api/provider/%s/test" % a["id"])
    assert (code, body["ok"], body["stage"]) == (200, False, "routing")


def test_落选条目自测回stage_disabled且不真发请求(c, stub):
    a, b = two(c)
    n = len([r for r in stub.requests if r["path"] == "/v1/messages"])
    code, body = post(c, "/hub/api/provider/%s/test" % b["id"])
    assert (code, body["ok"], body["stage"]) == (200, False, "disabled")
    assert len([r for r in stub.requests if r["path"] == "/v1/messages"]) == n


def test_自测的两种非200_不存在的id与cliproxy不可达(c, stub):
    a = create(c)[1]
    assert post(c, "/hub/api/provider/deadbeef/test")[0] == 404
    stub.set_down(True)
    code, body = post(c, "/hub/api/provider/%s/test" % a["id"])
    assert (code, body["error"]) == (503, "cliproxy_down")


def test_自测超时是业务结果不是down(c, stub, monkeypatch):
    """§3.7：30 秒超时 → 200 + stage:timeout（真等 30 秒的用例在验收里标了 slow）。"""
    a = create(c)[1]
    post(c, "/hub/api/provider/%s/activate" % a["id"])
    stub.messages_mode, stub.timeout_sleep = "timeout", 3
    real = cliproxy_client.CliproxyClient.probe_messages      # 真超时，只把 30s 窗口压到 0.3s
    monkeypatch.setattr(cliproxy_client.CliproxyClient, "probe_messages",
                        lambda self, model, timeout=0.3: real(self, model, timeout))
    code, body = post(c, "/hub/api/provider/%s/test" % a["id"])
    assert (code, body["ok"], body["stage"]) == (200, False, "timeout")


# ---------------- BUG-19：activate 全段互斥 ----------------

def test_切换被别的切换占着时409_activate_busy且零副作用(c, stub):
    """并发两个 activate 会各自基于旧快照读-改-写条目表，终态可能两条都带 prefix
    而双方都回 200。整段串行化后，抢不到锁的那个如实回 409，什么都不改。"""
    a = create(c)[1]
    before = (prefixes(stub), len(writes(stub)))
    with c.application.app_context():
        with hub_routes.hold_activate_lock():             # 锁被别人占着
            code, body = post(c, "/hub/api/provider/%s/activate" % a["id"])
            native = post(c, "/hub/api/claude-native/activate")
    assert (code, body["error"]) == (409, "activate_busy"), body
    assert (native[0], native[1]["error"]) == (409, "activate_busy"), native
    assert (prefixes(stub), len(writes(stub))) == before
    assert settings_now() is None, "抢不到锁却写了 settings.json"
    assert post(c, "/hub/api/provider/%s/activate" % a["id"])[0] == 200   # 锁释放后照常


def test_三个写settings的入口共用同一把锁(c):
    """§3.6 A0：同一把非阻塞锁挂 app.extensions["hub_activate"]，
    覆盖 §3.6 activate / §5 claude-native / §4.6 OAuth activate（第 3 段由 M5 接入）。"""
    with c.application.app_context():
        with hub_routes.hold_activate_lock():
            assert c.application.extensions["hub_activate"].locked()
    assert not c.application.extensions["hub_activate"].locked()


def test_并发切换不同provider终态只可能是某一方的完整结果(c, stub):
    import threading
    a = create(c)[1]
    b = create(c, label="备用网关", base_url=PUBLIC2, upstream_model="glm-4.6",
               model_alias="claude-opus-4-1-20250805")[1]
    for _ in range(8):
        gate, res = threading.Barrier(2), {}

        def go(tag, pid):
            gate.wait()
            res[tag] = post(c, "/hub/api/provider/%s/activate" % pid)[0]
        ts = [threading.Thread(target=go, args=("a", a["id"])),
              threading.Thread(target=go, args=("b", b["id"]))]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        assert sorted(res.values()) == [200, 409], res
        alias = settings_now()["env"]["ANTHROPIC_MODEL"]
        assert enabled_for(stub, alias) == 1, "settings 指的 alias 没有可命中的条目"
        assert enabled_for(stub, HAIKU) == 1, "haiku 被两次切换各写一半写没了"


# ---------------- BUG-20：haiku 漂移（§3.2 haiku_conflict + reconcile 的 I2）----------------

def seed_pair(stub, haiku=HAIKU):
    """两条主 alias 各自唯一、却共用同一 haiku alias，且都不带 prefix。
    这正是 B 阶段补偿失败留下的态：主模型走对后端，后台摘要在两家之间轮询。"""
    a = stub.seed_openai_block(name="a", alias=ALIAS, haiku_alias=haiku)
    b = stub.seed_openai_block(name="b", base_url="https://gw.example.com/v1",
                               upstream="glm-4.6", alias="claude-opus-4-1-20250805",
                               haiku_alias=haiku)
    write_settings({"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % stub.port,
                            "ANTHROPIC_MODEL": ALIAS}})
    return a, b


def test_haiku双启用时列表给haiku_conflict(c, stub):
    seed_pair(stub)                                   # app 已建好，桩状态是之后铺的
    body = c.get("/hub/api/provider").get_json()
    assert body["active"]["alias"] == ALIAS           # 与 active.kind 正交，五态照常取值
    assert body["active"]["kind"] == "cliproxy"
    assert "haiku_conflict" in body["warnings"]


def test_haiku只有一个不带prefix时不报conflict(c, stub):
    a, b = seed_pair(stub)
    b["prefix"] = "deadbeef"
    assert "haiku_conflict" not in c.get("/hub/api/provider").get_json()["warnings"]


def test_haiku_conflict可与alias_disabled同时出现(c, stub):
    """§3.2：两者正交，warnings 是数组不是单值。"""
    a, b = seed_pair(stub)
    a["disabled"] = True                              # 用户手改停用（读侧 OR，写侧不碰）
    stub.seed_openai_block(name="c2", base_url="https://gw2.example.com/v1", upstream="x",
                           alias="claude-haiku-only", haiku_alias=HAIKU)   # 第 2 个抢 haiku 的
    w = c.get("/hub/api/provider").get_json()["warnings"]
    assert "alias_disabled" in w and "haiku_conflict" in w, w


def test_reconcile修I2_haiku也收敛成一条且是同一条目(env, stub, capfd):
    """§3.6 断言点 2：主 alias 各自唯一、两条共用 haiku 且都不带 prefix → 重启门户 →
    无 haiku_conflict，且只剩一个条目不带 prefix。"""
    seed_pair(stub)
    body = make_app().get("/hub/api/provider").get_json()
    assert "haiku_conflict" not in body["warnings"], "只修 I1 的 reconcile 收不住 haiku 漂移"
    assert enabled_for(stub, HAIKU) == 1 and enabled_for(stub, ALIAS) == 1
    live = [b for b in stub.all_blocks if not b.get("prefix")]
    assert len(live) == 1 and live[0]["models"][0]["alias"] == ALIAS
    out = capfd.readouterr()[1]
    assert "alias=%s" % ALIAS in out and "haiku=%s" % HAIKU in out and "fixed=true" in out
