# -*- coding: utf-8 -*-
"""claude 适配器（T13）单测：能力矩阵、dispatcher HTTP、启停、新建暂存/回滚、令牌不落日志、静态门禁。

不连真实 8767、不启动真实 bot、不碰生产目录：全部在临时 HOME / configs / channels 下，
dispatcher 是回环上的假 HTTP 服务，端口由系统分配。
"""
from __future__ import annotations

import importlib.util
import json
import re
import socket
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
    assert meta["capabilities"]["bot.life_settings"] is True   # T16 已移植 life_settings
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
            "features": ["voice", "groups"], "enabled": True, "op_id": "create-test-1",
            "confirm_other_side_unknown": False, "telegram_token": TELEGRAM_TOKEN}


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


def _free_closed_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _steps(out):
    return {item["step"]: item for item in out.get("steps") or []}


def test_create_dispatcher未启动时新建成功且跳过首次拉起(home, monkeypatch):
    """沙箱/未注册自启服务时 dispatcher 端口不应答：新建不能因此超时撤回。"""
    make_templates(home)
    monkeypatch.setattr(claude_create, "allocate_port", lambda ctx: _free_closed_port())
    monkeypatch.setattr(claude_create, "_install_schedule", lambda *a, **k: "ok")
    monkeypatch.setattr(claude_create, "_uninstall_schedule", lambda *a, **k: None)
    monkeypatch.setattr(claude_create, "_ensure_dispatcher", lambda *a, **k: None)
    monkeypatch.setattr(claude_create, "START_PROBE_WAIT", 0.0)
    monkeypatch.setattr(claude_create, "START_RETRY_WAIT", 0.0)
    monkeypatch.setattr(claude_create, "START_KICK_WAIT", 0.0)
    out = ClaudeLocalAdapter().create(_spec(), SecretToken(TELEGRAM_TOKEN))
    steps = _steps(out)
    assert out["enabled"] is True and out["running"] is False
    assert steps["start"]["status"] == "done"
    assert steps["first_spawn"]["status"] == "skipped"
    assert steps["first_spawn"]["message"] == claude_create.FIRST_SPAWN_SKIPPED
    assert out["warnings"] == [claude_create.FIRST_SPAWN_SKIPPED]
    assert (home["channels"] / "newbot").is_dir()
    assert (home["configs"] / "newbot.yml").is_file()


def test_create_dispatcher在线但ensure失败仍回滚(home, monkeypatch):
    make_templates(home)
    with FakeDispatcher(bot_id="newbot", phase="running", allow_missing=True) as fake:
        monkeypatch.setattr(claude_create, "allocate_port", lambda ctx: fake.port)
        monkeypatch.setattr(claude_create, "_install_schedule", lambda *a, **k: "ok")
        monkeypatch.setattr(claude_create, "_uninstall_schedule", lambda *a, **k: None)

        def _ensure_fails(*a, **k):
            raise claude_proc.DispatcherError("error", "dispatcher 没能拉起 worker")

        monkeypatch.setattr(claude_proc, "ensure_worker", _ensure_fails)
        with pytest.raises(base.LifecycleError) as ei:
            ClaudeLocalAdapter().create(_spec(), SecretToken(TELEGRAM_TOKEN))
    assert ei.value.state == "rolled_back"
    assert not (home["channels"] / "newbot").exists()
    assert not (home["configs"] / "newbot.yml").exists()


def test_create_dispatcher在线但worker没起来仍回滚(home, monkeypatch):
    make_templates(home)
    with FakeDispatcher(bot_id="newbot", phase="stopped", spawn=False, allow_missing=True) as fake:
        monkeypatch.setattr(claude_create, "allocate_port", lambda ctx: fake.port)
        monkeypatch.setattr(claude_create, "_install_schedule", lambda *a, **k: "ok")
        monkeypatch.setattr(claude_create, "_uninstall_schedule", lambda *a, **k: None)
        monkeypatch.setattr(claude_create, "FIRST_SPAWN_WAIT", 0.0)
        with pytest.raises(base.LifecycleError) as ei:
            ClaudeLocalAdapter().create(_spec(), SecretToken(TELEGRAM_TOKEN))
    assert ei.value.state == "rolled_back"


def test_create_dispatcher在线正常拉起路径不变(home, monkeypatch):
    make_templates(home)
    with FakeDispatcher(bot_id="newbot", phase="stopped", allow_missing=True) as fake:
        monkeypatch.setattr(claude_create, "allocate_port", lambda ctx: fake.port)
        monkeypatch.setattr(claude_create, "_install_schedule", lambda *a, **k: "ok")
        monkeypatch.setattr(claude_create, "_uninstall_schedule", lambda *a, **k: None)
        out = ClaudeLocalAdapter().create(_spec(), SecretToken(TELEGRAM_TOKEN))
    assert _steps(out)["first_spawn"]["status"] == "done"
    assert out["running"] is True
    assert out["warnings"] == []


def test_create省略features也写全关文件(home, monkeypatch):
    make_templates(home)
    spec = _spec("newbot2")
    spec.pop("features")
    spec.pop("voice")
    with FakeDispatcher(bot_id="newbot2", phase="running", allow_missing=True) as fake:
        monkeypatch.setattr(claude_create, "allocate_port", lambda ctx: fake.port)
        monkeypatch.setattr(claude_create, "_install_schedule", lambda *a, **k: "ok")
        monkeypatch.setattr(claude_create, "_uninstall_schedule", lambda *a, **k: None)
        monkeypatch.setattr(claude_create, "_ensure_dispatcher", lambda *a, **k: None)
        ClaudeLocalAdapter().create(spec, SecretToken(TELEGRAM_TOKEN))
    channel = home["channels"] / "newbot2"
    doc = json.loads((channel / "features.json").read_text(encoding="utf-8"))
    assert doc["version"] == 1 and doc["default"] is False
    assert all(v is False for v in doc["features"].values())


def _install_compact_hook():
    """与产品代码同一份钩子定义：按文件路径加载 scripts/install_compact_hook.py。"""
    spec = importlib.util.spec_from_file_location("install_compact_hook_for_test",
                                                  REPO_ROOT / "scripts" / "install_compact_hook.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _create_with_template_settings(home, monkeypatch, bot_id, template_settings=None):
    make_templates(home)
    if template_settings is not None:
        (home["configs"] / "templates" / "claude" / "blank" / "settings.json").write_text(
            json.dumps(template_settings), encoding="utf-8")
    with FakeDispatcher(bot_id=bot_id, phase="running", allow_missing=True) as fake:
        monkeypatch.setattr(claude_create, "allocate_port", lambda ctx: fake.port)
        monkeypatch.setattr(claude_create, "_install_schedule", lambda *a, **k: "ok")
        monkeypatch.setattr(claude_create, "_uninstall_schedule", lambda *a, **k: None)
        monkeypatch.setattr(claude_create, "_ensure_dispatcher", lambda *a, **k: None)
        ClaudeLocalAdapter().create(_spec(bot_id), SecretToken(TELEGRAM_TOKEN))
    path = home["channels"] / bot_id / ".claude" / "settings.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_create新bot自带四条SessionStart钩子(home, monkeypatch):
    settings = _create_with_template_settings(home, monkeypatch, "newbotc")
    ich = _install_compact_hook()
    # 群聊、私聊压缩补回，一次性找回，清空后回顾：按安装器 HOOKS 的顺序恰好四条
    assert settings["hooks"]["SessionStart"] == [ich.hook_item(m, s) for m, s in ich.HOOKS]


def test_create模板旧写法的压缩钩子换成绝对路径_四条各一条(home, monkeypatch):
    stop = {"hooks": [{"type": "command", "command": "true"}]}
    template = {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:1"},
                "hooks": {"SessionStart": [{"matcher": "compact", "hooks": [
                    {"type": "command", "command": 'python3 "$CLAUDEBOTLIFE_REPO/scripts/compact_group_context.py"'}]}],
                    "Stop": [stop]}}
    settings = _create_with_template_settings(home, monkeypatch, "newbotd", template)
    assert "env" not in settings
    ich = _install_compact_hook()
    assert settings["hooks"]["SessionStart"] == [ich.hook_item(m, s) for m, s in ich.HOOKS]
    assert settings["hooks"]["Stop"] == [stop]


def test_create模板hooks结构不合预期时原样保留且新建成功(home, monkeypatch):
    settings = _create_with_template_settings(home, monkeypatch, "newbote", {"hooks": "bad"})
    assert settings["hooks"] == "bad"


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


def test_create非法编号422(home):
    bad = _spec("Bad/../id")
    with pytest.raises(base.AdapterError) as ei:
        ClaudeLocalAdapter().create(bad, SecretToken(TELEGRAM_TOKEN))
    assert ei.value.http_status == 422
    assert not list(home["channels"].iterdir())


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
    (channel / "access.json").write_text(json.dumps({"voiceId": "v1"}), encoding="utf-8")
    a = ClaudeLocalAdapter()
    got = a.get_features(BOT)
    assert all(a.get_features(BOT)["features"].values())
    assert got["keys"][0]["key"] == "voice" and got["keys"][0]["label"]
    assert got["requirements"]["voice"]["ready"] is True
    out = a.set_features(BOT, {"voice": False})
    assert out["features"]["voice"] is False
    doc = json.loads((channel / "features.json").read_text(encoding="utf-8"))
    assert doc["features"]["voice"] is False
    assert doc["version"] == 1 and doc["default"] is False


def test_set_features缺前提422(home):
    write_bot(home)   # 没有 access.json / voiceId
    with pytest.raises(base.AdapterError) as ei:
        ClaudeLocalAdapter().set_features(BOT, {"voice": True})
    assert ei.value.code == "requirement_missing"
    assert ei.value.http_status == 422
    assert "voice" in ei.value.fields


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
