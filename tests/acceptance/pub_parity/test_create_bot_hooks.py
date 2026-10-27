# -*- coding: utf-8 -*-
"""pub-parity：新建 bot 写出的项目设置自带四条 SessionStart 钩子（行为清单 17）。

调用方式照 tests/unit/test_adapters_claude.py::_create_with_template_settings：
走适配器的 create 入口；只桩掉"开机自启/占端口/拉 dispatcher"这类外部副作用，其余是真实流程。
全部在临时 HOME / configs / channels 下，dispatcher 是回环上的假 HTTP 服务。
"""
import json
import os
import shlex
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from pp_helpers import FOUR_HOOKS, REPO, script_path

if REPO not in sys.path:
    sys.path.insert(0, REPO)

import adapters.claude_create as claude_create  # noqa: E402
from adapters.claude_local import ClaudeLocalAdapter  # noqa: E402
from adapters.guard import SecretToken  # noqa: E402

API_TOKEN = "test-api-token-" + "0" * 40
TELEGRAM_TOKEN = "123456789:" + "A" * 35


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


def _make_templates(home, template_settings):
    (home["home"] / ".claude.json").write_text(
        json.dumps({"projects": {str(home["channels"]): {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
    root = home["configs"] / "templates" / "claude" / "blank"
    (root / "skills" / "voice").mkdir(parents=True)
    (root / "template.json").write_text(json.dumps({"id": "blank", "label": "空白"}), encoding="utf-8")
    (root / "CLAUDE.md").write_text("你是测试 bot。\n", encoding="utf-8")
    (root / "access.json").write_text(json.dumps({"allowFrom": []}), encoding="utf-8")
    (root / "settings.json").write_text(json.dumps(template_settings), encoding="utf-8")
    (root / "skills" / "voice" / "SKILL.md").write_text("# 语音技能\n", encoding="utf-8")


def _spec(bot_id):
    return {"bot_id": bot_id, "display_name": "新 Bot", "telegram_username": "new_bot",
            "telegram_user_id": 12345, "chat_id": "987654321", "persona_template": "blank",
            "voice": {"voice_id": "v1", "tts_model": "s2-pro"}, "features": ["voice", "groups"],
            "enabled": True, "op_id": "create-test-1", "confirm_other_side_unknown": False,
            "telegram_token": TELEGRAM_TOKEN}


def _create(home, monkeypatch, bot_id, template_settings):
    _make_templates(home, template_settings)
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
    path = home["channels"] / bot_id / ".claude" / "settings.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _managed(settings):
    names = {n for _, n in FOUR_HOOKS}
    out = []
    for it in settings["hooks"]["SessionStart"]:
        base = [os.path.basename(t) for t in shlex.split(it["hooks"][0]["command"])]
        out.append((it.get("matcher"), next((b for b in base if b in names), None)))
    return out


def _assert_commands_use_this_interpreter_and_repo_scripts(settings):
    for it, (_, name) in zip(settings["hooks"]["SessionStart"], FOUR_HOOKS):
        toks = shlex.split(it["hooks"][0]["command"])
        assert toks[0] == sys.executable, it
        assert os.path.realpath(toks[-1]) == os.path.realpath(script_path(name)) and os.path.isabs(toks[-1]), it


def test_新建bot_SessionStart恰好四条_顺序与命令正确_没有Stop写turn_end(home, monkeypatch):
    settings = _create(home, monkeypatch, "newbota", {})
    assert _managed(settings) == FOUR_HOOKS                 # 恰好四条，顺序：群聊、私聊、找回、回顾
    assert len(settings["hooks"]["SessionStart"]) == 4
    _assert_commands_use_this_interpreter_and_repo_scripts(settings)
    assert ".turn-end" not in json.dumps(settings)          # 不要 Stop 写 .turn-end
    assert "Stop" not in settings["hooks"]


def test_新建bot_模板旧写法的群聊钩子换成绝对路径_只留四条_其它钩子保留(home, monkeypatch):
    stop = {"hooks": [{"type": "command", "command": "true"}]}
    template = {"hooks": {"SessionStart": [{"matcher": "compact", "hooks": [
        {"type": "command", "command": 'python3 "$CLAUDEBOTLIFE_REPO/scripts/compact_group_context.py"'}]}],
        "Stop": [stop]}}
    settings = _create(home, monkeypatch, "newbotb", template)
    assert _managed(settings) == FOUR_HOOKS and len(settings["hooks"]["SessionStart"]) == 4
    _assert_commands_use_this_interpreter_and_repo_scripts(settings)
    assert "CLAUDEBOTLIFE_REPO" not in json.dumps(settings["hooks"]["SessionStart"])
    assert settings["hooks"]["Stop"] == [stop]
