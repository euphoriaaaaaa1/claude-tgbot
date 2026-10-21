# -*- coding: utf-8 -*-
"""直连 openai 的地址推导与 ``claude_cli._direct_call`` 的 openai 分支。

全部离线：上游是回环上的假 HTTP 服务，端口由系统分配。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import claude_cli
from scripts.derive_endpoints import direct_openai_url

API_KEY = "sk-test-" + "0123456789abcdef" * 2


class _CaptureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        self.server.hits.append({"path": self.path,
                                 "headers": dict(self.headers.items()),
                                 "body": raw})
        payload = json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def upstream():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
    httpd.hits = []
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.parametrize("base, want", [
    ("https://a.example", "https://a.example/v1"),
    ("https://a.example/v1", "https://a.example/v1"),
    ("https://a.example/v1/", "https://a.example/v1"),
    ("https://a.example/anthropic", "https://a.example/v1"),
    # 后缀只看路径：主机名恰为 v1 / anthropic 时不参与判断
    ("https://v1", "https://v1/v1"),
    ("https://anthropic", "https://anthropic/v1"),
    # 带查询串或片段：一个字节不动
    ("https://a.example/v1?x=1", "https://a.example/v1?x=1"),
    ("https://a.example/v1#f", "https://a.example/v1#f"),
])
def test_direct_openai_url_参数表(base, want):
    assert direct_openai_url(base) == want


def test_direct_call_openai_打到v1chatcompletions_且密钥只在请求头(upstream):
    port = upstream.server_address[1]
    # 两种面板写法（裸主机 / 带 /anthropic）都必须落到同一个 /v1/chat/completions
    for base in ("http://127.0.0.1:%d" % port,
                 "http://127.0.0.1:%d/anthropic" % port):
        entry = {"id": "p1", "protocol": "openai", "base_url": base, "api_key": API_KEY}
        assert claude_cli._direct_call(entry, "m-1", "你好", timeout=5) == "OK"

    assert [hit["path"] for hit in upstream.hits] == ["/v1/chat/completions"] * 2
    for hit in upstream.hits:
        assert hit["headers"].get("Authorization") == "Bearer " + API_KEY
        # 反向：密钥不出现在 URL、请求体，也不被塞进别的头
        assert API_KEY not in hit["path"]
        assert API_KEY.encode("utf-8") not in hit["body"]
        assert API_KEY not in json.dumps({k: v for k, v in hit["headers"].items()
                                          if k.lower() != "authorization"})
        assert json.loads(hit["body"]) == {
            "model": "m-1",
            "messages": [{"role": "user", "content": "你好"}],
        }
