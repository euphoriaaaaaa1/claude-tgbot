# -*- coding: utf-8 -*-
"""桩 dispatcher —— 只实现 INTERFACE §6.1 用到的 `POST /status`（普通模块，非测试文件）。

§10 冻结的是 cliproxy 桩；bot 桩的形状由 §6.1 正文推得（POST /status、2xx、phase 字段），
非 POST 一律 405（§6.1 明写 dispatcher.ts:978 的既有行为，门户漏用 POST 必须被测出来）。
绝不打真实 17801。
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class _H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        st = self.server.stub
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        st.hits.append(("POST", self.path))
        if self.path.split("?")[0] != "/status":
            return self._send(404, {"error": "stub: no such endpoint"})
        if st.delay:
            time.sleep(st.delay)
        if st.status_code != 200:
            return self._send(st.status_code, {"error": "stub forced"})
        return self._send(200, st.payload)

    def do_GET(self):
        self.server.stub.hits.append(("GET", self.path))
        return self._send(405, {"error": "method not allowed"})


class StubDispatcher:
    def __init__(self, phase="ready", pid=12345, queue_depth=0, in_flight=None):
        self.payload = {"phase": phase, "pid": pid, "queue_depth": queue_depth,
                        "in_flight": in_flight}
        self.status_code = 200
        self.delay = 0
        self.hits = []
        self._srv = None
        self._port = None

    def start(self):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        srv.stub = self
        srv.daemon_threads = True
        self._srv = srv
        self._port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self._srv:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None

    @property
    def port(self):
        return self._port
