# -*- coding: utf-8 -*-
"""sticker_store 按链接下载的建连细节（白盒补充）：验收只覆盖了缺省 open_url 的 http 直连，这里补 https 直连
（连核对过的地址、SNI 与证书校验按原主机名）与 no_proxy 绕过代理两处内部边界。只连 127.0.0.1 的端口 0。"""
import shutil
import socket
import ssl
import subprocess
import threading
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
