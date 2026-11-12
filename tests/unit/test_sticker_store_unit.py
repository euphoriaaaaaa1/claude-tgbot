# -*- coding: utf-8 -*-
"""sticker_store 按链接下载的建连细节（白盒补充）：验收只覆盖了缺省 open_url 的 http 直连，这里补 https 直连
（连核对过的地址、SNI 与证书校验按原主机名）与 no_proxy 绕过代理两处内部边界；另补整体超时后断开被放弃的
下载连接、Windows 上读目录文件撞 PermissionError 时短重试。只连 127.0.0.1 的端口 0。"""
import json
import os
import shutil
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from moments import sticker_store as store

HOST = "img.example.test"
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy",
              "NO_PROXY", "no_proxy")


@pytest.fixture(autouse=True)
def _clean_proxy_env(monkeypatch):
    for k in PROXY_VARS:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def no_resolve_host(monkeypatch):
    """原主机名一旦被解析就记下并失败；直连的 IP 字面量照常放行。"""
    seen = []
    real = socket.getaddrinfo

    def spy(host, *a, **kw):
        seen.append(host)
        if host == HOST:
            raise socket.gaierror(socket.EAI_NONAME, "不许解析原主机名")
        return real(host, *a, **kw)
    monkeypatch.setattr(socket, "getaddrinfo", spy)
    return seen


def _serve_once(handler):
    """127.0.0.1:0 上接一个连接，交给 handler(conn)；返回 (端口, 线程)。"""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    srv.settimeout(5)

    def run():
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(5)
            handler(conn)
        srv.close()
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return srv.getsockname()[1], t


def test_https直连_连的是addr_ClientHello里的SNI是原主机名(no_resolve_host):
    hello = bytearray()

    def grab(conn):
        try:
            hello.extend(conn.recv(4096))   # TLS ClientHello 明文，SNI 扩展就在里面
        except OSError:
            pass
    port, t = _serve_once(grab)
    req = urllib.request.Request("https://%s:%d/x.png" % (HOST, port))
    with pytest.raises((urllib.error.URLError, ssl.SSLError, ConnectionError, OSError)):
        store.open_url(req, 3, "127.0.0.1").close()
    t.join(5)
    assert hello[:1] == b"\x16", "没收到 TLS 握手"
    assert HOST.encode() in bytes(hello)
    assert HOST not in no_resolve_host


@pytest.mark.skipif(shutil.which("openssl") is None, reason="需要 openssl 命令生成自签证书")
def test_https直连_证书校验照常进行_自签证书被拒(tmp_path, no_resolve_host):
    key, cert = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-keyout", str(key), "-out", str(cert), "-subj", "/CN=" + HOST],
                   check=True, capture_output=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    names = []
    ctx.sni_callback = lambda sock, name, _ctx: names.append(name)

    def handshake(conn):
        try:
            with ctx.wrap_socket(conn, server_side=True):
                pass
        except (ssl.SSLError, OSError):
            pass
    port, t = _serve_once(handshake)
    req = urllib.request.Request("https://%s:%d/x.png" % (HOST, port))
    with pytest.raises(urllib.error.URLError) as info:
        store.open_url(req, 3, "127.0.0.1").close()
    t.join(5)
    assert isinstance(info.value.reason, ssl.SSLCertVerificationError), info.value.reason
    assert names == [HOST]
    assert HOST not in no_resolve_host


def test_设了代理但主机在no_proxy里_不走代理_按addr直连(monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("no_proxy", "pics.stk-unit.example")
    assert store._via_proxy(urllib.request.Request("http://pics.stk-unit.example/a.png")) is False
    assert store._via_proxy(urllib.request.Request("http://other.stk-unit.example/a.png")) is True
    assert store._via_proxy(urllib.request.Request("https://other.stk-unit.example/a.png")) is False   # 只设了 http


def test_Host头只在非默认端口时带端口():
    for url, netloc in (("http://a.example/x", "a.example"), ("http://a.example:80/x", "a.example"),
                        ("https://a.example:443/x", "a.example"), ("https://a.example:80/x", "a.example:80")):
        parts, host, _port = store._check_url(url)
        assert urllib.parse.urlsplit(store._wire_url(parts, host)).netloc == netloc


# ── S1：整体期限到了，被放弃的下载线程要很快结束，不挂着连接慢慢等对方 ────────────────

def _drip_headers(conn, stopped):
    """只回状态行，然后每 50 毫秒吐一个头部字节、永远不结束头部；对面断开（或 10 秒兜底）就停，记下停的时刻。"""
    try:
        conn.sendall(b"HTTP/1.1 200 OK\r\nX-Pad: ")
        end = time.monotonic() + 10
        while time.monotonic() < end:
            conn.sendall(b"a")
            time.sleep(0.05)
    except OSError:
        pass
    stopped.append(time.monotonic())


@pytest.mark.parametrize("via_proxy", [False, True], ids=["直连", "走代理"])
def test_整体超时后_下载线程的连接被断开_线程很快结束(monkeypatch, via_proxy):
    stopped = []
    port, server = _serve_once(lambda conn: _drip_headers(conn, stopped))
    monkeypatch.setattr(store, "IO_TIMEOUT", 5.0)      # 单次限时远大于整体，确保结束靠的是主线程断连
    monkeypatch.setattr(store, "TOTAL_TIMEOUT", 0.5)
    monkeypatch.setattr(store, "resolve", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))])   # 公网地址，过核对
    if via_proxy:
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:%d" % port)        # addr=None，经“代理”去连
    else:
        real_open = store.open_url

        def to_local(req, timeout, addr):   # 仍用缺省 open_url 直连，只把目标端口换成假服务器
            req.full_url = "http://%s:%d/x.png" % (HOST, port)
            return real_open(req, timeout, "127.0.0.1")
        monkeypatch.setattr(store, "open_url", to_local)

    before = set(threading.enumerate())
    t0 = time.monotonic()
    with pytest.raises(store.StickerError) as info:
        store.fetch("http://%s/x.png" % HOST)
    returned = time.monotonic()
    assert info.value.code == "timeout"
    assert store.TOTAL_TIMEOUT <= returned - t0 < store.TOTAL_TIMEOUT + 3   # 确实等到了期限才返回，且余量留足
    for t in [t for t in threading.enumerate() if t.name == "sticker-fetch" and t not in before]:
        t.join(1.0)
        assert not t.is_alive(), "期限过后下载线程还挂着"
    server.join(2.0)
    assert stopped and stopped[0] - returned < 1.5, "假服务器没看到连接被断开"


def test_dup失败_下载照常成功_只是不登记(monkeypatch):
    body = b"GIF89a" + b"\x00" * 20

    def serve(conn):
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
    port, server = _serve_once(serve)
    monkeypatch.setattr(store, "resolve", lambda *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))])
    real_open = store.open_url

    def to_local(req, timeout, addr):
        req.full_url = "http://%s:%d/x.png" % (HOST, port)
        return real_open(req, timeout, "127.0.0.1")
    monkeypatch.setattr(store, "open_url", to_local)

    def no_dup(self):
        raise OSError(24, "模拟描述符将尽")
    monkeypatch.setattr(socket.socket, "dup", no_dup)

    seen = []
    real_add = store._SocketWatch.add
    monkeypatch.setattr(store._SocketWatch, "add", lambda self, sock: (seen.append(self), real_add(self, sock))[1])
    assert store.fetch("http://%s/x.png" % HOST) == body
    server.join(2.0)
    assert seen and all(not w._dups for w in seen), "dup 失败时不该登记"


# ── C1：Windows 上读目录文件撞上替换时的 PermissionError 要短重试 ────────────────────

def _flaky_open(monkeypatch, path, fails):
    """前 fails 次以 rb 打开 path 时抛 PermissionError，之后照常；返回已抛次数的计数器。"""
    real_open = open
    count = [0]

    def fake(file, mode="r", *a, **kw):
        if isinstance(file, (str, bytes, os.PathLike)) and os.fspath(file) == path and mode == "rb" and count[0] < fails:
            count[0] += 1
            raise PermissionError(13, "模拟被替换中的文件")
        return real_open(file, mode, *a, **kw)
    monkeypatch.setattr("builtins.open", fake)
    return count


@pytest.fixture
def lib_env(tmp_path, monkeypatch):
    path = str(tmp_path / "stickers.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"sfw": [{"label": "a", "path": str(tmp_path / "s" / "sfw" / "000_a.gif")}], "nsfw": []}, f)
    monkeypatch.setenv("STICKERS_JSON", path)
    monkeypatch.setenv("STICKERS_DIR", str(tmp_path / "s"))
    monkeypatch.setattr(store, "_REPLACE_RETRY_SEC", 0.001)
    return path


def test_Windows上读撞PermissionError_短重试后照常读到(monkeypatch, lib_env):
    monkeypatch.setattr(store, "_on_windows", lambda: True)
    count = _flaky_open(monkeypatch, lib_env, store._REPLACE_TRIES - 1)
    assert [i["label"] for i in store.library()["groups"]["sfw"]] == ["a"]
    assert count[0] == store._REPLACE_TRIES - 1


def test_Windows上重试用完仍PermissionError_按原错误处理(monkeypatch, lib_env):
    monkeypatch.setattr(store, "_on_windows", lambda: True)
    count = _flaky_open(monkeypatch, lib_env, store._REPLACE_TRIES)
    with pytest.raises(store.StickerError) as info:
        store.library()
    assert info.value.code == "internal" and count[0] == store._REPLACE_TRIES


def test_非Windows上PermissionError不重试(monkeypatch, lib_env):
    monkeypatch.setattr(store, "_on_windows", lambda: False)
    count = _flaky_open(monkeypatch, lib_env, 1)
    with pytest.raises(store.StickerError) as info:
        store.library()
    assert info.value.code == "internal" and count[0] == 1
