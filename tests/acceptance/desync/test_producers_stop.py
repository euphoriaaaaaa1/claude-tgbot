"""需求⑤（公开 voicecall/server.py、moments/web.py、moments/hub_addbot.py）：
停用 bot 的生产者入口不 POST /ensure_worker、不写 inbox；set_enabled 幂等且不调 launchctl。
"""
import importlib
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from _helpers import write_cfg


class _Rec(BaseHTTPRequestHandler):
    hits = []

    def do_POST(self):
        _Rec.hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    do_GET = do_POST

    def log_message(self, *a):
        pass


@pytest.fixture
def server():
    _Rec.hits.clear()
    srv = HTTPServer(("127.0.0.1", 0), _Rec)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()


def _cfg(iso, bot, enabled, port):
    write_cfg(iso["cfg"], bot, enabled=enabled,
              extra=f"bot_channel_path: {iso['home']}/.claude/channels/{bot}\ndispatcher_port: {port}\nchat_id: 100\n")
    d = os.path.join(iso["home"], ".claude", "channels", bot)
    os.makedirs(os.path.join(d, "inbox"), exist_ok=True)
    with open(os.path.join(d, "access.json"), "w") as f:
        f.write('{"allowFrom": [100], "groups": {}}')
    return d


def _inbox(d):
    return os.listdir(os.path.join(d, "inbox"))


def _vc(registry):
    import voicecall.server as s
    return importlib.reload(s)


def _mw(registry):
    import moments.web as w
    return importlib.reload(w)


# ---------- voicecall ----------
def test_voicecall_write_inbox_停用bot_返回False_不写文件_stderr一行(iso, registry, server, capsys):
    d = _cfg(iso, "bot2", False, server.server_port)
    assert _vc(registry)._write_inbox("bot2", "合成文本", "voice-") is False
    assert _inbox(d) == []
    assert re.search(r"^\[\S+\] bot2 stopped, skip$", capsys.readouterr().err, re.M)


# 注：voicecall 启用路径的配置来源（chat_id/dispatcher_port 从哪读）契约未给，临时 configs 下报"未配 chat_id"，
# 故不写启用侧正向对照；停用侧靠 stderr 的 "stopped, skip" 行区分。


def test_voicecall_ensure_worker_停用bot_不POST_stderr一行(iso, registry, server, capsys):
    _cfg(iso, "bot2", False, server.server_port)
    assert _vc(registry)._ensure_worker_alive("bot2") is None
    assert _Rec.hits == []
    assert not os.path.exists(iso["calls"])
    assert re.search(r"^\[\S+\] bot2 stopped, skip$", capsys.readouterr().err, re.M)


def test_voicecall_中文bot名停用_不写文件(iso, registry, server):
    d = _cfg(iso, "陈璐璐", False, server.server_port)
    assert _vc(registry)._write_inbox("陈璐璐", "合成", "voice-") is False and _inbox(d) == []


# ---------- moments/web ----------
def test_moments_web_ensure_worker_停用bot_不POST_不写inbox_stderr一行(iso, registry, server, capsys):
    d = _cfg(iso, "bot3", False, server.server_port)
    assert _mw(registry)._ensure_worker_alive("bot3", "100", d) is None
    assert _Rec.hits == [] and _inbox(d) == []
    assert re.search(r"^\[\S+\] bot3 stopped, skip$", capsys.readouterr().err, re.M)


def test_moments_web_ensure_worker_启用bot_有POST_正向对照(iso, registry, server):
    d = _cfg(iso, "bot3", True, server.server_port)
    _mw(registry)._ensure_worker_alive("bot3", "100", d)
    assert any(p.rstrip("/").endswith("/ensure_worker") for p in _Rec.hits), _Rec.hits


# ---------- hub_addbot.set_enabled ----------
def _hub(registry):
    import moments.hub_addbot as h
    return importlib.reload(h)


def _enabled_in_yml(iso, bot):
    import yaml
    with open(os.path.join(iso["cfg"], f"{bot}.yml"), encoding="utf-8") as f:
        return yaml.safe_load(f).get("enabled")


def test_set_enabled_False两次_幂等_响应相同_yml为false(iso, registry):
    _cfg(iso, "bot2", True, 1)
    h = _hub(registry)
    r1 = h.set_enabled("bot2", False)
    r2 = h.set_enabled("bot2", False)
    assert r1 == r2 and _enabled_in_yml(iso, "bot2") is False
    assert "bot2" in registry.disabled_ids_safe()


def test_set_enabled_True_恢复_不调launchctl(iso, registry):
    _cfg(iso, "bot2", False, 1)
    h = _hub(registry)
    h.set_enabled("bot2", True)
    assert _enabled_in_yml(iso, "bot2") is True
    assert "bot2" not in registry.disabled_ids_safe()
    assert not os.path.exists(iso["calls"]), open(iso["calls"]).read() if os.path.exists(iso["calls"]) else ""


def test_set_enabled_True两次_幂等_yml仍为true(iso, registry):
    _cfg(iso, "bot2", False, 1)
    h = _hub(registry)
    assert h.set_enabled("bot2", True) == h.set_enabled("bot2", True)
    assert _enabled_in_yml(iso, "bot2") is True
