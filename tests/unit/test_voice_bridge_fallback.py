# -*- coding: utf-8 -*-
"""语音桥 Fish 合成的回落与粘住（voice-bridge/server_http.py 的 synthesize_voice）。

规则：原模型回非 2xx → 本次改用付费回落模型再发一次；只有"原模型回 4xx（429 除外）且回落成功"才粘住回落，
429 与 5xx 只本次退回；回落也失败（音色编号不存在、没权限这类请求本身的问题）照旧抛出、不粘住——
否则一只 bot 的音色失效就让所有 bot 此后都改用付费模型。

被测文件拷到临时目录再导入（它导入时会在自己旁边建 logs/ 写日志，不能落在仓里）；Fish 由 httpx.MockTransport
顶替，ffmpeg 转码换成假函数，不连网、不花钱。缺语音桥自己的依赖（httpx / fastapi / ormsgpack）时整份跳过。
"""
import asyncio
import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("fastapi")
pytest.importorskip("ormsgpack")

SRC = Path(__file__).resolve().parents[2] / "voice-bridge" / "server_http.py"


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    for py in SRC.parent.glob("*.py"):   # 连同它导入的同目录模块（auth_gate 等）一起拷
        shutil.copy(py, tmp_path / py.name)
    dst = tmp_path / "server_http.py"
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("FISH_AUDIO_API_KEY", "test-key-not-real")
    spec = importlib.util.spec_from_file_location("vb_server_http_under_test", dst)
    srv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(srv)
    monkeypatch.setattr(srv.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=b"OggS-fake", stderr=b""))
    state = {"seen": [], "reject": {}}

    def handler(r):
        m = r.headers.get("model")
        state["seen"].append(m)
        if m in state["reject"]:
            return httpx.Response(state["reject"][m], json={"error": "rejected"})
        return httpx.Response(200, content=b"ID3-fake-mp3")

    srv._fish_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=5.0)
    srv._fish_active_model = None
    monkeypatch.setattr(srv, "FISH_MODEL_DEFAULT", "s1")   # 相当于用户把默认模型配成了免费档 s1
    yield srv, state
    asyncio.run(srv._fish_client.aclose())


def _synth(srv, model):
    return asyncio.run(srv.synthesize_voice("你好", "", model=model))


@pytest.mark.parametrize("status,sticky", [(400, True), (403, True), (404, True), (429, False), (500, False), (503, False)])
def test_原模型失败_本次改用回落_只有4xx且回落成功才粘住(bridge, status, sticky):
    srv, st = bridge
    fb, primary = srv.FISH_MODEL_FALLBACK, "s1"
    st["reject"] = {primary: status}
    assert _synth(srv, primary) == b"OggS-fake"
    assert st["seen"] == [primary, fb]
    assert srv._fish_active_model == (fb if sticky else None)


@pytest.mark.parametrize("status", [400, 403, 404])
def test_回落也失败_问题在请求本身_照旧抛出且不粘住(bridge, status):
    srv, st = bridge
    fb, primary = srv.FISH_MODEL_FALLBACK, "s1"
    st["reject"] = {primary: status, fb: status}
    with pytest.raises(httpx.HTTPStatusError):
        _synth(srv, primary)
    assert st["seen"] == [primary, fb]
    assert srv._fish_active_model is None


def test_调用方传的不是默认模型_失败只本次退回_不粘住(bridge):
    srv, st = bridge
    fb = srv.FISH_MODEL_FALLBACK
    st["reject"] = {"s2-pro": 400}
    assert _synth(srv, "s2-pro") == b"OggS-fake"
    assert st["seen"] == ["s2-pro", fb] and srv._fish_active_model is None


def test_粘住之后直接用回落_不再先试原模型(bridge):
    srv, st = bridge
    fb, primary = srv.FISH_MODEL_FALLBACK, "s1"
    st["reject"] = {primary: 400}
    _synth(srv, primary)
    st["seen"].clear()
    assert _synth(srv, primary) == b"OggS-fake"
    assert st["seen"] == [fb]
