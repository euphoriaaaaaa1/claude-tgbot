# -*- coding: utf-8 -*-
"""pub-batch3b 验收夹具：本地 Flask 壳渲染仓内 console.html，页面所有 /api 请求由 Playwright page.route 伪造。

不 import 任何被测内部模块，只通过浏览器打开 /console 观察页面行为。
夹具名一律用 alpha/beta 这类通用名，不出现任何真实 bot 名或私有路径。
"""
import json
import re
import socket
import threading
from pathlib import Path
from urllib.parse import urlparse

import pytest

REPO = Path(__file__).resolve().parents[3]
TEMPLATES = REPO / "moments" / "templates"

# 生产端口：本进程一个都不许连（与 pub_memo_restart 同一护栏）
PROD_PORTS = set(range(17801, 17809)) | {7788, 7897, 8767}

CAPS_ALL = {
    "bot.profile": True,
    "bot.enable": True,
    "bot.life_settings": True,
    "bot.persona": True,
    "bot.voice": True,
    "bot.features": True,
    "bot.restart.graceful": True,
    "bot.archive": True,
    "bot.delete": True,
    "bot.group_chat": True,
}

WRITE_BUTTON_IDS = ["displayNameSave", "toggleEnabled", "lifeSave", "personaSave", "voiceSave"]


def backend(name="claude", label="Claude", status="ok", caps=None):
    return {
        "backend": name,
        "label": label,
        "status": status,
        "capabilities": dict(CAPS_ALL if caps is None else caps),
        "default": {},
    }


def bot(backend_name="claude", bot_id="alpha", name="Alpha", **kw):
    row = {
        "backend": backend_name,
        "bot_id": bot_id,
        "display_name": name,
        "enabled": True,
        "state": "running",
    }
    row.update(kw)
    return row


def overview(bots, backends=None):
    return {"backends": backends or [backend()], "bots": bots}


def default_reads(bot_id="alpha"):
    """按 bot id 生成不同内容：切到另一只后若还显示上一只的内容，就说明没重新加载。"""
    return {
        "profile": (200, {"ok": True, "display_name": bot_id.capitalize()}),
        "life": (200, {
            "ok": True,
            "fields": [{"key": "sleep_hours", "label": "睡眠", "type": "text"}],
            "values": {"sleep_hours": "23:00-" + bot_id},
        }),
        "persona": (200, {"ok": True, "text": "人设-" + bot_id, "version": "1"}),
        "voice": (200, {"ok": True, "voice_id": "voice-" + bot_id, "tts_model": "s1",
                        "text_language": "zh", "voice_language": "zh"}),
        "features": (200, {"ok": True, "keys": [{"key": "feat_" + bot_id, "label": "功能-" + bot_id}],
                           "features": {"feat_" + bot_id: True}, "requirements": {}}),
    }

DELETED = (404, {"ok": False, "code": "unknown_bot"})
BACKEND_DOWN = (502, {"ok": False, "code": "backend_down", "message": "底层连不上"})


def reads_with(**overrides):
    """只给出要覆盖的读接口；未覆盖的按 default_reads(bot_id) 生成。值是 (status, body) 元组。"""
    return dict(overrides)


def _build_shell():
    from flask import Flask, render_template

    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.config["TESTING"] = True

    @app.get("/console")
    def console_page():  # 只渲染页面壳，数据全部走 /api
        return render_template("console.html", tts_models=["s1", "s2-pro"])

    return app


@pytest.fixture(autouse=True)
def no_prod_port_connect(monkeypatch):
    hits = []
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def _watch(addr):
        if isinstance(addr, tuple) and len(addr) >= 2 and addr[1] in PROD_PORTS:
            hits.append(addr)
            return True
        return False

    def guarded_connect(self, addr):
        if _watch(addr):
            raise ConnectionRefusedError("测试护栏：不许连生产端口 %s" % (addr,))
        return real_connect(self, addr)

    def guarded_connect_ex(self, addr):
        if _watch(addr):
            return 111
        return real_connect_ex(self, addr)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    yield
    assert hits == [], "测试连了生产端口: %s" % hits


@pytest.fixture(scope="module")
def base_url():
    from werkzeug.serving import make_server

    srv = make_server("127.0.0.1", 0, _build_shell(), threaded=True)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % srv.server_port
    finally:
        srv.shutdown()
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def chrome():
    # 仓里没声明 playwright 依赖、CI 也不装：缺依赖或本机没装 Chrome 时整批按环境跳过。
    sync_api = pytest.importorskip("playwright.sync_api", reason="未安装 playwright")
    pw = sync_api.sync_playwright().start()
    try:
        browser = pw.chromium.launch(channel="chrome", headless=True)
    except Exception as e:  # noqa: BLE001  驱动/浏览器缺失
        pw.stop()
        pytest.skip("未安装 playwright 或 Chrome：%s" % type(e).__name__)
    try:
        yield browser
    finally:
        browser.close()
        pw.stop()


class ConsoleShell:
    """一个隔离浏览器上下文 + 伪造的所有 /api 响应。"""

    def __init__(self, page, base, data, reads=None):
        self.page = page
        self.base = base
        self.data = data
        self.reads = reads_with(**(reads or {}))
        self.requests = []      # [(method, url)]，含全部 /api 请求
        self.gets = []

    def install(self):
        self.page.route("**/api/**", self._handle)

    def _handle(self, route):
        req = route.request
        path = urlparse(req.url).path
        self.requests.append((req.method, req.url))
        if path == "/api/console/overview":
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps(self.data))
        m = re.match(r"^/api/console/bots/[^/]+/([^/]+)/(profile|life|persona|voice|features)$", path)
        if m:
            bot_id, kind = m.group(1), m.group(2)
            status, body = self.reads.get(kind) or default_reads(bot_id)[kind]
            return route.fulfill(status=status, content_type="application/json",
                                 body=json.dumps(body))
        if path == "/api/console/archives":
            body = {"ok": True, "backends": [], "archives": []}
        elif path == "/api/console/ops":
            body = {"ok": True, "ops": []}
        else:
            body = {"ok": True}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def open(self, query=""):
        self.page.goto(self.base + "/console" + query)
        self.wait_detail()

    def wait_detail(self):
        self.page.wait_for_function(
            "() => {const h = document.getElementById('loadingHint');"
            " return !!(h && h.hidden);}")
        self.page.wait_for_function(
            "() => {const h = document.getElementById('blkHead'); return !!(h && !h.hidden);}")

    def wait_for(self, js_expr, timeout=5000):
        self.page.wait_for_function("() => (" + js_expr + ")", timeout=timeout)

    def wait_reads(self, tail="persona"):
        """等详情块读接口的请求发出去并留一点渲染时间（不依赖被测行为本身）。"""
        for _ in range(60):
            if any(("/" + tail) in url for _m, url in self.requests):
                break
            self.page.wait_for_timeout(50)
        self.page.wait_for_timeout(150)

    def writes(self):
        return [(m, u) for m, u in self.requests if m in ("PUT", "POST", "PATCH", "DELETE")]

    def api_writes(self):
        return [(m, u) for m, u in self.requests
                if m in ("PUT", "POST", "PATCH", "DELETE") and "/api/console/" in u]


@pytest.fixture
def open_console(chrome, base_url):
    contexts = []

    def _open(data, reads=None, query=""):
        ctx = chrome.new_context(viewport={"width": 1360, "height": 1000})
        contexts.append(ctx)
        page = ctx.new_page()
        shell = ConsoleShell(page, base_url, data, reads)
        shell.install()
        shell.open(query)
        return shell

    yield _open
    for ctx in contexts:
        ctx.close()


# ── 页面观察工具（都只看 DOM） ───────────────────────────────────────────
JS_VISIBLE_IDS = """(ids) => ids.filter((id) => {
  const el = document.getElementById(id);
  return !!el && !el.hidden && el.offsetParent !== null;
})"""


def visible_write_ids(page):
    """可见的写入口 id/选择器：五个保存按钮 + 生命周期区 + 功能开关 + 卡片重启。"""
    ids = page.evaluate(JS_VISIBLE_IDS, WRITE_BUTTON_IDS + ["lifecycleFoot"])
    feats = page.locator('[data-role="feature-toggle"]:visible').count()
    restarts = page.locator('[data-action="restart"]:visible').count()
    if feats:
        ids.append('feature-toggle*%d' % feats)
    if restarts:
        ids.append('card-restart*%d' % restarts)
    return ids


def enabled_write_ids(page):
    """可见且未禁用的写入口：上述按钮里未禁用者 + 功能开关 + 卡片重启。"""
    js = """() => %s.filter((id) => {
      const el = document.getElementById(id);
      return !!el && !el.hidden && el.offsetParent !== null && !el.disabled;
    })""" % json.dumps(WRITE_BUTTON_IDS + ["lifecycleFoot"])
    ids = list(page.evaluate(js))
    feats = page.locator('[data-role="feature-toggle"]:visible:not([disabled])').count()
    restarts = page.locator('[data-action="restart"]:visible:not([disabled])').count()
    if feats:
        ids.append('feature-toggle*%d' % feats)
    if restarts:
        ids.append('card-restart*%d' % restarts)
    return ids


def banner_count(page):
    return page.locator('#roBanner').count()


def banner_visible(page):
    loc = page.locator('#roBanner')
    return loc.count() > 0 and loc.first.is_visible()


def banner_text(page):
    loc = page.locator('#roBanner')
    return loc.first.inner_text() if loc.count() else ''


def detail_has_ro(page):
    return page.evaluate("() => document.getElementById('detail').classList.contains('ro')")


def jump_visible(page):
    loc = page.locator('#roJump')
    return loc.count() > 0 and loc.first.is_visible()
