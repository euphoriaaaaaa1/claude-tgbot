# -*- coding: utf-8 -*-
"""§3.6 activate —— 唯一写 settings.json 的端点之一。

覆盖：两种写入形态、幂等、A/B/C 三阶段的如实错误契约、无副作用与补偿、
文件权限与临时文件卫生、direct 形态不碰 8317。
所有用例的 settings.json 都在 tmp 下（conftest 的 settings_path 强制）。
"""
import json
import os
import stat

import pytest

from conftest import provider_payload, write_settings, read_settings, FAKE_DOWNSTREAM_KEY

ALIAS = "claude-sonnet-4-5-20250929"
HAIKU = "claude-3-5-haiku-20241022"


def _mk(api, **over):
    code, body = api.post("/hub/api/provider", provider_payload(**over))
    assert code == 201, body
    return body


def test_切换成功后settings三键按cliproxy形态写入(api, settings_path, stub):
    """BRIEF S1：填 key → 切换 → bot 用它回复。BASE_URL 不带 /v1（§3.6 形态表）。"""
    p = _mk(api)
    code, body = api.post("/hub/api/provider/%s/activate" % p["id"])
    assert code == 200, body
    env = read_settings(settings_path)["env"]
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:%d" % stub.port
    assert env["ANTHROPIC_AUTH_TOKEN"] == FAKE_DOWNSTREAM_KEY
    assert env["ANTHROPIC_MODEL"] == ALIAS
    assert "ANTHROPIC_API_KEY" not in env, "§3.6 形态表要求删除 ANTHROPIC_API_KEY"


def test_切换不动其余键与顶层model(api, settings_path):
    write_settings(settings_path, {"model": "sonnet", "permissions": {"allow": ["Bash"]},
                                   "env": {"KEEP_ME": "1"}})
    p = _mk(api)
    assert api.post("/hub/api/provider/%s/activate" % p["id"])[0] == 200
    data = read_settings(settings_path)
    assert data["model"] == "sonnet"
    assert data["permissions"] == {"allow": ["Bash"]}
    assert data["env"]["KEEP_ME"] == "1"


def test_settings不存在时新建而不是报错(api, settings_path):
    assert not settings_path.exists()
    p = _mk(api)
    assert api.post("/hub/api/provider/%s/activate" % p["id"])[0] == 200
    assert settings_path.exists()
    assert read_settings(settings_path)["env"]["ANTHROPIC_MODEL"] == ALIAS


def test_新建的settings文件权限是600(api, settings_path):
    """§3.6 [R4D] B3：原文件不存在时才用 0600（文件里有 AUTH_TOKEN 明文）。"""
    p = _mk(api)
    assert api.post("/hub/api/provider/%s/activate" % p["id"])[0] == 200
    mode = stat.S_IMODE(os.stat(settings_path).st_mode)
    assert mode == 0o600, "新建 settings.json 权限是 %o，明文令牌对同机其他用户可读" % mode


def test_已存在文件的权限被沿用不被悄悄收紧(api, settings_path):
    """[R4D] B3：不要把用户原本 644 的文件悄悄改成 600。"""
    write_settings(settings_path, {})
    os.chmod(settings_path, 0o644)
    p = _mk(api)
    assert api.post("/hub/api/provider/%s/activate" % p["id"])[0] == 200
    assert stat.S_IMODE(os.stat(settings_path).st_mode) == 0o644


def test_重复activate幂等_两次都200且结果一致(api, settings_path, stub):
    """§3.6：重复点击、重试、断电重跑都收敛到同一结果，不因"已经是这个状态"报错。"""
    p = _mk(api)
    c1, b1 = api.post("/hub/api/provider/%s/activate" % p["id"])
    first = settings_path.read_text(encoding="utf-8")
    c2, b2 = api.post("/hub/api/provider/%s/activate" % p["id"])
    assert (c1, c2) == (200, 200), (b1, b2)
    assert settings_path.read_text(encoding="utf-8") == first
    assert stub.aliases_enabled(ALIAS) == 1, "幂等重跑后同 alias 的 enabled 条目不是 1 个"


def test_来回切两个provider_同alias运行时恒只有一个enabled(api, settings_path, stub):
    """BRIEF S1/S2 的核心场景：配好两个来源在页面上来回切。"""
    a = _mk(api)
    b = _mk(api, label="另一个网关", base_url="https://gw.example.com/v1",
            upstream_model="glm-4.6")
    assert api.post("/hub/api/provider/%s/activate" % a["id"])[0] == 200
    assert stub.aliases_enabled(ALIAS) == 1
    assert api.post("/hub/api/provider/%s/activate" % b["id"])[0] == 200
    assert stub.aliases_enabled(ALIAS) == 1, "切过去之后旧条目没被 disable，cliproxy 路由不确定"
    code, body = api.get("/hub/api/provider")
    assert body["active"]["id"] == b["id"]
    assert api.post("/hub/api/provider/%s/activate" % a["id"])[0] == 200
    assert api.get("/hub/api/provider")[1]["active"]["id"] == a["id"]


def test_切换到不存在的id返回404且无副作用(api, settings_path, stub):
    write_settings(settings_path, {"env": {}})
    before = settings_path.read_text(encoding="utf-8")
    code, body = api.post("/hub/api/provider/deadbeef/activate")
    assert code == 404
    assert body["error"] == "not_found"
    assert settings_path.read_text(encoding="utf-8") == before
    assert not stub.paths_hit("openai-compatibility") or \
        all(r["method"] == "GET" for r in stub.paths_hit("openai-compatibility")), \
        "A 阶段失败却已经改了 cliproxy（步骤顺序错）"


@pytest.mark.parametrize("raw,why", [("{ 坏掉的", "整体不是 JSON"),
                                     ('"a string"', "顶层不是对象"),
                                     ('{"env": "x"}', "env 不是对象（[R4D] I1-5）"),
                                     ('{"env": 3}', "env 是数字")])
def test_settings不可解析返回409且不动cliproxy(api, settings_path, stub, raw, why):
    p = _mk(api)
    settings_path.write_text(raw, encoding="utf-8")
    snapshot = json.dumps(stub.openai_compat, sort_keys=True)
    code, body = api.post("/hub/api/provider/%s/activate" % p["id"])
    assert code == 409, "%s：期望 409 settings_unparsable，实得 %s" % (why, code)
    assert body["error"] == "settings_unparsable"
    assert json.dumps(stub.openai_compat, sort_keys=True) == snapshot, \
        "A 阶段失败仍改了 cliproxy → 旧 alias 被禁而 settings 仍指向它，bot 全线 502"
    assert settings_path.read_text(encoding="utf-8") == raw


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root 无视目录权限，该用例无意义")
def test_settings目录不可写返回500_settings_write_failed(api, settings_path, stub):
    p = _mk(api)
    snapshot = json.dumps(stub.openai_compat, sort_keys=True)
    os.chmod(settings_path.parent, 0o500)
    try:
        code, body = api.post("/hub/api/provider/%s/activate" % p["id"])
    finally:
        os.chmod(settings_path.parent, 0o700)
    assert code == 500
    assert body["error"] == "settings_write_failed"
    assert json.dumps(stub.openai_compat, sort_keys=True) == snapshot, "A3 失败前不该动 cliproxy"


def test_cliproxy不可达时切换返回503且settings不变(api, settings_path, stub):
    p = _mk(api)
    write_settings(settings_path, {"env": {"ANTHROPIC_MODEL": "old-alias"}})
    stub.set_down(True)
    code, body = api.post("/hub/api/provider/%s/activate" % p["id"])
    assert code == 503
    assert body["error"] == "cliproxy_down"
    assert read_settings(settings_path)["env"]["ANTHROPIC_MODEL"] == "old-alias"


def test_B阶段cliproxy拒绝时502且settings原样(api, settings_path, stub):
    """§3.6 三态表：B1/B2 失败 → 502 cliproxy_reject，已补偿，无副作用。"""
    p = _mk(api)
    write_settings(settings_path, {"env": {"ANTHROPIC_MODEL": "old-alias"}})
    stub.mgmt_status = 500
    code, body = api.post("/hub/api/provider/%s/activate" % p["id"])
    assert code == 502
    assert body["error"] == "cliproxy_reject"
    assert read_settings(settings_path)["env"]["ANTHROPIC_MODEL"] == "old-alias"


def test_C阶段落盘失败返回500且不留临时文件(api, settings_path, stub, monkeypatch):
    """§3.6 [R4D] I9 的用例：mock os.replace 抛异常 → 目录下无残留临时文件。

    只 patch stdlib 的 os.replace，不碰实现代码。实现若用 `from os import replace`
    则本用例打不中（会以"没触发失败"的形式失败），那本身也是要报告的信息。
    """
    p = _mk(api)
    write_settings(settings_path, {"env": {}})
    before = set(os.listdir(settings_path.parent))

    def boom(*a, **kw):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(os, "replace", boom)
    code, body = api.post("/hub/api/provider/%s/activate" % p["id"])
    assert code == 500, "os.replace 抛异常仍返回 %s" % code
    assert body["error"] == "settings_write_failed"
    residue = set(os.listdir(settings_path.parent)) - before
    assert not residue, "残留临时文件（含 AUTH_TOKEN 明文）：%s" % residue


def test_写settings用的临时文件不是可预测的固定名(api, settings_path, stub, monkeypatch):
    """[R4D] I9：固定名 settings.json.hubtmp 会被并发互相覆盖、且名字可预测。"""
    p = _mk(api)
    seen = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b, *x, **k: (seen.append(str(a)), real(a, b))[1])
    assert api.post("/hub/api/provider/%s/activate" % p["id"])[0] == 200
    assert seen, "没观测到 os.replace 原子替换（§3.6 C2 要求）"
    assert not any(s.endswith("settings.json.hubtmp") for s in seen)


# ⑩（修订 4）：`anthropic-direct` 永不启用（S0-B 已实证 claude-api-key 能承载第三方端点），
# 原先 3 条 direct activate 用例删除。改为一条恒真断言：anthropic 条目也走 cliproxy 路由。

def test_anthropic条目也按cliproxy形态写settings(api, stub, settings_path):
    """§3.1 修订 4：route 恒 "cliproxy"，所以 BASE_URL 指本机 8317 而不是用户端点。"""
    code, p = api.post("/hub/api/provider", provider_payload(
        kind="anthropic", label="第三方 Anthropic 网关",
        base_url="https://gw.example.com", upstream_model="glm-4.6"))
    assert code == 201, p
    assert p["route"] == "cliproxy"
    assert api.post("/hub/api/provider/%s/activate" % p["id"])[0] == 200
    env = read_settings(settings_path)["env"]
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:%d" % stub.port
    assert env["ANTHROPIC_MODEL"] == p["model_alias"], "走 cliproxy 就该填官方 alias"


def _startup_reconcile():
    """启动自愈的触发点：门户进程**显式调用**，不再由蓝图注册副作用触发。

    改动理由是架构级的：注册即连管理端口会让每个 import 门户模块的 bot 进程
    都去连 8317。契约随之变成"谁启动谁调"，用例照此显式调一次。
    """
    from moments import hub_routes
    hub_routes.reconcile()


def test_启动reconcile把0个不带prefix的主alias自愈(make_client, settings_path, stub):
    """§3.6 断言点 1（㊶ 按 prefix 口径改写）：构造"主 alias 下 0 个不带 prefix" → 重启门户 → 自愈。"""
    from conftest import _Api
    stub.seed_openai_block(prefix="deadbeef")          # 唯一候选却带着 prefix = 谁都命中不了
    write_settings(settings_path, {"env": {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % stub.port,
        "ANTHROPIC_MODEL": ALIAS}})
    c, _ = make_client()
    _startup_reconcile()                               # 自愈由门户进程显式触发，不再挂在蓝图注册上
    code, body = _Api(c).get("/hub/api/provider")
    assert code == 200
    assert "alias_disabled" not in body.get("warnings", []), \
        "启动 reconcile 没把唯一候选条目的 prefix 清掉（漂移无人收敛）"
    assert stub.aliases_enabled(ALIAS) == 1


def test_启动reconcile把haiku双抢也修掉(make_client, settings_path, stub):
    """§3.6 断言点 2（㊵ I2）：主 alias 各自唯一、但两条都不带 prefix 且共用同一 haiku alias。

    只修 I1 会留下永远自愈不了的态：主模型走对后端、后台摘要在两家之间轮询。
    """
    from conftest import _Api
    stub.seed_openai_block(name="a", alias=ALIAS)
    stub.seed_openai_block(name="b", alias="claude-opus-4-1-20250805",
                           base_url="https://gw.example.com/v1", upstream="glm-4.6")
    write_settings(settings_path, {"env": {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % stub.port,
        "ANTHROPIC_MODEL": ALIAS}})
    assert stub.aliases_enabled(HAIKU) == 2, "前置条件：两条都不带 prefix 抢同一个 haiku"
    c, _ = make_client()
    _startup_reconcile()
    code, body = _Api(c).get("/hub/api/provider")
    assert code == 200
    assert "haiku_conflict" not in body.get("warnings", []), \
        "reconcile 没修 I2 —— 后台摘要仍在两家之间轮询"
    assert stub.aliases_enabled(HAIKU) == 1, "haiku alias 下应只剩一个不带 prefix 的条目"


def test_activate响应体不含任何key明文(api, stub):
    """S8 反向用例：切换要把下游 key 写进 settings，但响应里一个字都不许有。"""
    p = _mk(api)
    code, body = api.post("/hub/api/provider/%s/activate" % p["id"])
    assert code == 200
    text = json.dumps(body, ensure_ascii=False)
    assert FAKE_DOWNSTREAM_KEY not in text
    assert "sk-test" not in text


# ---------------- §10 修订 3：patch_fail_after_n 把 activate_partial 变成可黑盒触发 ----------------

def _two_sharing_alias(api):
    """建两条抢同一 alias 的 provider：a 已启用，b 新建即 disabled。"""
    a = _mk(api)
    assert api.post("/hub/api/provider/%s/activate" % a["id"])[0] == 200
    b = _mk(api, label="备用网关", base_url="https://gw.example.com/v1",
            upstream_model="glm-4.6")
    assert b["disabled"] is True
    return a, b


def test_补偿失败时返回500_activate_partial(api, stub, settings_path):
    """§10 时序表：写 1 成功(B1) → 写 2 失败(B2) → 写 3 失败(补偿) = activate_partial。"""
    write_settings(settings_path, {"env": {"ANTHROPIC_MODEL": ALIAS}})
    a, b = _two_sharing_alias(api)
    before = settings_path.read_text(encoding="utf-8")
    stub.patch_fail_after_n = 1
    code, body = api.post("/hub/api/provider/%s/activate" % b["id"])
    assert code == 500, "期望 500 activate_partial，实得 %s %s" % (code, body)
    assert body["error"] == "activate_partial"
    assert body.get("detail"), "detail 必须明写：请重试或手动检查"
    assert settings_path.read_text(encoding="utf-8") == before, \
        "activate_partial 的定义是 cliproxy 已切、settings 未切"


def test_不一致态在列表里表现为alias_disabled(api, stub, settings_path):
    """§10 断言 2：旧条目仍是 disabled，正是 alias_disabled 这一态的定义。"""
    write_settings(settings_path, {"env": {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % stub.port, "ANTHROPIC_MODEL": ALIAS}})
    a, b = _two_sharing_alias(api)
    stub.patch_fail_after_n = 1
    assert api.post("/hub/api/provider/%s/activate" % b["id"])[0] == 500
    stub.patch_fail_after_n = None
    code, body = api.get("/hub/api/provider")
    assert code == 200
    assert "alias_disabled" in body["warnings"], "不一致态没有暴露成可收敛的 warning"


def test_故障排除后重放同一次activate收敛为200(api, stub, settings_path):
    """§10 断言 3：幂等 + 重试可收敛 —— 这是 activate_partial 唯一可接受的出路。"""
    a, b = _two_sharing_alias(api)
    stub.patch_fail_after_n = 1
    assert api.post("/hub/api/provider/%s/activate" % b["id"])[0] == 500
    stub.patch_fail_after_n = None
    code, body = api.post("/hub/api/provider/%s/activate" % b["id"])
    assert code == 200, "重放没收敛：%s %s" % (code, body)
    assert stub.aliases_enabled(ALIAS) == 1
    assert read_settings(settings_path)["env"]["ANTHROPIC_MODEL"] == ALIAS


# ---------------- §3.0c / §3.6 B 阶段（修订 4）：两组 alias 各跑一次互斥 ----------------


def test_activate对主alias与haiku_alias各做一次互斥(api, stub):
    """§3.0c 必须存在的用例：少跑一组 = haiku 请求打到别的后端或 502。

    两个 provider 的默认 haiku 名相同，所以切换后两个 alias 都必须只剩一个 enabled。
    """
    a = _mk(api)
    assert api.post("/hub/api/provider/%s/activate" % a["id"])[0] == 200
    b = _mk(api, label="备用网关", base_url="https://gw.example.com/v1",
            upstream_model="glm-4.6")
    assert api.post("/hub/api/provider/%s/activate" % b["id"])[0] == 200
    assert stub.aliases_enabled(ALIAS) == 1, "主 alias 没做互斥"
    assert stub.aliases_enabled(HAIKU) == 1, \
        "haiku alias 没做互斥（B 阶段只跑了一组）——后台摘要请求会打到旧后端"


def test_切换后生效的两条alias属于同一个provider(api, stub):
    """互斥做了但做错对象也不行：两个 alias 必须落在同一条 enabled 条目上。"""
    a = _mk(api)
    api.post("/hub/api/provider/%s/activate" % a["id"])
    b = _mk(api, label="备用网关", base_url="https://gw.example.com/v1",
            upstream_model="glm-4.6")
    assert api.post("/hub/api/provider/%s/activate" % b["id"])[0] == 200
    live = [blk for blk in stub.all_blocks if not blk.get("prefix")]
    assert len(live) == 1, "应恰有一条不带 prefix 的条目，实得 %d" % len(live)
    assert {m["alias"] for m in live[0]["models"]} == {ALIAS, HAIKU}
    assert live[0]["base-url"] == "https://gw.example.com/v1"


def test_activate过程不产生alias数违规(api, stub):
    """§10 记账：B 阶段的每次写回去的条目仍必须是两条 models。"""
    p = _mk(api)
    assert api.post("/hub/api/provider/%s/activate" % p["id"])[0] == 200
    assert stub.alias_count_violation == [], \
        "activate 把条目写成了非两条 models：%s" % stub.alias_count_violation


# ---------------- ㉒（修订 5）：空 settings.json 等同文件不存在 ----------------

@pytest.mark.parametrize("raw,why", [("", "0 字节"), ("   \n\t\n", "纯空白"), ("{}", "空对象")])
def test_空settings视为不存在而不是不可解析(api, settings_path, raw, why):
    """㉒：里面没有用户内容可丢，R3-B 防的是"覆盖用户手写内容"，空文件不在该风险内。"""
    p = _mk(api)
    settings_path.write_text(raw, encoding="utf-8")
    code, body = api.post("/hub/api/provider/%s/activate" % p["id"])
    assert code == 200, "%s 被误判成 settings_unparsable：%s" % (why, body)
    assert read_settings(settings_path)["env"]["ANTHROPIC_MODEL"] == ALIAS


# ---------------- ㊴（修订 10）：并发 activate → 409 activate_busy，不排队 ----------------

def _hold_lock(a, app, pid):
    """按 §3.6 A0 的契约位置拿锁：app.extensions["hub_activate"]。

    锁是**懒建**的（首次 activate 时才挂上去），所以先成功切一次做热身 ——
    这不改变契约，只是测试要拿到那个对象。
    """
    assert a.post("/hub/api/provider/%s/activate" % pid)[0] == 200, "热身切换应成功"
    lock = (app.extensions or {}).get("hub_activate")
    assert lock is not None, ("§3.6 A0 要求互斥锁挂在 app.extensions['hub_activate']，"
                              "现有的键：%s" % sorted((app.extensions or {}).keys()))
    assert lock.acquire(blocking=False), "锁没被释放，前置条件不成立"
    return lock


def test_并发activate抢不到锁返回409_activate_busy(make_client, settings_path):
    from conftest import _Api
    c, app = make_client()
    a = _Api(c)
    p = _mk(a)
    lock = _hold_lock(a, app, p["id"])
    try:
        code, body = a.post("/hub/api/provider/%s/activate" % p["id"])
    finally:
        lock.release()
    assert code == 409, "并发切换应直接拒绝而不是排队，实得 %s" % code
    assert body["error"] == "activate_busy"
    assert body.get("detail"), "detail 要告诉用户稍候重试"


def test_锁释放后同一次activate就能成功(make_client, settings_path):
    from conftest import _Api
    c, app = make_client()
    a = _Api(c)
    p = _mk(a)
    lock = _hold_lock(a, app, p["id"])
    assert a.post("/hub/api/provider/%s/activate" % p["id"])[0] == 409
    lock.release()
    assert a.post("/hub/api/provider/%s/activate" % p["id"])[0] == 200


def test_同一把锁覆盖三个写settings的入口(make_client, settings_path, stub):
    """§3.6 A0 末句：本锁覆盖 §3.6 / §4.6 / §5 三个入口，它们互斥彼此。"""
    from conftest import _Api
    c, app = make_client()
    a = _Api(c)
    stub.seed_auth_file(email="alice@example.com")
    lock = _hold_lock(a, app, _mk(a)["id"])
    try:
        native = a.post("/hub/api/claude-native/activate")
        oauth = a.post("/hub/api/oauth/accounts/codex/%s/activate"
                       % stub.email_hash("alice@example.com"))
    finally:
        lock.release()
    assert native[0] == 409 and native[1]["error"] == "activate_busy", \
        "切回官方订阅没被同一把锁挡住：%s" % (native,)
    assert oauth[0] == 409 and oauth[1]["error"] == "activate_busy", \
        "OAuth 账户切换没被同一把锁挡住：%s" % (oauth,)
