# -*- coding: utf-8 -*-
"""voice-bridge HTTP 鉴权闸（纯函数，便于单测）。

默认 fail-closed：没有令牌时，除 /health 外一律拒绝。
只有显式设置 ``VOICE_BRIDGE_ALLOW_INSECURE=1`` 才恢复旧的无鉴权行为。

令牌来源（按顺序）：
1. ``VOICE_BRIDGE_TOKEN`` 环境变量；
2. ``VOICE_BRIDGE_TOKEN_FILE`` 指向的文件（默认 ``~/.claude/secrets/voice-bridge.token``）。

调用方（dispatcher / voicecall / moments）读同一个文件并带 ``Authorization: Bearer``。
"""
import hmac
import os
import secrets

DEFAULT_TOKEN_FILE = "~/.claude/secrets/voice-bridge.token"


def _env(env):
    return os.environ if env is None else env


def load_token(env=None) -> str:
    """返回可用的令牌；没有/不可读/非可见 ASCII 时返回空串。"""
    e = _env(env)
    token = (e.get("VOICE_BRIDGE_TOKEN") or "").strip()
    if token:
        return token
    path = e.get("VOICE_BRIDGE_TOKEN_FILE") or DEFAULT_TOKEN_FILE
    try:
        with open(os.path.expanduser(path), "r", encoding="utf-8") as f:
            raw = f.read(4096).strip()
    except OSError:
        return ""
    return raw if raw.isascii() and raw.isprintable() else ""


def insecure_allowed(env=None) -> bool:
    return (_env(env).get("VOICE_BRIDGE_ALLOW_INSECURE") or "").strip() == "1"


def request_allowed(path: str, authorization: str, token: str, allow_insecure: bool) -> bool:
    """除 /health 外的请求是否放行。"""
    if path == "/health":
        return True
    if token:
        got = authorization[7:] if authorization.startswith("Bearer ") else ""
        if not got:
            return False
        return hmac.compare_digest(got.encode("utf-8"), token.encode("utf-8"))
    return bool(allow_insecure)
