# -*- coding: utf-8 -*-
"""provider 管理台验收测试夹具（黑盒）。

只依据 BRIEF-provider-hub.md 与 INTERFACE-provider-hub.md（修订 2）编写，
不读实现代码。实现未落地时本目录整体 error/fail —— 那是红基线，不是夹具坏了。

硬约束（INTERFACE §9.4）：
1. 绝不读写真实 ~/.claude/settings.json：autouse fixture 强制 CLAUDE_SETTINGS_PATH 指向 tmp，
   并在夹具里二次断言该路径不在真实 HOME 下（回落真实路径正是事故成因）。
2. 不打真实 cliproxy(8317)：一律用 stub_cliproxy（INTERFACE §10 冻结契约）。
3. 不监听端口：Flask app.test_client()（照抄 tests/imagegen_params/conftest.py 的理由）。
4. 全程假 key（sk-test-*），不联网，不起 cliproxy 二进制。
"""
import importlib
import json
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

REPO_ROOT = _ROOT
FAKE_KEY = "sk-test-0000000000000000000000"
FAKE_MGMT_KEY = "sk-test-mgmt-key-0000"
FAKE_DOWNSTREAM_KEY = "sk-test-downstream-0000"


def _load_app():
    """重新导入门户 Flask app（env 在导入期被读，所以每次改 env 都要重载）。

    黑盒探测入口：INTERFACE §0 只说"既有 Flask 进程 moments/web.py"，没写导出名。
    这里按 Flask 惯例依次试 app / application / create_app()，都没有就 fail，
    并把找到的属性名报出来，方便实现方对齐。
    """
    for name in ("moments.web",):
        mod = sys.modules.get(name)
        mod = importlib.reload(mod) if mod else importlib.import_module(name)
        for attr in ("app", "application"):
            obj = getattr(mod, attr, None)
            if obj is not None and hasattr(obj, "test_client"):
                return obj
        factory = getattr(mod, "create_app", None)
        if callable(factory):
            return factory()
    raise AssertionError(
        "moments.web 未导出可用的 Flask app（试过 app / application / create_app()）")


@pytest.fixture
def settings_path(tmp_path, monkeypatch):
    """把 settings.json 接缝钉死在 tmp 上，并守住"绝不回落真实路径"这条红线。"""
    p = tmp_path / "fakehome" / ".claude" / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(p))
    real = Path.home() / ".claude" / "settings.json"
    assert p.resolve() != real.resolve(), "CLAUDE_SETTINGS_PATH 指到了真实 settings.json"
    assert str(tmp_path) in str(p), "CLAUDE_SETTINGS_PATH 必须在 tmp_path 内"
    return p


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch, settings_path):
    """autouse：任何用例都不许继承外部 env 里的真实口令/端口/家目录。"""
    for k in ("HUB_ACCESS_PASSWORD", "HUB_ADMIN_PASSWORD", "HUB_ALLOW_PRIVATE_BASE_URL",
              "CLIPROXY_PORT", "CLIPROXY_MGMT_KEY", "CLIPROXY_API_KEY",
              "CLIPROXY_ANTHROPIC_COMPAT", "MOMENTS_WEB_HOST", "MOMENTS_WEB_PORT",
              "ACCESS_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MOMENTS_WEB_HOST", "127.0.0.1")
    monkeypatch.setenv("CLAUDE_TGBOT_HOME", str(tmp_path / "tgbot-home"))
    (tmp_path / "tgbot-home").mkdir(exist_ok=True)
    # §0.3：环境变量读不到时门户会回落直读 hub.env。测试必须用 HUB_ENV_FILE 指向 tmp，
    # 否则会读到真实 ~/.claude-tgbot/hub.env（里面是用户的真凭据）。默认指向一个不存在的文件。
    monkeypatch.setenv("HUB_ENV_FILE", str(tmp_path / "hub.env"))
    assert os.environ.get("CLAUDE_SETTINGS_PATH"), "CLAUDE_SETTINGS_PATH 未设 → 直接判该用例失败"


sys.path.insert(0, str(Path(__file__).resolve().parent))
from stub_cliproxy import StubCliproxy  # noqa: E402
from stub_dispatcher import StubDispatcher  # noqa: E402


@pytest.fixture
def stub():
    """按 INTERFACE §10 起的桩 cliproxy，随机端口，测试结束关闭。"""
    s = StubCliproxy(mgmt_key=FAKE_MGMT_KEY, downstream_key=FAKE_DOWNSTREAM_KEY)
    s.start()
    try:
        yield s
    finally:
        s.stop()


@pytest.fixture
def make_client(monkeypatch, stub):
    """(**env) -> (client, app)。每次调用按给定 env 重载门户，拿一个新的测试客户端。

    env 缺省：门关闭（无 HUB_ACCESS_PASSWORD）、cliproxy 指向桩、anthropic 兼容=1。
    """
    def _make(**env):
        base = {
            "CLIPROXY_PORT": str(stub.port),
            "CLIPROXY_MGMT_KEY": FAKE_MGMT_KEY,
            "CLIPROXY_API_KEY": FAKE_DOWNSTREAM_KEY,
            "CLIPROXY_ANTHROPIC_COMPAT": "1",
        }
        base.update({k: v for k, v in env.items() if v is not None})
        for k, v in base.items():
            monkeypatch.setenv(k, str(v))
        for k, v in env.items():
            if v is None:
                monkeypatch.delenv(k, raising=False)
        app = _load_app()
        app.config["TESTING"] = True
        return app.test_client(), app
    return _make


@pytest.fixture
def client(make_client):
    """默认客户端：门关闭（老用户零改造形态），可直达全部路由。"""
    c, _ = make_client()
    return c


@pytest.fixture
def api(client):
    """(code, body) 小工具，body 解不出 JSON 时给 {"_raw_text": ..., "_ctype": ...}。"""
    return _Api(client)


class _Api:
    def __init__(self, client):
        self.c = client

    def _wrap(self, r):
        return r.status_code, _body(r)

    def get(self, path, **kw):
        return self._wrap(self.c.get(path, **kw))

    def post(self, path, payload=None, **kw):
        if payload is None and "data" not in kw and "json" not in kw:
            payload = {}
        if payload is not None and "data" not in kw:
            kw["json"] = payload
        return self._wrap(self.c.post(path, **kw))

    def patch(self, path, payload=None, **kw):
        return self._wrap(self.c.patch(path, json=payload, **kw))

    def delete(self, path, **kw):
        return self._wrap(self.c.delete(path, **kw))


def _body(resp):
    try:
        j = resp.get_json()
    except Exception:
        j = None
    if j is None:
        return {"_raw_text": resp.get_data(as_text=True)[:800],
                "_ctype": resp.headers.get("Content-Type", "")}
    return j


def login(client, password=None, admin_password=None):
    """走 §1.5 的表单登录（application/x-www-form-urlencoded），返回原始响应。"""
    form = {}
    if password is not None:
        form["password"] = password
    if admin_password is not None:
        form["admin_password"] = admin_password
    return client.post("/login", data=form,
                       content_type="application/x-www-form-urlencoded")


def provider_payload(**over):
    """§3.3 的合法请求体，默认 openai 形态（S0-B 前枚举里只有它）。"""
    body = {"label": "DeepSeek", "kind": "openai",
            "base_url": "https://api.deepseek.com/v1", "api_key": FAKE_KEY,
            "upstream_model": "deepseek-chat",
            "model_alias": "claude-sonnet-4-5-20250929"}
    body.update(over)
    return {k: v for k, v in body.items() if v is not _OMIT}


_OMIT = object()
OMIT = _OMIT


def write_settings(path, obj):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def read_settings(path):
    p = Path(path)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


@pytest.fixture
def dispatcher_stub():
    """bot dispatcher 桩（§6.1：只认 POST /status）。"""
    s = StubDispatcher()
    s.start()
    try:
        yield s
    finally:
        s.stop()


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: 含真实等待（30s 超时 / 爆破窗口），默认 skip")
