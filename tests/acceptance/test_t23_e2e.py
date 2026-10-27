# -*- coding: utf-8 -*-
"""T23 公开版端到端沙箱：聚会、拍照轮次、群发现、朋友圈、主动消息、生活、关系。

全部使用临时 HOME/配置目录 + 回环假 Telegram + mock dsh，绝不连接真实
Telegram、上游模型、dsh 或生产端口。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PY = sys.executable
TOKEN = "acceptance-token"
GROUP = "-1001234567890"
BOT = "bot2"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Quiet(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, status, payload):
        raw = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class TelegramStub:
    """只实现 dispatcher 启动所需的 getMe/getUpdates/sendMessage。"""

    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def _handler(self):
        me = {"id": 987654321, "is_bot": True, "first_name": "T23",
              "username": "t23_bot", "can_join_groups": True,
              "can_read_all_group_messages": True}

        class H(_Quiet):
            def do_GET(self):
                self.send_json(200, {"ok": True, "result": me})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                method = self.path.rstrip("/").rsplit("/", 1)[-1]
                if method == "getMe":
                    self.send_json(200, {"ok": True, "result": me})
                elif method == "getUpdates":
                    self.send_json(200, {"ok": True, "result": []})
                elif method == "sendMessage":
                    self.send_json(200, {"ok": True, "result": {
                        "message_id": 1, "date": 0,
                        "chat": {"id": 10001, "type": "private"}, "text": "ok"}})
                else:
                    self.send_json(200, {"ok": True, "result": True})
        return H

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class DshStub:
    def __init__(self):
        self.life = {"fields": ["mood"], "values": {"mood": "calm"}}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def _handler(self):
        stub = self
        caps = {key: True for key in (
            "bot.features", "bot.life_settings", "bot.persona", "bot.voice",
            "bot.enable", "bot.group_chat", "bot.groups", "bot.provider",
            "bot.restart", "bot.create", "bot.archive", "bot.delete",
            "bot.profile", "bot.groups_discovered")}
        bot = {"bot_id": BOT, "person": BOT, "display_name": "Bot 2",
               "username": "t23_bot", "enabled": True, "running": False,
               "state": "worker_idle", "polling": False, "provider": None}

        class H(_Quiet):
            def _ok(self):
                return self.headers.get("Authorization") == "Bearer " + TOKEN

            def do_GET(self):
                if not self._ok():
                    return self.send_json(401, {"error": "unauthorized"})
                path = self.path.split("?", 1)[0]
                if path.endswith("/meta"):
                    return self.send_json(200, {"backend": "dsh", "capabilities": caps,
                                                "effort_levels": ["low", "medium", "high", "xhigh", "max"]})
                if path.endswith("/bots"):
                    return self.send_json(200, [bot])
                if path.endswith("/bots/" + BOT):
                    return self.send_json(200, bot)
                if path.endswith("/life"):
                    return self.send_json(200, dict(stub.life))
                if path.endswith("/groups/discovered"):
                    return self.send_json(200, {
                        "can_read_all": True, "can_join_groups": True,
                        "groups": [{"chat_id": GROUP, "title": "T23 Group",
                                    "username": "t23group", "type": "supergroup"}],
                        "warnings": []})
                if path.endswith("/features"):
                    keys = [{"key": k, "label": k} for k in (
                        "voice", "image", "stickers", "moments", "proactive",
                        "life", "relationship", "groups")]
                    return self.send_json(200, {"keys": keys,
                                                "features": {k["key"]: True for k in keys},
                                                "requirements": {}, "warnings": []})
                return self.send_json(404, {"error": "not found"})

            def do_PUT(self):
                if not self._ok():
                    return self.send_json(401, {"error": "unauthorized"})
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path.split("?")[0].endswith("/life"):
                    stub.life["values"] = body.get("values", {})
                    return self.send_json(200, {"applied": True, "queued": False, "message": "已应用"})
                return self.send_json(200, {})

            def do_POST(self):
                self.send_json(200, {"ok": True})
        return H

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Client:
    def __init__(self, base):
        self.base = base
        self.jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method, path, payload=None, form=None):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode("utf-8")
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=20) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def json(self, method, path, payload=None):
        status, raw, _ = self.request(method, path, payload=payload)
        try:
            return status, json.loads(raw or b"null")
        except ValueError:
            return status, {"raw": raw.decode("utf-8", "replace")}


def _wait_http(url, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status < 500:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    home = tmp_path / "home"
    configs = tmp_path / "configs"
    channels = tmp_path / "channels" / BOT
    for p in (home, configs, channels, channels / "inbox", home / ".claude" / "secrets"):
        p.mkdir(parents=True, exist_ok=True)
    (configs / "_global.yml").write_text("moments:\n  enabled: true\n", encoding="utf-8")
    (configs / (BOT + ".yml")).write_text("enabled: true\ndisplay_name: Bot 2\n", encoding="utf-8")
    (configs / "_group_chat.yml").write_text(
        "groups:\n  - chat_id: \"%s\"\n    allowFrom: [\"10001\"]\n    requireMention: false\n" % GROUP,
        encoding="utf-8")
    (channels / "access.json").write_text(json.dumps({
        "dmPolicy": "allowlist", "allowFrom": ["10001"],
        "groups": {GROUP: {"allowFrom": ["10001"]}},
    }), encoding="utf-8")
    (channels / "features.json").write_text(json.dumps({
        "version": 1, "default": True,
        "features": {k: True for k in (
            "voice", "image", "stickers", "moments", "proactive",
            "life", "relationship", "groups")},
    }), encoding="utf-8")
    token_file = tmp_path / "adapter.token"
    token_file.write_text(TOKEN, encoding="utf-8")
    token_file.chmod(0o600)
    provider_path = tmp_path / "providers.json"
    provider_path.write_text(json.dumps({"providers": [], "bindings": {}, "proxy": {}}), encoding="utf-8")

    tg = TelegramStub()
    dsh = DshStub()
    port = free_port()
    env = dict(os.environ)
    env.update({
        "HOME": str(home), "MOMENTS_WEB_MODE": "provider",
        "MOMENTS_WEB_HOST": "127.0.0.1", "MOMENTS_WEB_PORT": str(port),
        "CLAUDEBOT_CONFIG_DIR": str(configs),
        "CLAUDEBOT_CHANNELS_ROOT": str(tmp_path / "channels"),
        "CLAUDEBOT_PROVIDER_PATH": str(provider_path),
        "CONSOLE_BACKENDS": "dsh",
        "CONSOLE_DSH_ADAPTER_URL": dsh.url,
        "CONSOLE_ADAPTER_TOKEN_FILE": str(token_file),
        "CONSOLE_TELEGRAM_API_BASE": tg.url,
        "DISPATCHER_PARTY_DIR": str(tmp_path / "party"),
        "NO_OPEN": "1",
        "PYTHONPATH": str(REPO),
    })
    password = "t23-password"
    setup = subprocess.run([PY, "-m", "moments.panel_auth", "--set-password"],
                           cwd=str(REPO), env=env, input=password + "\n" + password + "\n",
                           text=True, capture_output=True, timeout=60)
    assert setup.returncode == 0, setup.stderr

    log = open(tmp_path / "web.log", "wb")
    proc = subprocess.Popen([PY, "-m", "moments.web"], cwd=str(REPO), env=env,
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    assert _wait_http("http://127.0.0.1:%d/login" % port), "web 未启动"
    client = Client("http://127.0.0.1:%d" % port)
    status, body, _ = client.request("POST", "/login", form={"password": password, "next": "/provider"})
    assert status == 200 and b"<html" in body.lower(), (status, body[:200])

    class Box:
        pass
    box = Box()
    box.tmp = tmp_path
    box.home = home
    box.configs = configs
    box.channels = channels
    box.client = client
    box.tg = tg
    box.dsh = dsh
    box.env = env
    box.web_proc = proc
    try:
        yield box
    finally:
        try:
            os.killpg(proc.pid, 15)
        except OSError:
            pass
        proc.wait(timeout=10)
        log.close()
        tg.stop()
        dsh.stop()


def test_party_switch_and_group_discovery(sandbox):
    client = sandbox.client
    status, body = client.json("GET", "/api/console/groups")
    assert status == 200 and isinstance(body.get("groups"), list), body
    assert any(g["chat_id"] == GROUP for g in body["groups"]), body
    assert body["groups"][0]["party"]["on"] is False

    status, body = client.json("POST", "/api/console/groups/%s/party" % GROUP, {"on": True})
    assert status == 200 and body["party"]["on"] is True, body
    status, body = client.json("GET", "/api/console/groups")
    assert body["groups"][0]["party"]["on"] is True

    status, body = client.json("POST", "/api/console/groups/%s/party" % GROUP, {"on": False})
    assert status == 200 and body["party"]["on"] is False

    status, body = client.json("GET", "/api/console/bots/dsh/%s/groups/discovered" % BOT)
    assert status == 200 and body["ok"], body
    assert [g["chat_id"] for g in body["groups"]] == [GROUP], body


def test_life_moments_and_relationship(sandbox, monkeypatch):
    client = sandbox.client
    status, body = client.json("GET", "/api/console/bots/dsh/%s/life" % BOT)
    assert status == 200 and body["ok"] and body["values"] == {"mood": "calm"}, body

    status, body = client.json("PUT", "/api/console/bots/dsh/%s/life" % BOT,
                               {"values": {"mood": "happy"}})
    assert status == 200 and body["ok"] and body["applied"] is True, body
    status, body = client.json("GET", "/api/console/bots/dsh/%s/life" % BOT)
    assert body["values"]["mood"] == "happy", body

    # 朋友圈：数据层一次写读（临时 DB，不碰仓库 state.db）
    import db
    db_path = sandbox.tmp / "moments.db"
    monkeypatch.setattr(db, "DB_PATH", str(db_path))
    db.init()
    mid = db.insert_moment(BOT, int(time.time()), "T23 朋友圈", "", {}, "daily", "public")
    rows = db.list_moments(bot_id=BOT, limit=5)
    assert [r["id"] for r in rows] == [mid]
    assert rows[0]["text"] == "T23 朋友圈" and rows[0]["visibility"] == "public"

    # 关系：临时 HOME 下的真实文件读写
    import relationship
    monkeypatch.setenv("HOME", str(sandbox.home))
    rel_dir = sandbox.home / ".claude" / "channels" / BOT
    rel_dir.mkdir(parents=True, exist_ok=True)
    stats = relationship.default_stats("established")
    stats = relationship.apply_delta(stats, {"affection": -10, "trust": 3})
    relationship.save(BOT, stats)
    again = relationship.load(BOT, "established")
    assert again["affection"] == pytest.approx(stats["affection"])
    assert (rel_dir / "relationship.json").is_file()


def test_photo_round_idempotent_and_expired(sandbox, tmp_path):
    if not shutil.which("bun"):
        pytest.skip("bun 不在 PATH")
    channel = sandbox.tmp / "dsp-channel"
    photo_root = sandbox.tmp / "photo"
    party_root = sandbox.tmp / "dsp-party"
    for p in (channel, channel / "inbox", photo_root / BOT, party_root):
        p.mkdir(parents=True, exist_ok=True)
    (channel / "access.json").write_text(json.dumps({
        "dmPolicy": "allowlist", "allowFrom": ["10001"], "groups": {GROUP: {"allowFrom": ["10001"]}}}),
        encoding="utf-8")
    (channel / "features.json").write_text(json.dumps({
        "version": 1, "default": True,
        "features": {k: True for k in (
            "voice", "image", "stickers", "moments", "proactive",
            "life", "relationship", "groups")}}), encoding="utf-8")
    api_token = "t23" + "0" * 60
    tok_file = channel / ".api-token"
    tok_file.write_text(api_token, encoding="utf-8")
    tok_file.chmod(0o600)

    now_ms = 1_800_000_000_000
    state = {"v": 1, "chat_id": "10001", "opened_at_ms": now_ms, "rounds": 0,
             "last_round_at_ms": None, "trigger_message_id": "1", "last_delivery_key": ""}
    (photo_root / BOT / "10001.json").write_text(json.dumps(state), encoding="utf-8")

    port = free_port()
    env = dict(sandbox.env)
    env.update({
        "HOME": str(sandbox.tmp / "dsp-home"),
        "CHANNEL_DIR": str(channel), "BOT_NAME": BOT,
        "TELEGRAM_BOT_TOKEN": "123456789:AA-your-telegram-bot-token-here",
        "DISPATCHER_PORT": str(port),
        "DISPATCHER_TELEGRAM_API_BASE": sandbox.tg.url,
        "DISPATCHER_PHOTO_MODE_DIR": str(photo_root),
        "DISPATCHER_PARTY_DIR": str(party_root),
        "CLAUDEBOT_TEST": "1", "DISPATCHER_TEST_NOW_MS": str(now_ms),
        "CLAUDEBOTLIFE_TEST": "1", "CLAUDEBOTLIFE_TEST_ROOT": str(sandbox.tmp),
    })
    (sandbox.tmp / "dsp-home").mkdir(exist_ok=True)
    log = open(sandbox.tmp / "dispatcher.log", "wb")
    proc = subprocess.Popen(["bun", "run", "dispatcher.ts"], cwd=str(REPO / "dispatcher"),
                            env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    try:
        assert _wait_http("http://127.0.0.1:%d/health" % port, timeout=30), "dispatcher 未启动"
        base = "http://127.0.0.1:%d" % port

        def photo(key):
            req = urllib.request.Request(
                base + "/photo_round", data=json.dumps(
                    {"chat_id": "10001", "delivery_key": key}).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Authorization": "Bearer " + api_token}, method="POST")
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())

        first = photo("k1")
        assert first == {"ok": True, "active": True, "round": 1,
                         "expires_at_ms": now_ms + 86_400_000}, first
        again = photo("k1")
        assert again["round"] == 1, again
        second = photo("k2")
        assert second["round"] == 2, second

        # 满 24 小时 → 过期；同一调用返回 active=false 并清掉状态
        expired = dict(state, opened_at_ms=now_ms - 86_400_000, rounds=2)
        (photo_root / BOT / "10001.json").write_text(json.dumps(expired), encoding="utf-8")
        gone = photo("k3")
        assert gone == {"ok": True, "active": False, "round": 0, "expires_at_ms": 0}, gone
        assert not (photo_root / BOT / "10001.json").exists()
    finally:
        try:
            os.killpg(proc.pid, 15)
        except OSError:
            pass
        proc.wait(timeout=10)
        log.close()


def test_proactive_self_initiate_writes_inbox(sandbox, tmp_path):
    harness = tmp_path / "harness"
    harness.mkdir()
    db_path = tmp_path / "proactive.db"
    (harness / "sitecustomize.py").write_text(
        "import os, subprocess, sys\n"
        "sys.path.insert(0, os.environ['T23_REPO'])\n"
        "import db\n"
        "db.DB_PATH = os.environ['T23_DB_PATH']\n"
        "db.init()\n"
        "_real = subprocess.run\n"
        "def _run(cmd, *a, **k):\n"
        "    s = ' '.join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)\n"
        "    if 'life-context.py' in s:\n"
        "        return subprocess.CompletedProcess(cmd, 0, '{\"action\":\"FALLBACK\"}\\n', '')\n"
        "    return _real(cmd, *a, **k)\n"
        "subprocess.run = _run\n",
        encoding="utf-8")

    dead = free_port()
    env = dict(sandbox.env)
    env.update({
        "HOME": str(sandbox.home),
        "CLAUDEBOT_CONFIG_DIR": str(sandbox.configs),
        "SELF_INITIATE_NOW": str(int(time.time()) + 10_000_000),
        "DISABLED_CHECK": "",
        "DISPATCHER_PORT_" + BOT.upper(): str(dead),
        "PYTHONPATH": str(harness) + os.pathsep + str(REPO),
        "T23_REPO": str(REPO), "T23_DB_PATH": str(db_path),
    })
    proactive_dir = sandbox.home / ".claude" / "channels" / BOT
    (proactive_dir / "inbox").mkdir(parents=True, exist_ok=True)
    tok = proactive_dir / ".api-token"
    tok.write_text("t23" + "1" * 60, encoding="utf-8")
    tok.chmod(0o600)

    result = subprocess.run([PY, str(REPO / "scripts" / "self_initiate.py"), BOT, "10001"],
                            cwd=str(REPO), env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    inbox = proactive_dir / "inbox"
    files = sorted(inbox.glob("self-*.json"))
    assert len(files) == 1, [p.name for p in inbox.iterdir()]
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["chat_id"] == "10001" and payload["chat_type"] == "private"
    assert payload["text"].startswith("[self-initiate]")
    assert payload["is_bot_sender"] is False
