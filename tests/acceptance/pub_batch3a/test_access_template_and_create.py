# -*- coding: utf-8 -*-
"""pub-batch3a：access.json 模板与新建 bot 补缺省（行为清单 16、17）。

16 读仓里的模板文件；17 走适配器的 create 入口（照 tests/acceptance/pub_parity/test_create_bot_hooks.py 的夹具写法）：
只桩掉"开机自启/占端口/拉 dispatcher"这类外部副作用，其余是真实流程。
全部在临时 HOME / configs / channels 下，dispatcher 是回环上的假 HTTP 服务；令牌为假值。
"""
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

import adapters.claude_create as claude_create  # noqa: E402
from adapters.claude_local import ClaudeLocalAdapter  # noqa: E402
from adapters.guard import SecretToken  # noqa: E402

TELEGRAM_TOKEN = "123456789:" + "A" * 35
TEMPLATE_PATH = Path(REPO) / "channels" / "_persona_template" / "access.json"
DEFAULTS = {"splitOnParagraph": True, "paragraphDelay": 1200, "burstWindowMs": 8000, "burstMaxMs": 0}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        if self.path == "/status":
            body = {"ok": True, "bot": self.server.bot_id, "phase": "running", "pid": 999999, "queue_depth": 0}
        elif self.path == "/ensure_worker":
            body = {"ok": True, "running": True, "spawned": False}
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.fixture
def home(tmp_path, monkeypatch):
    h, configs, channels = tmp_path / "home", tmp_path / "configs", tmp_path / "channels"
    for p in (h, configs, channels):
        p.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(configs))
    monkeypatch.setenv("HUB_CHANNELS_DIR", str(channels))
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.setenv("HUB_ENV_FILE", str(tmp_path / "hub.env"))
    monkeypatch.setenv("CLAUDE_TGBOT_HOME", str(tmp_path / "tgbot-home"))
    stub_dir = tmp_path / "fake-service-bin"
    stub_dir.mkdir()
    for name in ("launchctl", "systemctl", "schtasks"):
        s = stub_dir / name
        s.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        s.chmod(0o755)
    monkeypatch.setenv("PATH", str(stub_dir) + os.pathsep + os.environ.get("PATH", ""))
    return {"home": h, "configs": configs, "channels": channels}


_ABSENT = object()


def _make_templates(home, template_access):
    """template_access: _ABSENT=模板里没有 access.json；dict=写成 JSON；str=原样写（用来造损坏文件）。"""
    (home["home"] / ".claude.json").write_text(
        json.dumps({"projects": {str(home["channels"]): {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
    root = home["configs"] / "templates" / "claude" / "blank"
    (root / "skills" / "voice").mkdir(parents=True)
    (root / "template.json").write_text(json.dumps({"id": "blank", "label": "空白"}), encoding="utf-8")
    (root / "CLAUDE.md").write_text("你是测试 bot。\n", encoding="utf-8")
    (root / "settings.json").write_text("{}", encoding="utf-8")
    (root / "skills" / "voice" / "SKILL.md").write_text("# 语音技能\n", encoding="utf-8")
    if template_access is not _ABSENT:
        raw = template_access if isinstance(template_access, str) else json.dumps(template_access)
        (root / "access.json").write_text(raw, encoding="utf-8")


def _spec(bot_id):
    return {"bot_id": bot_id, "display_name": "新 Bot", "telegram_username": "new_bot",
            "telegram_user_id": 12345, "chat_id": "987654321", "persona_template": "blank",
            "voice": {"voice_id": "v1", "tts_model": "s2-pro"}, "features": ["voice", "groups"],
            "enabled": True, "op_id": "create-test-1", "confirm_other_side_unknown": False,
            "telegram_token": TELEGRAM_TOKEN}


def _create_and_read_access(home, monkeypatch, bot_id, template_access):
    _make_templates(home, template_access)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.bot_id = bot_id
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(claude_create, "allocate_port", lambda ctx: httpd.server_address[1])
        monkeypatch.setattr(claude_create, "_install_schedule", lambda *a, **k: "ok")
        monkeypatch.setattr(claude_create, "_uninstall_schedule", lambda *a, **k: None)
        monkeypatch.setattr(claude_create, "_ensure_dispatcher", lambda *a, **k: None)
        ClaudeLocalAdapter().create(_spec(bot_id), SecretToken(TELEGRAM_TOKEN))
    finally:
        httpd.shutdown()
        httpd.server_close()
    path = home["channels"] / bot_id / "access.json"
    assert path.is_file(), f"新建后应有 {path}"
    return json.loads(path.read_text(encoding="utf-8"))


def _picked(access):
    return {k: access.get(k, "<缺>") for k in DEFAULTS}


def test_16_仓内模板access_json_含攒波两键_且原有分段两键仍在():
    tpl = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    assert tpl.get("burstWindowMs") == 8000
    assert tpl.get("burstMaxMs") == 0
    assert tpl.get("splitOnParagraph") is True
    assert tpl.get("paragraphDelay") == 1200


def test_17a_用没有access_json的空白模板新建_四个值齐全(home, monkeypatch):
    access = _create_and_read_access(home, monkeypatch, "newbota", _ABSENT)
    assert _picked(access) == DEFAULTS


def test_17b_模板已给的paragraphDelay和burstMaxMs_新建后保留原值_缺的补缺省(home, monkeypatch):
    access = _create_and_read_access(home, monkeypatch, "newbotb",
                                     {"allowFrom": [], "paragraphDelay": 500, "burstMaxMs": 30000})
    assert _picked(access) == {"splitOnParagraph": True, "paragraphDelay": 500, "burstWindowMs": 8000, "burstMaxMs": 30000}


def test_17c_模板access_json损坏_按空对象处理再补四个缺省(home, monkeypatch):
    access = _create_and_read_access(home, monkeypatch, "newbotc", "{not json")
    assert _picked(access) == DEFAULTS
