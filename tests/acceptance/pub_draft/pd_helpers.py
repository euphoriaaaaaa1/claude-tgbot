# -*- coding: utf-8 -*-
"""pub-draft 验收助手：回环假服务、起草脚本子进程、YAML 读写。

所有断言只按接口文件的对外行为；脚本路径固定为 scripts/draft_proactive_config.py。
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "scripts" / "draft_proactive_config.py"
BOT = "pd_bot"
CHAT = "10001"

MODEL_KEY = "sk-pubdraft-SENTINEL-KEY-0123456789"
DSH_TOKEN = "dsh-SENTINEL-TOKEN-0123456789"
PERSONA = "人设哨兵-PUBDRAFT-PERSONA-正文不得出现在输出里"
MODEL_TEXT = "MODEL-OUT-SENTINEL-草稿正文"
RESP_ID = "msg-SENTINEL-RESPONSE-ID"


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_yaml(path):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def dump_yaml(data, path):
    Path(path).write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def anthropic_response(text, stop_reason="end_turn"):
    """Anthropic 兼容回包（接口 B11c 用 stop_reason）。"""
    return {"id": RESP_ID, "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": text}] if text is not None else [],
            "stop_reason": stop_reason, "usage": {"input_tokens": 3, "output_tokens": 5}}


class FakeHTTP:
    """通用回环假服务：handler(self) 返回 (status, headers, body_bytes)；hits 逐次记录。"""

    def __init__(self, handler):
        outer = self
        self.hits = []
        self.handler = handler
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._build())
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def url(self):
        return "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def _build(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                outer.hits.append({"method": self.command, "path": self.path,
                                   "body": raw, "headers": dict(self.headers)})
                status, headers, body = outer.handler(self, raw)
                if isinstance(body, str):
                    body = body.encode("utf-8")
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = _serve
            do_POST = _serve
            do_PUT = _serve

        return H

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def model_stub(text, status=200, stop_reason="end_turn", headers=None):
    """假模型服务：任意路径都回同一份 Anthropic 回包。"""
    body = json.dumps(anthropic_response(text, stop_reason), ensure_ascii=False)

    def handler(_req, _raw):
        return status, headers or {"Content-Type": "application/json"}, body
    return FakeHTTP(handler)


def dsh_stub(bot=BOT, life=None, mode="ok", put_bodies=None):
    """假 dsh 适配器（/adapter/v1 形状，与仓库既有桩一致）。返回 (FakeHTTP, puts)。"""
    puts = put_bodies if put_bodies is not None else []
    life_state = dict(life or {"sleep_hours": ["01:00-02:00"]})
    life_file_exists = True

    def handler(req, raw):
        path = req.path.split("?", 1)[0]
        if mode == "5xx":
            return 500, {"Content-Type": "application/json"}, json.dumps({"code": "boom"})
        if req.headers.get("Authorization") != "Bearer " + DSH_TOKEN:
            return 401, {"Content-Type": "application/json"}, json.dumps({"code": "unauthorized"})
        if path.endswith("/bots/" + bot):
            if mode == "bot_missing":
                return 404, {"Content-Type": "application/json"}, json.dumps({"code": "not_found"})
            if mode == "bad_shape":
                return 200, {"Content-Type": "application/json"}, json.dumps(["not-a-map"])
            return 200, {"Content-Type": "application/json"}, json.dumps(
                {"bot_id": bot, "person": bot, "display_name": "PD"}, ensure_ascii=False)
        if path.endswith("/bots/%s/persona" % bot):
            return 200, {"Content-Type": "application/json"}, json.dumps(
                {"text": PERSONA, "version": "0" * 16}, ensure_ascii=False)
        if path.endswith("/bots/%s/life" % bot):
            if mode == "life_missing" or not life_file_exists:
                return 404, {"Content-Type": "application/json"}, json.dumps({"code": "not_found"})
            if req.command == "PUT":
                puts.append(json.loads(raw or b"{}"))
                if mode == "apply_422":
                    return 422, {"Content-Type": "application/json"}, \
                        json.dumps({"code": "invalid", "fields": {"sleep_hours": "bad"}})
                body = json.loads(raw or b"{}")
                life_state.update(body.get("values", body))
                return 200, {"Content-Type": "application/json"}, \
                    json.dumps({"applied": True, "queued": False, "message": "ok"})
            if mode == "bad_shape":
                return 200, {"Content-Type": "application/json"}, json.dumps("nope")
            return 200, {"Content-Type": "application/json"}, json.dumps(
                {"fields": [{"key": "sleep_hours"}], "values": life_state}, ensure_ascii=False)
        return 404, {"Content-Type": "application/json"}, json.dumps({"code": "not_found"})

    return FakeHTTP(handler), puts


def dead_url():
    """本机无人监听的端口。"""
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return "http://127.0.0.1:%d" % port


def write_model_cfg(env, base_url, key=MODEL_KEY, timeout=5):
    """写模型地址/密钥（新旧写法都写，避免猜测内部只认哪一处）。"""
    cfg = {
        "jiwen": {
            "tick_interval_min": 5,
            "delta_llm": {"base_url": base_url, "api_key": key,
                          "model": "deepseek-test", "timeout": timeout},
        }
    }
    dump_yaml(cfg, Path(env["configs"]) / "_global.yml")
    if key is not None:
        dump_yaml({"deepseek": {"api_key": key}}, Path(env["configs"]) / "_secrets.yml")


def write_persona(channel, text=PERSONA):
    (Path(channel) / "CLAUDE.md").write_text(text, encoding="utf-8")


def run_draft(args, env, cwd=None, extra_env=None, timeout=60):
    """跑起草脚本子进程；脚本缺失时直接断言失败，避免把'文件不存在'误判成退出码 2。"""
    assert SCRIPT.is_file(), "实现缺失：scripts/draft_proactive_config.py"
    e = dict(os.environ)
    e.update({"HOME": str(env["home"]), "USERPROFILE": str(env["home"]),
              "CLAUDEBOT_CONFIG_DIR": str(env["configs"]),
              "HUB_CONFIGS_DIR": str(env["configs"]),
              "CLAUDEBOT_GLOBAL_CFG": str(Path(env["configs"]) / "_global.yml"),
              "CLAUDEBOT_SECRETS_FILE": str(Path(env["configs"]) / "_secrets.yml"),
              "CLAUDEBOT_PROVIDER_PATH": str(Path(env["root"]) / "providers.json"),
              "CLAUDEBOT_CHANNELS_ROOT": str(env["channels"]),
              "CLAUDEBOTLIFE_TEST": "1",
              "PYTHONPATH": str(REPO)})
    if extra_env:
        e.update({k: str(v) for k, v in extra_env.items()})
    return subprocess.run([sys.executable, str(SCRIPT)] + list(args), cwd=str(cwd or REPO),
                          env=e, capture_output=True, text=True, timeout=timeout)


def valid_draft_yaml(**over):
    data = {"persona_summary": "摘要值", "speaking_threshold": "阈值值",
            "jiwen": {"delta_hints": "思念规则"}, "sleep_hours": ["00:30-06:30"]}
    data.update(over)
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def no_sensitive_output(result, extra_texts=()):
    """B23：stdout/stderr 不得出现密钥、令牌、人设原文、草稿正文、回包正文。"""
    blob = (result.stdout or "") + (result.stderr or "")
    for secret in (MODEL_KEY, DSH_TOKEN, PERSONA, MODEL_TEXT, RESP_ID) + tuple(extra_texts):
        assert secret not in blob, "敏感内容泄漏到输出: %r" % secret
