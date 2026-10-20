# -*- coding: utf-8 -*-
"""getMe 桩（INTERFACE-hub2 §7「getMe 桩契约（冻结）」）。

绑 127.0.0.1 随机端口，只认 `GET /bot<token>/getMe`。
冻结的 mode：ok | 401 | 429 | 500 | badjson | hang，外加 `echo_token_in_body`。
另加几个 mode 覆盖 §4.1 分型表里 R9-1/R9-2/R9-3 与 R2-2 三行（冻结名一个没动）：
ok_false_200 / no_username / redirect / huge / 404。

这是普通模块不是测试文件；绝不发真实 api.telegram.org 请求。
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 桩里回显用的"看起来像真 token"的串，用于 §4.1 脱敏用例
LEAK_TOKEN = "987654321:AAFleakLeakLeakLeakLeakLeakLeak0000"


class _H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send_raw(self, code, raw, ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _send(self, code, obj, **kw):
        self._send_raw(code, json.dumps(obj).encode("utf-8"), **kw)

    def do_POST(self):
        self.server.stub.hits.append(("POST", self.path))
        self._send(405, {"ok": False, "description": "stub: method not allowed"})

    def do_GET(self):
        st = self.server.stub
        st.hits.append(("GET", self.path))
        path = self.path.split("?")[0]
        if path == "/redirected":
            return self._send(200, {"ok": True, "result": {"username": "after_redirect",
                                                           "id": 1}})
        if not (path.startswith("/bot") and path.endswith("/getMe")):
            return self._send(404, {"ok": False, "description": "stub: no such route"})
        st.token_seen = path[len("/bot"):-len("/getMe")]
        return self._dispatch(st)

    def _dispatch(self, st):
        mode = st.mode
        desc = st.description
        if st.echo_token_in_body:
            desc = f"{desc} token={LEAK_TOKEN} url=/bot{LEAK_TOKEN}/getMe"
        if mode == "hang":
            time.sleep(st.delay)
            return self._send(200, {"ok": True, "result": {"username": st.username,
                                                           "id": st.bot_id}})
        if mode == "ok":
            return self._send(200, {"ok": True,
                                    "result": {"id": st.bot_id, "is_bot": True,
                                               "username": st.username,
                                               "first_name": st.username}})
        if mode == "401":
            return self._send(401, {"ok": False, "error_code": 401,
                                    "description": desc or "Unauthorized"})
        if mode == "404":
            return self._send(404, {"ok": False, "error_code": 404,
                                    "description": desc or "Not Found"})
        if mode == "429":
            return self._send(429, {"ok": False, "error_code": 429,
                                    "description": desc or "Too Many Requests: retry after 5"})
        if mode == "500":
            return self._send(500, {"ok": False, "error_code": 500,
                                    "description": desc or "Bad Gateway"})
        if mode == "badjson":
            return self._send_raw(200, b"<html>not json at all</html>",
                                  ctype="text/html")
        if mode == "ok_false_200":            # R9-1：HTTP 200 但 body ok:false
            return self._send(200, {"ok": False, "error_code": st.error_code or 429,
                                    "description": desc or "Too Many Requests: retry after 9"})
        if mode == "no_username":             # R9-2：ok:true 但 result 缺 username
            return self._send(200, {"ok": True, "result": {"id": st.bot_id,
                                                           "is_bot": True}})
        if mode == "redirect":                # R9-3：3xx 重定向
            return self._send(302, {"ok": False, "description": "moved"},
                              extra={"Location": "/redirected"})
        if mode == "huge":                    # R2-2：超 64 KiB 响应体
            blob = b'{"ok": true, "result": {"username": "x", "id": 1, "pad": "'
            blob += b"P" * st.huge_bytes + b'"}}'
            return self._send_raw(200, blob)
        return self._send(500, {"ok": False, "description": f"stub: unknown mode {mode}"})


class StubTelegram:
    """可编程 getMe 桩。`base` 直接喂给 HUB_TELEGRAM_API_BASE。"""

    def __init__(self, mode="ok", echo_token_in_body=False,
                 username="my_bot", bot_id=123456789):
        self.mode = mode
        self.echo_token_in_body = echo_token_in_body
        self.username = username
        self.bot_id = bot_id
        self.description = ""
        self.error_code = None
        self.delay = 12          # hang 模式睡这么久（> §4.1 的 10 秒超时）
        self.huge_bytes = 300 * 1024
        self.hits = []
        self.token_seen = None
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

    @property
    def base(self):
        return f"http://127.0.0.1:{self._port}"
