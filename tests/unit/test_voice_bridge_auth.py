# -*- coding: utf-8 -*-
"""voice-bridge 鉴权闸单测：默认 fail-closed、令牌文件读取、Bearer 校验。"""
import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = _ROOT / "voice-bridge" / "auth_gate.py"


def _load():
    spec = importlib.util.spec_from_file_location("voice_bridge_auth_gate", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


A = _load()


def test_无令牌_非health一律拒绝():
    assert not A.request_allowed("/transcribe_file", "", "", False)
    assert not A.request_allowed("/synthesize_voice", "Bearer x", "", False)
    assert A.request_allowed("/health", "", "", False)


def test_显式allow_insecure才放行():
    assert A.request_allowed("/transcribe_file", "", "", True)
    assert A.insecure_allowed({"VOICE_BRIDGE_ALLOW_INSECURE": "1"})
    for v in ("", "0", "true", "yes"):
        assert not A.insecure_allowed({"VOICE_BRIDGE_ALLOW_INSECURE": v})


def test_令牌校验_Bearer与常数时间比较():
    assert A.request_allowed("/x", "Bearer sekret", "sekret", False)
    assert not A.request_allowed("/x", "Bearer wrong", "sekret", False)
    assert not A.request_allowed("/x", "sekret", "sekret", False)
    assert not A.request_allowed("/x", "", "sekret", False)


def test_令牌来源_环境变量优先(tmp_path):
    f = tmp_path / "voice-bridge.token"
    f.write_text("file-token\n", encoding="utf-8")
    if sys.platform == "win32":
        f.chmod(0o600)
    assert A.load_token({"VOICE_BRIDGE_TOKEN": "env-token",
                         "VOICE_BRIDGE_TOKEN_FILE": str(f)}) == "env-token"
    assert A.load_token({"VOICE_BRIDGE_TOKEN_FILE": str(f)}) == "file-token"


def test_令牌来源_文件缺失返回空串(tmp_path):
    assert A.load_token({"VOICE_BRIDGE_TOKEN_FILE": str(tmp_path / "nope")}) == ""
