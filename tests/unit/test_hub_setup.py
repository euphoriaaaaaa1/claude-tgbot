# -*- coding: utf-8 -*-
"""第 7 段 · /hub/setup 密钥初始化向导（网页版 scripts/setup_keys.sh）。

跑：python3 -m pytest tests/unit/test_hub_setup.py -q

盯三件事：
  1. 写盘走的是 scripts/setup_keys.py 那一份（不是本段复制粘贴的第二份逻辑），
     且格式校验与 setup_keys.sh 一个口径；
  2. **响应体里一个字节的密钥值都不许出现**（成功和失败两条路径都验）；
  3. /hub 前缀自动落在 hub_auth 的门内，本段不自建鉴权。

不起 moments.web（会拖进 db / 朋友圈路由），只把蓝图挂到裸 Flask 上；
落盘全在 tmp_path 沙箱里 —— `_sk_paths` 被换掉，绝不碰机主真实的
configs/_global.yml 与 ~/.claude/channels/。
"""
import json
import os
import sys
from pathlib import Path

import pytest
from flask import Flask

from moments import hub_auth, hub_routes
from moments.hub_routes import hub_bp

TEMPLATES = Path(__file__).resolve().parents[2] / "moments" / "templates"
ROOT = Path(__file__).resolve().parents[2]

GOOD = {
    "token": "123456789:AAcanary-token-value-abcdefghij",
    "userid": "987654321",
    "deepseek": "sk-canaryvaluexxxxxxxxxx",
}
BAD = {
    "token": "not-a-token-canaryvalue",
    "userid": "canaryvalue12",
    "deepseek": "sk-canary",          # 短于 16 位
}


@pytest.fixture
def sandbox(tmp_path):
    """与 test_setup_keys.py 同形的三个目标文件（装完 install.sh 的样子，三项都还没填）。"""
    repo = tmp_path / "repo"
    (repo / "configs").mkdir(parents=True)
    (repo / "configs" / "_global.yml").write_text(
        "jiwen:\n  delta_llm:                   # 🔴 独立于主模型\n"
        "    base_url: https://api.deepseek.com/anthropic\n"
        '    api_key: "YOUR_DEEPSEEK_API_KEY"   # ← 填你自己的 key\n'
        "    model: deepseek-v4-flash\n",
        encoding="utf-8")
    bot = tmp_path / "home" / ".claude" / "channels" / "chenlulu"
    bot.mkdir(parents=True)
    (bot / ".env").write_text("# 注释\nTELEGRAM_BOT_TOKEN=123456789:AA-your-telegram-bot-token-here\n",
                              encoding="utf-8")
    (bot / "access.json").write_text(json.dumps(
        {"dmPolicy": "allowlist", "allowFrom": ["YOUR_TELEGRAM_USER_ID"], "groups": {}},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return repo, tmp_path / "home"


def _app(monkeypatch, repo, home):
    monkeypatch.setattr(hub_routes, "_sk_paths",
                        lambda: hub_routes.setup_keys.key_paths("chenlulu", repo, home))
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.register_blueprint(hub_bp)
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(sandbox, monkeypatch):
    return _app(monkeypatch, *sandbox).test_client()


def post(client, **body):
    return client.post("/hub/api/setup/keys", json=body)


def status(client):
    r = client.get("/hub/api/setup/status")
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()


# ── status ───────────────────────────────────────────────────────────
def test_status三项全是布尔且初始都没填(client):
    st = status(client)
    assert set(st) == {"deepseek", "token", "userid"}
    assert all(v is False for v in st.values()), st


def test_status不回显任何值只回布尔(client, sandbox):
    """占位符也好真 key 也好，status 只该说"填了没"。"""
    repo, _ = sandbox
    body = client.get("/hub/api/setup/status").get_data(as_text=True)
    assert "YOUR_DEEPSEEK_API_KEY" not in body and "api_key" not in body


# ── 写入成功 ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("field", ["token", "userid", "deepseek"])
def test_写入成功回ok并把该项翻成已填(client, field):
    r = post(client, field=field, value=GOOD[field])
    assert r.status_code == 200, r.get_data(as_text=True)
    b = r.get_json()
    assert b["ok"] is True
    assert b["filled"][field] is True
    assert status(client)[field] is True


def test_写入落到setup_keys那三个文件里(client, sandbox):
    repo, home = sandbox
    bot = home / ".claude" / "channels" / "chenlulu"
    for field in ("token", "userid", "deepseek"):
        assert post(client, field=field, value=GOOD[field]).status_code == 200
    env = (bot / ".env").read_text(encoding="utf-8")
    assert env.count("TELEGRAM_BOT_TOKEN=") == 1 and GOOD["token"] in env
    assert env.startswith("# 注释")                                   # 原文件内容没被冲掉
    acc = json.loads((bot / "access.json").read_text(encoding="utf-8"))
    assert acc["allowFrom"] == [GOOD["userid"]] and acc["dmPolicy"] == "allowlist"
    yml = (repo / "configs" / "_global.yml").read_text(encoding="utf-8")
    assert 'api_key: "%s"' % GOOD["deepseek"] in yml
    assert "🔴 独立于主模型" in yml                                    # YAML 注释保住
    assert status(client) == {"token": True, "userid": True, "deepseek": True}


# ── 校验拒绝 ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("field", ["token", "userid", "deepseek"])
def test_格式不对回400_bad_format且不落盘(client, field, sandbox):
    repo, home = sandbox
    before = {p: p.read_bytes() for p in (
        home / ".claude" / "channels" / "chenlulu" / ".env",
        home / ".claude" / "channels" / "chenlulu" / "access.json",
        repo / "configs" / "_global.yml")}
    r = post(client, field=field, value=BAD[field])
    assert r.status_code == 400, r.get_data(as_text=True)
    assert r.get_json()["error"] == "bad_format"
    assert r.get_json()["detail"] == hub_routes.setup_keys.FIELD_FMT[field]
    assert status(client)[field] is False
    for p, raw in before.items():
        assert p.read_bytes() == raw, "校验失败却动了 %s" % p.name


def test_未知字段回400_bad_field(client):
    r = post(client, field="anthropic_key", value="sk-whatever-1234567890")
    assert r.status_code == 400 and r.get_json()["error"] == "bad_field"


@pytest.mark.parametrize("body", [None, [], "x", 3])
def test_请求体不是对象回400_bad_body(client, body):
    r = client.post("/hub/api/setup/keys", json=body)
    assert r.status_code == 400 and r.get_json()["error"] == "bad_body"


def test_value不是字符串回400_bad_body(client):
    assert post(client, field="userid", value=987654321).get_json()["error"] == "bad_body"


def test_配置还没铺就写回409_not_provisioned(monkeypatch, tmp_path):
    """没跑过 install.sh：目标文件根本不存在，得说清楚，不能报成 500。"""
    c = _app(monkeypatch, tmp_path / "nope-repo", tmp_path / "nope-home").test_client()
    for field in ("token", "userid", "deepseek"):
        r = post(c, field=field, value=GOOD[field])
        assert r.status_code == 409, (field, r.get_data(as_text=True))
        assert r.get_json()["error"] == "not_provisioned"


def test_yaml结构被改过回409而不是把进程带走(client, sandbox):
    """set_deepseek 原来抛 SystemExit（BaseException）—— 在 Flask 里会绕过 except Exception。"""
    repo, _ = sandbox
    (repo / "configs" / "_global.yml").write_text("jiwen:\n  other: 1\n", encoding="utf-8")
    r = post(client, field="deepseek", value=GOOD["deepseek"])
    assert r.status_code == 409 and r.get_json()["error"] == "write_failed"


# ── 红线：响应里绝不出现密钥值 ────────────────────────────────────────
@pytest.mark.parametrize("field", ["token", "userid", "deepseek"])
def test_成功响应不含密钥值(client, field):
    body = post(client, field=field, value=GOOD[field]).get_data(as_text=True)
    assert "canary" not in body.lower(), body
    assert GOOD[field] not in body


@pytest.mark.parametrize("field", ["token", "userid", "deepseek"])
def test_失败响应不含用户输入的值(client, field):
    """校验失败最容易顺手把"你填的是 xxx"回显出去 —— 那就是把密钥写进了日志与响应。"""
    body = post(client, field=field, value=BAD[field]).get_data(as_text=True)
    assert "canary" not in body.lower(), body


# ── 权限门（不自建鉴权，靠 /hub 前缀落进 hub_auth）────────────────────
@pytest.fixture
def gated(sandbox, monkeypatch):
    monkeypatch.setenv("HUB_ACCESS_PASSWORD", "hub-access-pw-12345")
    monkeypatch.delenv("HUB_ADMIN_PASSWORD", raising=False)
    app = _app(monkeypatch, *sandbox)
    hub_auth.install(app)
    return app.test_client()


@pytest.mark.parametrize("method,path", [
    ("get", "/hub/api/setup/status"),
    ("post", "/hub/api/setup/keys"),
])
def test_没登录时API回401并带HubGate头(gated, method, path):
    r = getattr(gated, method)(path, headers={"Accept": "*/*"}, json={"field": "userid", "value": "987654321"})
    assert r.status_code == 401
    assert r.headers.get("Hub-Gate") == "1"


def test_没登录时页面跳登录而不是裸奔(gated):
    r = gated.get("/hub/setup", headers={"Accept": "text/html"})
    assert r.status_code == 302 and "/login" in r.headers["Location"]


# ── 页面本身 ─────────────────────────────────────────────────────────
def test_页面恒200且不碰磁盘(monkeypatch, tmp_path):
    """§2 铁律：依赖全坏（目录都不存在）也得回 200 HTML，状态由前端 API 取。"""
    c = _app(monkeypatch, tmp_path / "nope-repo", tmp_path / "nope-home").test_client()
    r = c.get("/hub/setup")
    assert r.status_code == 200 and "密钥" in r.get_data(as_text=True)


def test_页面三项齐全且输入框是password型(client):
    body = client.get("/hub/setup").get_data(as_text=True)
    for field in ("token", "userid", "deepseek"):
        assert 'data-field="%s"' % field in body
    assert body.count('type="password"') == 3
    assert ".innerHTML" not in body, "页面用了 innerHTML —— 结果区必须 textContent 渲染"


def test_导航里有密钥入口且未填齐时会报警(client):
    nav = (TEMPLATES / "_nav.html").read_text(encoding="utf-8")
    assert 'href="/hub/setup"' in nav
    assert "/hub/api/setup/status" in nav, "导航没有未填齐提示"
    body = client.get("/hub/setup").get_data(as_text=True)
    assert body.count('class="chip on"') == 1, "nav_active 没传对"


# ── 与终端向导同源 ───────────────────────────────────────────────────
def test_三条校验正则与setup_keys_sh逐字一致():
    """网页和终端两个向导必须一个口径 —— 两边各改各的就会出现"命令行填得进、网页填不进"。"""
    sh = (ROOT / "scripts" / "setup_keys.sh").read_text(encoding="utf-8")
    for field, rx in hub_routes.setup_keys.FIELD_RE.items():
        assert rx.pattern in sh, "%s 的正则与 setup_keys.sh 漂移了：%s" % (field, rx.pattern)


def test_路由段没有自己复制一份写盘逻辑():
    """写盘只许有 scripts/setup_keys.py 那一份。"""
    import inspect
    src = inspect.getsource(hub_routes.setup_write_key)
    assert "setup_keys.write_key" in src
    for forbidden in ("open(", "write_text", "json.dumps"):
        assert forbidden not in src, "第 7 段自己动手写文件了：%s" % forbidden


def test_安装脚本与README都指了网页入口():
    for f in ("install.sh", "README.md"):
        assert "/hub/setup" in (ROOT / f).read_text(encoding="utf-8"), "%s 没说网页也能填" % f


# ── as_provider：同一把 DeepSeek key 顺带当对话模型 ─────────────────────
#
# 三条分支各一个用例（成功 / 同名已存在只换 key / 激活失败），外加"不勾就是现行为"
# 和红线"key 不回显"。provider 那一半全走第 2 段的既有函数，所以这里要盯的不是
# CRUD 本身（那是 test_hub_routes_provider.py 的活），而是**联动的接缝**：
# 建没建、切没切、失败了会不会把已经写好的 jiwen key 一起赔进去。
sys.path.insert(0, str(ROOT / "tests" / "acceptance" / "provider_hub"))
from stub_cliproxy import StubCliproxy  # noqa: E402

MGMT, DOWNSTREAM = "sk-test-mgmt-0000", "sk-test-downstream-0000"
PRESET = hub_routes.DEEPSEEK_PRESET


@pytest.fixture
def stub():
    s = StubCliproxy(mgmt_key=MGMT, downstream_key=DOWNSTREAM).start()
    try:
        yield s
    finally:
        s.stop()


@pytest.fixture
def linked(sandbox, monkeypatch, stub, tmp_path):
    """setup 沙箱 + 桩 cliproxy。DNS 也换成假的 —— 单测不许联网（conftest 同款口径）。"""
    monkeypatch.setenv("CLIPROXY_PORT", str(stub.port))
    monkeypatch.setenv("CLIPROXY_MGMT_KEY", MGMT)
    monkeypatch.setenv("CLIPROXY_API_KEY", DOWNSTREAM)
    monkeypatch.setenv("CLIPROXY_ANTHROPIC_COMPAT", "1")
    monkeypatch.setattr(hub_routes.provider_model, "resolve_host", lambda _h: [])
    return _app(monkeypatch, *sandbox).test_client()


def _settings():
    return json.loads(Path(os.environ["CLAUDE_SETTINGS_PATH"]).read_text(encoding="utf-8"))


def test_勾了as_provider就把同一把key建成provider并切过去(linked, stub):
    r = post(linked, field="deepseek", value=GOOD["deepseek"], as_provider=True)
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()["ok"] is True and "provider_error" not in r.get_json()
    # 建了一条 anthropic 条目（claude-api-key 块），值全来自 DEEPSEEK_PRESET
    assert stub.openai_compat == [] and stub.gemini_api_key == []
    assert len(stub.claude_api_key) == 1
    blk = stub.claude_api_key[0]
    assert blk["base-url"] == PRESET["base_url"] and blk["api-key"] == GOOD["deepseek"]
    assert [m["alias"] for m in blk["models"]] == [
        PRESET["model_alias"], hub_routes.provider_model.DEFAULT_HAIKU_ALIAS]
    assert all(m["name"] == PRESET["upstream_model"] for m in blk["models"])
    # 切过去了：settings.json 指向本机 cliproxy + 该 alias，且当选条目不带 prefix
    env = _settings()["env"]
    assert env["ANTHROPIC_MODEL"] == PRESET["model_alias"]
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:%d" % stub.port
    assert stub.aliases_enabled(PRESET["model_alias"]) == 1
    # jiwen 那把也照常写了（联动不许把原本这件事顶掉）
    assert status(linked)["deepseek"] is True


def test_同名provider已存在时只换key不重复建(linked, stub, sandbox):
    old = "sk-oldcanaryvaluexxxxxxx"
    assert post(linked, field="deepseek", value=old, as_provider=True).status_code == 200
    assert stub.claude_api_key[0]["api-key"] == old
    r = post(linked, field="deepseek", value=GOOD["deepseek"], as_provider=True)
    assert r.status_code == 200 and "provider_error" not in r.get_json()
    assert len(stub.claude_api_key) == 1, "同一个来源被建了第二遍"
    assert stub.claude_api_key[0]["api-key"] == GOOD["deepseek"]
    assert stub.aliases_enabled(PRESET["model_alias"]) == 1


def test_激活失败不回滚jiwen_key且如实回provider_error(linked, stub, sandbox):
    repo, _ = sandbox
    stub.set_down()                      # cliproxy 连不上 → 创建那一步就 503
    r = post(linked, field="deepseek", value=GOOD["deepseek"], as_provider=True)
    assert r.status_code == 200, r.get_data(as_text=True)
    b = r.get_json()
    assert b["ok"] is True and b["filled"]["deepseek"] is True
    assert b["provider_error"] == "cliproxy_down"
    # 红线：切模型失败不许把已经写对的 key 抹掉
    assert 'api_key: "%s"' % GOOD["deepseek"] in \
        (repo / "configs" / "_global.yml").read_text(encoding="utf-8")


def test_不勾就只写jiwen不碰cliproxy和settings(linked, stub, tmp_path):
    for body in ({}, {"as_provider": False}):
        assert post(linked, field="deepseek", value=GOOD["deepseek"], **body).status_code == 200
    assert stub.all_blocks == []
    assert not Path(os.environ["CLAUDE_SETTINGS_PATH"]).exists()


@pytest.mark.parametrize("down", [False, True])
def test_联动路径的响应同样不含key(linked, stub, down):
    if down:
        stub.set_down()
    body = post(linked, field="deepseek", value=GOOD["deepseek"], as_provider=True) \
        .get_data(as_text=True)
    assert "canary" not in body.lower(), body


def test_预置常量里没有api_key字段():
    """模板常量不许带 key —— 带了就等于往 cliproxy 里预埋一条无主凭据。"""
    assert "api_key" not in PRESET and set(PRESET) == {
        "label", "kind", "base_url", "upstream_model", "model_alias"}
