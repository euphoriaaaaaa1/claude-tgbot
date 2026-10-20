# -*- coding: utf-8 -*-
"""claude 适配器（T13）单测：能力矩阵、dispatcher HTTP、启停、新建暂存/回滚、令牌不落日志、静态门禁。

不连真实 8767、不启动真实 bot、不碰生产目录：全部在临时 HOME / configs / channels 下，
dispatcher 是回环上的假 HTTP 服务，端口由系统分配。
"""
from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import adapters.claude_create as claude_create
import adapters.claude_local as claude_local
import adapters.claude_proc as claude_proc
import bots_registry
from adapters import base
from adapters.claude_local import ClaudeLocalAdapter
from adapters.guard import SecretToken

REPO_ROOT = Path(__file__).resolve().parents[2]
API_TOKEN = "test-api-token-" + "0" * 40
TELEGRAM_TOKEN = "123456789:" + "A" * 35
BOT = "mybot"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj=None, text=""):
        raw = (json.dumps(obj) if obj is not None else text).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json" if obj is not None else "text/plain")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        fake = self.server.fake
        fake.hits.append((self.path, self.headers.get("X-Api-Token")))
        if not (fake.allow_missing and not self.headers.get("X-Api-Token")) \
                and self.headers.get("X-Api-Token") != fake.api_token:
            return self._send(401, text="unauthorized")
        if self.path == "/status":
            return self._send(200, {"ok": True, "bot": fake.bot_id, "phase": fake.phase,
                                    "pid": fake.pid, "queue_depth": 0})
        if self.path == "/ensure_worker":
            if fake.phase == "stopped" and fake.spawn:
                fake.phase = "running"
            return self._send(200, {"ok": True, "running": fake.phase in ("starting", "running"),
                                    "spawned": fake.spawn})
        return self._send(404, {"ok": False})


class FakeDispatcher:
    def __init__(self, bot_id=BOT, phase="running", pid=999999, api_token=API_TOKEN, spawn=True,
                 allow_missing=False):
        self.bot_id = bot_id
        self.phase = phase
        self.pid = pid
        self.api_token = api_token
        self.spawn = spawn
        self.allow_missing = allow_missing
        self.hits = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.fake = self
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    configs = tmp_path / "configs"
    channels = tmp_path / "channels"
    for p in (h, configs, channels):
        p.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("HUB_CONFIGS_DIR", str(configs))
    monkeypatch.setenv("HUB_CHANNELS_DIR", str(channels))
    return {"home": h, "configs": configs, "channels": channels}


def write_bot(home, bot_id=BOT, port=17801, enabled=None, channel=None):
    channel = Path(channel or (home["channels"] / bot_id))
    channel.mkdir(parents=True, exist_ok=True)
    (channel / ".api-token").write_text(API_TOKEN, encoding="utf-8")
    lines = [
        "display_name: 测试 Bot",
        'bot_channel_path: "%s"' % channel,
        "chat_id: 987654321",
        "dispatcher_port: %d" % port,
        "namespace_uuid: 550e8400-e29b-41d4-a716-446655440001",
        "telegram_username: test_bot",
        "launcher: registry",
    ]
    if enabled is not None:
        lines.append("enabled: %s" % ("true" if enabled else "false"))
    (home["configs"] / (bot_id + ".yml")).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return channel


# ── 能力矩阵 ────────────────────────────────────────────────────────────────

def test_能力矩阵与协议键一致():
    meta = ClaudeLocalAdapter().meta()
    assert meta["backend"] == "claude"
    assert meta["adapter"] == "v1"
    assert set(meta["capabilities"]) == set(base.CAPABILITY_KEYS)
    assert all(isinstance(v, bool) for v in meta["capabilities"].values())
    assert meta["capabilities"]["bot.create"] is True
    assert meta["capabilities"]["bot.archive"] is True
    assert meta["capabilities"]["bot.features"] is True
    assert meta["capabilities"]["bot.life_settings"] is False   # 依赖模块尚未移植
    assert tuple(meta["effort_levels"]) == base.EFFORT_LEVELS


# ── dispatcher HTTP ─────────────────────────────────────────────────────────

def test_status与ensure_worker带口令(home):
    with FakeDispatcher(phase="stopped") as fake:
        write_bot(home, port=fake.port)
        st = claude_proc.status(BOT)
        assert st["alive"] is False and st["phase"] == "stopped"
        assert claude_proc.polling(BOT) is False
        out = claude_proc.ensure_worker(BOT)
        assert out["running"] is True
        assert claude_proc.polling(BOT) is True
        assert all(tok == API_TOKEN for _, tok in fake.hits)


def test_口令不对映射unauthorized(home):
    with FakeDispatcher(api_token="other") as fake:
        write_bot(home, port=fake.port)
        with pytest.raises(claude_proc.DispatcherError) as ei:
            claude_proc.status(BOT)
        assert ei.value.status == "unauthorized"


def test_dispatcher不可达映射down(home):
    write_bot(home, port=1)   # 回环 1 端口没有服务
    with pytest.raises(claude_proc.DispatcherError) as ei:
        claude_proc.status(BOT)
    assert ei.value.status == "down"


def test_restart先停再ensure(home, monkeypatch):
    with FakeDispatcher(phase="running") as fake:
        write_bot(home, port=fake.port)
        monkeypatch.setattr(claude_proc, "_kill", lambda pid, force: setattr(fake, "phase", "stopped"))
        out = claude_proc.restart_worker(BOT, fake.port)
        assert out["status"] == "ok"
        assert fake.phase == "running"
        assert any(p == "/ensure_worker" for p, _ in fake.hits)


# ── bots() 与启停 ───────────────────────────────────────────────────────────

def test_bots反映运行状态与停用(home):
    with FakeDispatcher(phase="running") as fake:
        write_bot(home, port=fake.port, enabled=True)
        row = ClaudeLocalAdapter().bots()[0]
        assert row["bot_id"] == BOT and row["state"] == "running"
        assert row["enabled"] is True and row["polling"] is True
        # 停用靠 yml 的 enabled:false，不碰旧标记目录
        write_bot(home, port=fake.port, enabled=False)
        row = ClaudeLocalAdapter().bots()[0]
        assert row["state"] == "stopped" and row["enabled"] is False


def test_set_enabled写yml并停worker(home, monkeypatch):
    with FakeDispatcher(phase="running") as fake:
        write_bot(home, port=fake.port)
        monkeypatch.setattr(claude_proc, "_kill", lambda pid, force: setattr(fake, "phase", "stopped"))
        out = ClaudeLocalAdapter().set_enabled(BOT, False)
        assert out == {"enabled": False, "running": False, "message": "已停用"}
        text = (home["configs"] / (BOT + ".yml")).read_text(encoding="utf-8")
        assert re.search(r"^enabled: false$", text, re.M)
        assert not (home["home"] / ".claude" / "dispatcher" / ".disabled").exists()


def test_set_enabled启用会ensure_worker(home):
    with FakeDispatcher(phase="stopped") as fake:
        write_bot(home, port=fake.port, enabled=False)
        out = ClaudeLocalAdapter().set_enabled(BOT, True)
        assert out["enabled"] is True and out["running"] is True
        assert fake.phase == "running"


# ── 新建：暂存、提交、回滚 ──────────────────────────────────────────────────

def make_templates(home):
    (home["home"] / ".claude.json").write_text(
        json.dumps({"projects": {str(home["channels"]): {"hasTrustDialogAccepted": True}}}),
        encoding="utf-8")
    root = home["configs"] / "templates" / "claude" / "blank"
    (root / "skills" / "voice").mkdir(parents=True)
    (root / "template.json").write_text(json.dumps({"id": "blank", "label": "空白"}), encoding="utf-8")
    (root / "CLAUDE.md").write_text("你是测试 bot。\n", encoding="utf-8")
    (root / "access.json").write_text(json.dumps({"allowFrom": []}), encoding="utf-8")
    (root / "settings.json").write_text("{}", encoding="utf-8")
    (root / "skills" / "voice" / "SKILL.md").write_text("# 语音技能\n", encoding="utf-8")


def _spec(bot_id="newbot"):
    return {"bot_id": bot_id, "display_name": "新 Bot", "telegram_username": "new_bot",
            "telegram_user_id": 12345, "chat_id": "987654321", "persona_template": "blank",
            "voice": {"voice_id": "v1", "tts_model": "s2-pro"},
            "features": ["voice", "groups"], "enabled": True, "op_id": "create-test-1"}


def test_create提交产物且令牌只在env(home, monkeypatch, caplog, capsys):
    make_templates(home)
    with FakeDispatcher(bot_id="newbot", phase="running", allow_missing=True) as fake:
        monkeypatch.setattr(claude_create, "allocate_port", lambda ctx: fake.port)
        monkeypatch.setattr(claude_create, "_install_schedule", lambda *a, **k: "ok")
        monkeypatch.setattr(claude_create, "_uninstall_schedule", lambda *a, **k: None)
        monkeypatch.setattr(claude_create, "_ensure_dispatcher", lambda *a, **k: None)
        out = ClaudeLocalAdapter().create(_spec(), SecretToken(TELEGRAM_TOKEN))
    assert out["bot_id"] == "newbot" and out["enabled"] is True
    channel = home["channels"] / "newbot"
    assert (channel / ".env").read_text(encoding="utf-8") == "TELEGRAM_BOT_TOKEN=%s\n" % TELEGRAM_TOKEN
    assert (channel / "CLAUDE.md").read_text(encoding="utf-8") == "你是测试 bot。\n"
    assert (channel / ".claude" / "skills" / "newbot-voice" / "SKILL.md").exists()
    features = json.loads((channel / "features.json").read_text(encoding="utf-8"))
    assert features["features"]["voice"] is True and features["features"]["groups"] is True
    yml = (home["configs"] / "newbot.yml").read_text(encoding="utf-8")
    assert "dispatcher_port: %d" % fake.port in yml
    assert TELEGRAM_TOKEN not in yml
    assert not list((home["channels"]).glob(".staging-*"))
    out_text = capsys.readouterr()
    assert TELEGRAM_TOKEN not in out_text.out and TELEGRAM_TOKEN not in out_text.err
    assert TELEGRAM_TOKEN not in caplog.text


def test_create失败整单回滚且不残留令牌(home, monkeypatch, caplog, capsys):
    make_templates(home)
    with FakeDispatcher(bot_id="newbot", phase="stopped", spawn=False, allow_missing=True) as fake:
        monkeypatch.setattr(claude_create, "allocate_port", lambda ctx: fake.port)
        monkeypatch.setattr(claude_create, "_install_schedule", lambda *a, **k: "ok")
        monkeypatch.setattr(claude_create, "_uninstall_schedule", lambda *a, **k: None)
        monkeypatch.setattr(claude_create, "FIRST_SPAWN_WAIT", 0.0)

        def _start_fails(*a, **k):
            raise base.AdapterError("unavailable", 503, "新建超时，已撤回")

        monkeypatch.setattr(claude_create, "_start", _start_fails)
        with pytest.raises(base.LifecycleError) as ei:
            ClaudeLocalAdapter().create(_spec(), SecretToken(TELEGRAM_TOKEN))
    assert ei.value.state == "rolled_back"
    assert not (home["channels"] / "newbot").exists()
    assert not (home["configs"] / "newbot.yml").exists()
    leftovers = [p for p in home["channels"].rglob("*") if p.is_file()]
    assert all(TELEGRAM_TOKEN not in p.read_text(encoding="utf-8", errors="ignore") for p in leftovers)
    out_text = capsys.readouterr()
    assert TELEGRAM_TOKEN not in out_text.out and TELEGRAM_TOKEN not in out_text.err
    assert TELEGRAM_TOKEN not in caplog.text


# ── 人设、功能开关、群设置 ──────────────────────────────────────────────────

def test_persona版本冲突与写入(home):
    channel = write_bot(home)
    (channel / "CLAUDE.md").write_text("旧人设\n", encoding="utf-8")
    a = ClaudeLocalAdapter()
    cur = a.get_persona(BOT)
    with pytest.raises(base.AdapterError) as ei:
        a.set_persona(BOT, {"text": "新人设", "version": "0" * 16})
    assert ei.value.code == "stale"
    out = a.set_persona(BOT, {"text": "新人设", "version": cur["version"]})
    assert out["version"] != cur["version"]
    assert a.get_persona(BOT)["text"] == "新人设"


def test_features默认全开与写入(home):
    channel = write_bot(home)
    a = ClaudeLocalAdapter()
    assert all(a.get_features(BOT)["features"].values())
    out = a.set_features(BOT, {"voice": False})
    assert out["changed"] == ["voice"] and out["features"]["voice"] is False
    doc = json.loads((channel / "features.json").read_text(encoding="utf-8"))
    assert doc["features"]["voice"] is False


def test_groups进退(home):
    channel = write_bot(home)
    (channel / "access.json").write_text(json.dumps({"allowFrom": ["987654321"], "groups": {}}),
                                        encoding="utf-8")
    a = ClaudeLocalAdapter()
    out = a.set_groups(BOT, {"chat_id": "-1001234567890", "join": True,
                             "policy": {"requireMention": True, "otherBotUsernames": ["peer_bot"]}})
    assert out["in_group"] is True
    assert out["policy"]["otherBotUsernames"] == ["@peer_bot"]
    assert "-1001234567890" in a.groups(BOT)["groups"]
    a.set_groups(BOT, {"chat_id": "-1001234567890", "join": False})
    assert "-1001234567890" not in a.groups(BOT)["groups"]


# ── 归档、恢复、删除 ────────────────────────────────────────────────────────

def test_归档恢复删除走文件层(home):
    with FakeDispatcher(bot_id=BOT, phase="stopped") as fake:
        write_bot(home, port=fake.port)
        a = ClaudeLocalAdapter()
        out = a.archive(BOT, {})
        assert out["archive_id"].startswith(BOT + "-")
        assert not (home["channels"] / BOT).exists()
        assert not (home["configs"] / (BOT + ".yml")).exists()
        archives = a.list_archives()
        assert len(archives) == 1
        restored = a.restore(archives[0]["archive_id"])
        assert restored["bot_id"] == BOT and restored["enabled"] is False
        assert (home["configs"] / (BOT + ".yml")).exists()
        assert a.delete(BOT, BOT)["message"] == "已删除"
        assert not (home["channels"] / BOT).exists()


# ── 静态门禁 ────────────────────────────────────────────────────────────────

def test_适配器不再依赖终端会话或平台启动项():
    # 拼接写法：门禁扫描源码时不该被测试文件里的关键词误伤。
    banned = ["\\bt" + "mux\\b", "launch" + "ctl", "Launch" + "Agents",
              "com." + "wsxwj", "import bot_" + "registry", "import fc" + "ntl"]
    pat = re.compile("|".join(banned))
    hits = []
    for path in list((REPO_ROOT / "adapters").glob("*.py")) + [REPO_ROOT / "moments" / "worker_launch.py"]:
        if path.name == "platform_lock.py":
            continue
        if pat.search(path.read_text(encoding="utf-8")):
            hits.append(path.name)
    assert hits == []


def test_适配器进程层只走本机HTTP():
    text = (REPO_ROOT / "adapters" / "claude_proc.py").read_text(encoding="utf-8")
    assert "/status" in text and "/ensure_worker" in text
    assert "127.0.0.1" in text
    assert "X-Api-Token" in text
