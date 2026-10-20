# -*- coding: utf-8 -*-
"""语音试听（INTERFACE §2.12）。

``POST /api/console/voice/preview``：校验请求体后在服务端调用语音桥
``/synthesize_voice``，成功原样回 ``audio/ogg``。令牌只从文件读，绝不回显。
"""
import http.client
import json
import os
import re
import threading
import time
import urllib.parse

from flask import Blueprint, Response, jsonify, request

from moments.request_body import BodyError, json_object

voice_preview_bp = Blueprint("voice_preview", __name__)

MAX_BODY = 4 * 1024
TTS_MODELS = ("s1", "s2-pro", "s2.1-pro", "s2.1-pro-free", "drama-3")
VOICE_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
DEFAULT_BRIDGE = "http://127.0.0.1:7788"
DEFAULT_TOKEN_FILE = "~/.claude/secrets/voice-bridge.token"
TIMEOUT = 60.0
COOLDOWN = 3.0

_lock = threading.Lock()
_last_at = 0.0


def _invalid(fields):
    return jsonify({"ok": False, "code": "invalid", "message": "请求体不合规", "fields": fields}), 422


def _fail(code, status, message):
    return jsonify({"ok": False, "code": code, "message": message}), status


def _token():
    path = os.environ.get("VOICE_BRIDGE_TOKEN_FILE") or DEFAULT_TOKEN_FILE
    try:
        with open(os.path.expanduser(path), "r", encoding="utf-8") as f:
            token = f.read(4096).strip()
    except OSError:
        return ""
    return token if token.isascii() and token.isprintable() else ""


def _bridge():
    raw = os.environ.get("CLAUDEBOT_VOICE_BRIDGE_URL") or DEFAULT_BRIDGE
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), parts.path.rstrip("/")


@voice_preview_bp.post("/api/console/voice/preview")
def voice_preview():
    global _last_at
    try:
        body = json_object(MAX_BODY)
    except BodyError as e:
        return jsonify({"ok": False, "code": e.code, "message": e.message}), e.http_status

    fields = {}
    text = body.get("text")
    if not isinstance(text, str) or not text.strip() or len(text) > 200:
        fields["text"] = "文字必须是 1 到 200 个字符"
    voice_id = body.get("voice_id")
    if not isinstance(voice_id, str) or VOICE_ID_RE.fullmatch(voice_id) is None:
        fields["voice_id"] = "语音编号只能用字母、数字、下划线、连字符，1 到 64 个字符"
    model = body.get("tts_model")
    if model not in (None, "") and model not in TTS_MODELS:
        fields["tts_model"] = "语音模型只能是 %s 之一" % "、".join(TTS_MODELS)
    if fields:
        return _invalid(fields)

    token = _token()
    target = _bridge()
    if not token or target is None:
        return _fail("voice_bridge_unconfigured", 503, "语音桥没有配置")

    cooldown = float(os.environ.get("CLAUDEBOT_VOICE_PREVIEW_COOLDOWN_S") or COOLDOWN)
    with _lock:
        now = time.monotonic()
        if cooldown > 0 and now - _last_at < cooldown:
            return _fail("busy", 409, "试听太频繁，请稍后再试")
        _last_at = now

    host, port, prefix = target
    payload = {"text": text, "voice_id": voice_id, "emotion": "NEUTRAL", "format": "ogg"}
    if model:
        payload["tts_model"] = model
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + token}
    timeout = float(os.environ.get("CLAUDEBOT_VOICE_PREVIEW_TIMEOUT_S") or TIMEOUT)
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        try:
            conn.request("POST", prefix + "/synthesize_voice", body=raw, headers=headers)
            resp = conn.getresponse()
            audio = resp.read()
            status = resp.status
        except TimeoutError:
            return _fail("voice_bridge_timeout", 504, "语音桥超时")
        except (OSError, http.client.HTTPException):
            return _fail("voice_bridge_down", 502, "语音桥没有运行")
    finally:
        conn.close()
    if status == 401:
        return _fail("voice_bridge_unauthorized", 502, "语音桥拒绝了令牌")
    if status >= 400:
        return _fail("voice_failed", 502, "语音桥合成失败")
    out = Response(audio, status=200, mimetype="audio/ogg")
    out.headers["X-TTS-Model"] = model or ""
    out.headers["Cache-Control"] = "no-store"
    out.headers["X-Content-Type-Options"] = "nosniff"
    return out
