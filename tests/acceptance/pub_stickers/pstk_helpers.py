# -*- coding: utf-8 -*-
"""表情包管理页验收的公共小工具（不是测试文件）。

- 测试图片一律是"最小合法文件头 + 填充字节"，不是真实表情图片；样本库的标签、分类用占位字。
- 接口调用小工具：上传、按链接加、错误体核对。
- 假图片服务器：只监听 127.0.0.1 的端口 0，外加"解析""打开连接"两个替身，挂到
  moments.sticker_store 的 resolve / open_url 上（接口文件 §0 的测试挂点）。

本模块顶层不导入任何被测模块：被测模块还没实现时，测试照样能被收集。
"""
import base64
import hashlib
import json
import os
import socket
import struct
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread
from urllib.parse import urlsplit, urlunsplit

UPLOAD = "/api/console/stickers/upload"
LINK = "/api/console/stickers/link"
LIST = "/api/console/stickers"
PAGE = "/console/stickers"
MiB = 1024 * 1024
MAX_IMG = 10 * MiB
ITEM_KEYS = {"id", "group", "label", "category", "url", "file", "size", "missing", "img"}
INTERNAL = {"ok": False, "code": "internal", "message": "服务器内部错误，请稍后再试"}
LIMITS = {"max_bytes": 10485760, "types": ["gif", "jpg", "png", "webp"]}
EMPTY_LIST = {"ok": True, "groups": {"sfw": [], "nsfw": []}, "categories": [],
              "incomplete": {"sfw": 0, "nsfw": 0}, "limits": LIMITS}
BROKEN_HEAD = "表情库文件坏了（"
BROKEN_TAIL = "），已停止一切写入，请先修好它"


# ── 测试图片 ─────────────────────────────────────────────────────────────────

def _fill(tag, n):
    return (("|" + tag).encode("utf-8") * (n // 2 + 2))[:n]


def gif(tag="a", n=64):
    return b"GIF89a" + _fill(tag, n)


def gif87(tag="a", n=64):
    return b"GIF87a" + _fill(tag, n)


def png(tag="a", n=64):
    return b"\x89PNG\r\n\x1a\n" + _fill(tag, n)


def jpg(tag="a", n=64):
    return b"\xff\xd8\xff\xe0" + _fill(tag, n)


def webp(tag="a", n=64):
    body = b"WEBP" + _fill(tag, n)
    return b"RIFF" + struct.pack("<I", len(body)) + body


MAKERS = {"gif": gif, "png": png, "jpg": jpg, "webp": webp}
HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00"


def sha10(b):
    return hashlib.sha1(b).hexdigest()[:10]


def b64(b):
    return base64.b64encode(b).decode("ascii")


# ── 目录文件与图片目录 ───────────────────────────────────────────────────────

def dumps(lib):
    return json.dumps(lib, indent=2, ensure_ascii=False)


def raw(paths):
    return paths.json.read_bytes()


def load(paths):
    return json.loads(paths.json.read_text(encoding="utf-8"))


def write_lib(paths, lib, trailing_newline=False):
    text = dumps(lib) + ("\n" if trailing_newline else "")
    paths.json.write_bytes(text.encode("utf-8"))
    if os.name == "posix":
        os.chmod(paths.json, 0o644)


def put_image(paths, group, name, data):
    d = paths.dir / group
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(data)
    return str(d / name)


def base(p):
    """条目 path 的最后一段（/ 与 \\ 都当分隔符）。"""
    return p.replace("\\", "/").rsplit("/", 1)[-1]


def sample_library(paths):
    """样本库（图片也真的写到图片根里）：sfw 三条完整 + 一条不完整 + 一个不是对象的；nsfw 两条；
    一条带多余的键；顶层多一个键。键顺序同接口文件 §2。"""
    sfw, nsfw = [], []
    for i in range(3):
        url = "https://media.stk-sample.example/s/%d.gif" % i
        data = gif("s%d" % i, 40 + i)
        name = "%03d_%s.gif" % (i, sha10(url.encode()))
        sfw.append({"url": url, "label": "占位%d" % i, "path": put_image(paths, "sfw", name, data), "size": len(data)})
    sfw[1]["note"] = "占位备注"
    sfw.insert(2, {"url": "https://media.stk-sample.example/s/x.gif", "path": str(paths.dir / "sfw" / "zz.gif")})
    sfw.append("占位串")
    for i, cat in enumerate(("占位类甲", "占位类乙")):
        url = "https://media.stk-sample.example/n/%d.png" % i
        data = png("n%d" % i, 50 + i)
        name = "%03d_%s.png" % (i, sha10(url.encode()))
        nsfw.append({"url": url, "label": "成人占位%d" % i, "category": cat,
                     "path": put_image(paths, "nsfw", name, data), "size": len(data)})
    return {"sfw": sfw, "version": 1, "nsfw": nsfw}


def listing(d):
    """目录里的全部名字（含隐藏文件）；不是目录时为空集。"""
    return set(os.listdir(str(d))) if os.path.isdir(str(d)) else set()


def stray_temps(paths):
    """目录文件所在目录与两个档目录里遗留的临时文件。"""
    out = []
    for d in (paths.shared, paths.dir / "sfw", paths.dir / "nsfw"):
        out += [n for n in listing(d) if n.endswith(".tmp")]
    return out


def all_image_digests(root):
    """图片根下（含 .removed）所有普通文件的内容摘要，按多重集计。"""
    out = []
    for d, _dirs, files in os.walk(str(root)):
        for f in files:
            with open(os.path.join(d, f), "rb") as fh:
                out.append(hashlib.sha1(fh.read()).hexdigest())
    return sorted(out)


# ── 接口调用 ─────────────────────────────────────────────────────────────────

def item_api(group, name):
    return "/api/console/stickers/%s/%s" % (group, name)


def upload_body(data, group="sfw", label="占位", category=None, filename="a.gif"):
    body = {"group": group, "label": label, "filename": filename,
            "data": data if isinstance(data, str) else b64(data)}
    if category is not None:
        body["category"] = category
    return body


def upload(c, data, group="sfw", label="占位", category=None, filename="a.gif"):
    return c.post(UPLOAD, json=upload_body(data, group, label, category, filename))


def link(c, url, group="sfw", label="占位", category=None):
    body = {"group": group, "label": label, "url": url}
    if category is not None:
        body["category"] = category
    return c.post(LINK, json=body)


def ok_item(r):
    """断言 200 {"ok": true, "item": {...}}，返回 item。"""
    b = r.get_json(silent=True)
    assert r.status_code == 200, (r.status_code, b if b is not None else r.get_data(as_text=True)[:500])
    assert set(b) == {"ok", "item"} and b["ok"] is True, b
    assert set(b["item"]) == ITEM_KEYS, b["item"]
    return b["item"]


def err(r, code, status, message=None, prefix=None, contains=None):
    """断言业务错误体：状态码、错误码、键集合；可选核对文案。返回响应体。"""
    b = r.get_json(silent=True)
    assert r.status_code == status, (r.status_code, b if b is not None else r.get_data(as_text=True)[:500])
    assert isinstance(b, dict), b
    assert b.get("ok") is False and b.get("code") == code, b
    assert isinstance(b.get("message"), str) and b["message"], b
    assert set(b) == ({"ok", "code", "message", "existing"} if code == "duplicate" else {"ok", "code", "message"}), b
    if message is not None:
        assert b["message"] == message, b["message"]
    if prefix is not None:
        assert b["message"].startswith(prefix), b["message"]
    if contains is not None:
        assert contains in b["message"], b["message"]
    return b


def sized(prefix, n):
    """恰好 n 字节的 JSON 体：prefix 是开头的几个键，后面用 pad 填满。"""
    head, tail = prefix + ', "pad": "', '"}'
    text = head + "x" * (n - len(head.encode()) - len(tail.encode())) + tail
    assert len(text.encode()) == n
    return text


# ── 假图片服务器与两个替身 ───────────────────────────────────────────────────

HOSTS = {
    "pics.stk-test.example": ["93.184.216.34"], "cdn.stk-test.example": ["93.184.216.35"],
    "hop.stk-test.example": ["93.184.216.36"], "two.stk-test.example": ["93.184.216.37", "93.184.216.38"],
    "fake.stk-test.example": ["198.18.0.9"], "fake2.stk-test.example": ["198.19.255.1"],
    "v6pub.stk-test.example": ["2606:2800:220:1:248:1893:25c8:1946"],
    "inner.stk-test.example": ["10.1.2.3"], "loop.stk-test.example": ["127.0.0.1"], "v6loop.stk-test.example": ["::1"],
    "cgnat.stk-test.example": ["100.64.0.5"], "mapped.stk-test.example": ["::ffff:10.0.0.1"],
    "mixed.stk-test.example": ["93.184.216.34", "192.168.1.5"], "ll.stk-test.example": ["169.254.1.1"],
    "mcast.stk-test.example": ["224.0.0.1"], "zero.stk-test.example": ["0.0.0.0"], "ula.stk-test.example": ["fd00::1"],
    "v6ll.stk-test.example": ["fe80::1%en0"], "reserved.stk-test.example": ["240.0.0.1"],
}


class _H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        srv = self.server
        route = urlsplit(self.path).path
        srv.requests.append({"line": self.path, "route": route,
                             "headers": {k.lower(): v for k, v in self.headers.items()}})
        spec = srv.routes.get(route, ("body", 404, {}, b"nope"))
        try:
            getattr(self, "_" + spec[0])(*spec[1:])
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _body(self, status, headers, body):
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, status, location):
        self._body(status, {"Location": location}, b"")

    def _stall_headers(self):
        self.server.stop.wait(30)

    def _stall_body(self):
        self.send_response(200)
        self.send_header("Content-Length", "100")
        self.end_headers()
        self.wfile.write(b"GIF89a")
        self.wfile.flush()
        self.server.stop.wait(30)

    def _drip_body(self, interval):
        self.send_response(200)
        self.send_header("Content-Length", "1000")
        self.end_headers()
        self.wfile.write(b"GIF89a")
        for _ in range(994):
            self.wfile.flush()
            if self.server.stop.wait(interval):
                return
            self.wfile.write(b"x")

    def _drip_headers(self, interval):
        self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
        for _ in range(1000):
            self.wfile.flush()
            if self.server.stop.wait(interval):
                return
            self.wfile.write(b"a")

    def _huge(self, total, declared):
        self.send_response(200)
        if declared:
            self.send_header("Content-Length", str(total))
        else:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        chunk = b"GIF89a" + b"z" * (64 * 1024 - 6)
        sent = 0
        while sent < total and not self.server.stop.is_set():
            self.wfile.write(chunk)
            sent += len(chunk)


class Fake:
    """127.0.0.1:<端口 0> 上的假图片服务器，同时提供两个替身。"""

    def __init__(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
        self.srv.daemon_threads = True
        self.srv.requests, self.srv.routes, self.srv.stop = [], {}, Event()
        self.port = self.srv.server_address[1]
        Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.resolved = []   # 每次"解析"替身收到的主机名
        self.opened = []     # 每次"打开连接"替身收到的 {"host", "addr", "timeout"}
        self.no_redirect = None

    @property
    def requests(self):
        return self.srv.requests

    def route(self, path, *spec):
        self.srv.routes[path] = spec

    def png(self, path, tag="p", ctype="image/png"):
        data = png(tag)
        self.route(path, "body", 200, {"Content-Type": ctype}, data)
        return data

    def hosts_seen(self):
        return [r["headers"].get("x-stk-host") for r in self.requests]

    def resolve(self, host, port, family=0, type=0, proto=0, flags=0):
        self.resolved.append(host)
        if host not in HOSTS:
            raise socket.gaierror(socket.EAI_NONAME, "unknown host")
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 (ip, port, 0, 0) if ":" in ip else (ip, port)) for ip in HOSTS[host]]

    def open(self, req, timeout, addr):
        """打开连接替身：记下 addr；连接改到假服务器，原主机名放进 X-Stk-Host；不用代理，不自动跟随跳转。"""
        parts = urlsplit(req.full_url)
        self.opened.append({"host": parts.hostname, "addr": addr, "timeout": timeout})
        target = urlunsplit(("http", "127.0.0.1:%d" % self.port, parts.path or "/", parts.query, ""))
        headers = dict(req.header_items())
        headers["X-stk-host"] = parts.hostname
        new = urllib.request.Request(target, headers=headers, method=req.get_method())
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), self.no_redirect())
        return opener.open(new, timeout=timeout)

    def close(self):
        self.srv.stop.set()
        self.srv.shutdown()
        self.srv.server_close()
