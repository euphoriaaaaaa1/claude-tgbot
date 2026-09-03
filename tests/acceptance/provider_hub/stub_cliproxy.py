# -*- coding: utf-8 -*-
"""桩 cliproxy —— **按 INTERFACE §10 冻结契约实现**（普通模块，不是测试文件）。

绑 127.0.0.1 随机端口，进程内起、测试结束关（照抄 tests/imagegen_params/ip_cli.py 的形态）。
绝不下载、不启动真实 cli-proxy-api 二进制，绝不联网。

§10（修订 3）冻结的端点与开关全部实现，含新增的 auth-files 三端点、
`mgmt_status`、`patch_fail_after_n`。另有两个 §10 未列、但被 §3.0b 探测与 §8.1 A9b
要求的端点（`claude-api-key` / `v1/models`）在下方标注。
"""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MGMT_PREFIX = "/v0/management/"
NONEXISTENT_IN_V7 = (
    "/v0/management/anthropic-compatibility",
    "/v0/management/gemini-compatibility",
    "/v0/management/anthropic-api-key",
)


class _H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ---- 基础设施 ----
    def log_message(self, *a):
        pass

    def _read(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _send(self, code, obj, ctype="application/json"):
        raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _record(self, body):
        self.server.stub.requests.append({
            "method": self.command, "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body.decode("utf-8", "replace"),
        })

    def _mgmt_ok(self):
        """§10：桩校验 X-Management-Key 头，不匹配回 401 —— 才测得出门户漏带头的 bug。"""
        if not self.path.startswith(MGMT_PREFIX):
            return True
        return self.headers.get("X-Management-Key") == self.server.stub.mgmt_key

    def _dispatch(self):
        st = self.server.stub
        body = self._read()
        self._record(body)
        path = self.path.split("?", 1)[0]
        query = self.path.split("?", 1)[1] if "?" in self.path else ""

        if path == "/__stub/config" and self.command == "POST":
            st.apply_config(json.loads(body or b"{}"))
            return self._send(200, {"status": "ok"})

        if path == "/__stub/state" and self.command == "GET":
            return self._send(200, {"alias_count_violation": st.alias_count_violation})

        if path == "/healthz" and self.command == "GET":
            return self._send(200, {"status": "ok"})

        if not self._mgmt_ok():
            return self._send(401, {"error": "missing or bad X-Management-Key"})

        if path.startswith(MGMT_PREFIX) and st.mgmt_status != 200:
            return self._send(st.mgmt_status, {"error": "stub forced management failure"})
        # patch_fail_after_n：对管理 API 的写请求计数，前 N 次成功，第 N+1 次起一律 500
        if (path.startswith(MGMT_PREFIX) and self.command in ("PUT", "PATCH", "DELETE")
                and st.patch_fail_after_n is not None):
            st._writes += 1
            if st._writes > st.patch_fail_after_n:
                return self._send(500, {"error": "stub forced write failure"})

        if path in NONEXISTENT_IN_V7:
            # §10 修订 4：这三个块名在真实 v7 上管理 API 实测 404。
            # 桩如实模拟，才测得出门户把 kind 映射到错块名的 bug。
            return self._send(404, {"error": "no such management endpoint", "path": path})

        h = getattr(self, "_h_" + path.strip("/").replace("/", "_").replace("-", "_"), None)
        if h is None:
            return self._send(404, {"error": "stub: no such endpoint", "path": path})
        return h(body, query)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = lambda self: self._dispatch()

    # ---- §10 冻结端点 ----
    def _h_v0_management_openai_compatibility(self, body, query):
        return self._block("openai_compat", body)

    def _h_v0_management_api_keys(self, body, query):
        return self._send(200, [self.server.stub.downstream_key])

    def _h_v0_management_codex_auth_url(self, body, query):
        st = self.server.stub
        st.last_state = "stub-state-0001"
        return self._send(200, {"status": "ok", "url": "https://example.invalid/auth",
                                "state": st.last_state})

    def _h_v0_management_oauth_callback(self, body, query):
        return self._send(200, {"status": "ok"})

    def _h_v0_management_get_auth_status(self, body, query):
        return self._send(200, {"status": self.server.stub.auth_status})

    def _h_v0_management_auth_files(self, body, query):
        """§10 修订 3：OAuth 账户列表 / 删除。空列表形态 {"status":"ok","files":[]}。"""
        st = self.server.stub
        if self.command == "GET":
            return self._send(200, {"status": "ok", "files": st.auth_files})
        if self.command == "DELETE":
            # 门户可能用 body 的 name/email，也可能用 query 串；两种都认，匹配不到就当已删
            blob = (body or b"").decode("utf-8", "replace") + " " + (query or "")
            st.auth_files = [f for f in st.auth_files
                             if f.get("name") not in blob and f.get("email") not in blob]
            return self._send(200, {"status": "ok"})
        return self._send(405, {"error": "method not allowed"})

    def _h_v0_management_auth_files_status(self, body, query):
        """启用/禁用某账户。"""
        st = self.server.stub
        if self.command != "PATCH":
            return self._send(405, {"error": "method not allowed"})
        p = json.loads(body or b"{}")
        for f in st.auth_files:
            if p.get("name") in (None, f.get("name")) or str(p.get("email")) == f.get("email"):
                if "disabled" in p:
                    f["disabled"] = bool(p["disabled"])
        return self._send(200, {"status": "ok"})

    def _h_v1_messages(self, body, query):
        """三种可编程模式（§10）：ok / http4xx_echo（回显请求头）/ unknown_model。"""
        st = self.server.stub
        if st.messages_mode == "unknown_model":
            return self._send(502, {"error": {"type": "api_error",
                                              "message": "unknown provider for model"}})
        if st.messages_mode == "http4xx_echo":
            echo = {k: v for k, v in self.headers.items()}
            return self._send(401, {"error": {"type": "authentication_error",
                                              "message": "invalid key",
                                              "echoed_request_headers": echo}})
        if st.messages_mode == "timeout":
            import time
            time.sleep(st.timeout_sleep)
        model = ""
        try:
            model = json.loads(body or b"{}").get("model", "")
        except Exception:
            pass
        return self._send(200, {"id": "msg_stub", "type": "message", "role": "assistant",
                                "model": model,
                                "content": [{"type": "text", "text": "stub reply ok"}],
                                "usage": {"input_tokens": 1, "output_tokens": 1}})

    # ---- §10 未冻结、被正文其它章节要求的三个端点（INTERFACE 缺口，见文件头）----
    def _h_v0_management_claude_api_key(self, body, query):
        """§10 修订 4：anthropic kind 的承载块（实测块名，不是 anthropic-compatibility）。"""
        return self._block("claude_api_key", body)

    def _h_v0_management_gemini_api_key(self, body, query):
        """§10 修订 4：gemini kind 的承载块（不是 gemini-compatibility）。"""
        return self._block("gemini_api_key", body)

    def _block(self, attr, body):
        """三个承载块共用的 GET/PUT/PATCH/DELETE 语义，顺带做 §3.0c 的 alias 数记账。"""
        st = self.server.stub
        cur = getattr(st, attr)
        if self.command == "GET":
            return self._send(200, cur)
        if self.command == "PUT":
            new = json.loads(body or b"[]")
            st.note_alias_counts(new)
            setattr(st, attr, new)
            return self._send(200, {"status": "ok"})
        if self.command == "PATCH":
            p = json.loads(body or b"{}")
            i = p.get("index")
            if not isinstance(i, int) or not (0 <= i < len(cur)):
                return self._send(400, {"error": "bad index"})
            st.note_alias_counts([p.get("value")])
            cur[i] = p.get("value")
            return self._send(200, {"status": "ok"})
        if self.command == "DELETE":
            setattr(st, attr, [])
            return self._send(200, {"status": "ok"})
        return self._send(405, {"error": "method not allowed"})

    def _h_v1_models(self, body, query):
        names = [m.get("alias") or m.get("name")
                 for blk in self.server.stub.all_blocks
                 for m in (blk.get("models") or [])]
        return self._send(200, {"data": [{"id": n} for n in names if n]})

    def _h_v0_management_antigravity_auth_url(self, body, query):
        return self._h_v0_management_codex_auth_url(body, query)


class StubCliproxy:
    """进程内桩。down 模式 = 真的把监听关掉（连接被拒），与 §10 的措辞一致。"""

    def __init__(self, mgmt_key="k", downstream_key="d"):
        self.mgmt_key = mgmt_key
        self.downstream_key = downstream_key
        self.openai_compat = []          # kind=openai 的承载块
        self.claude_api_key = []         # kind=anthropic 的承载块（§10：不是 anthropic-compatibility）
        self.gemini_api_key = []         # kind=gemini 的承载块（不是 gemini-compatibility）
        self.alias_count_violation = []  # §10：收到 models 长度 ≠ 2 的写就记一条
        self.messages_mode = "ok"
        self.timeout_sleep = 40
        self.auth_status = "ok"
        self.mgmt_status = 200          # 非 200 时所有 /v0/management/* 回该码（§10）
        self._patch_fail_after_n = None
        self._writes = 0
        self.auth_files = []            # GET /v0/management/auth-files 的 files[]
        self.requests = []
        self.last_state = None
        self._srv = None
        self._port = None

    # ---- 生命周期 ----
    def start(self):
        srv = ThreadingHTTPServer(("127.0.0.1", self._port or 0), _H)
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

    def set_down(self, down=True):
        """down=True：关掉监听 → 门户连过来得 ECONNREFUSED；再 start() 用同一端口恢复。"""
        if down:
            self.stop()
        elif self._srv is None:
            self.start()

    @property
    def patch_fail_after_n(self):
        return self._patch_fail_after_n

    @patch_fail_after_n.setter
    def patch_fail_after_n(self, n):
        """赋值即把写计数清零：测试通常先建好数据再打开这个开关。"""
        self._patch_fail_after_n = n
        self._writes = 0

    @property
    def port(self):
        return self._port

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self._port

    # ---- 测试侧便利方法（不属于 §10 的线上契约，只在测试进程内用）----
    def apply_config(self, cfg):
        """对应 §10 的 POST /__stub/config。"""
        if "messages_mode" in cfg:
            self.messages_mode = cfg["messages_mode"]
        if "auth_status" in cfg:
            self.auth_status = cfg["auth_status"]
        if "mgmt_status" in cfg:
            self.mgmt_status = cfg["mgmt_status"]
        if "patch_fail_after_n" in cfg:
            self.patch_fail_after_n = cfg["patch_fail_after_n"]
        if cfg.get("reset_violations"):
            self.alias_count_violation = []
        if "auth_files" in cfg:
            self.auth_files = cfg["auth_files"]
        if "down" in cfg and cfg["down"]:
            threading.Thread(target=self.stop, daemon=True).start()

    def note_alias_counts(self, entries):
        """§10 修订 4：写进来的条目里 models 长度 ≠ 2 就记一条（桩不替实现做校验，只记账）。"""
        for e in entries or []:
            if not isinstance(e, dict):
                continue
            n = len(e.get("models") or [])
            if n != 2:
                self.alias_count_violation.append({"name": e.get("name"), "models": n})

    def seed_openai_block(self, name="deepseek", base_url="https://api.deepseek.com/v1",
                          api_key="sk-test-seeded-0000", upstream="deepseek-v4-flash",
                          alias="claude-sonnet-4-5-20250929", display_name="DeepSeek",
                          disabled=False, haiku_alias="claude-3-5-haiku-20241022"):
        """按 §3.0c 铺一条**两个 models** 的块（主 alias + haiku alias）。"""
        blk = {"name": name, "base-url": base_url,
               "api-key-entries": [{"api-key": api_key}],
               "models": [{"name": upstream, "alias": alias, "display-name": display_name,
                           "force-mapping": True},
                          {"name": upstream, "alias": haiku_alias,
                           "display-name": display_name, "force-mapping": True}],
               "disabled": disabled}
        self.openai_compat.append(blk)
        return blk

    @property
    def all_blocks(self):
        return self.openai_compat + self.claude_api_key + self.gemini_api_key

    def aliases_enabled(self, alias):
        """某 alias 当前有几个 enabled 条目（§3.6 的运行时唯一性断言用）。"""
        n = 0
        for blk in self.all_blocks:
            if blk.get("disabled"):
                continue
            for m in blk.get("models") or []:
                if m.get("alias") == alias:
                    n += 1
        return n

    def paths_hit(self, pattern):
        return [r for r in self.requests if re.search(pattern, r["path"])]

    def seed_auth_file(self, provider="codex", email="alice@example.com", disabled=False,
                       alias="claude-sonnet-4-5-20250929"):
        """按 §10 的 files[] 形状铺一个账户，返回该条目。"""
        f = {"name": "%s-%s-plus.json" % (provider, email), "provider": provider,
             "email": email, "disabled": disabled,
             "model_aliases": ({alias: "gpt-5"} if alias else {})}
        self.auth_files.append(f)
        return f

    @staticmethod
    def email_hash(email):
        import hashlib
        return hashlib.sha1(email.encode()).hexdigest()[:8]
