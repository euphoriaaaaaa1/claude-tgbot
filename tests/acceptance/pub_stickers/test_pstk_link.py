# -*- coding: utf-8 -*-
"""按链接添加（接口文件 §1.5，含「建连与代理」D2 与 198.18/15 的 D1；行为清单 15-23）。

真实连接一律落到 127.0.0.1 端口 0 的假服务器：resolve 替身把测试主机名解析成公网样地址，open_url 替身把连接改到
假服务器（不走代理）。被拦的情况断言假服务器一个请求都没收到。另有几条换回缺省 open_url，验证按 addr 直连与走代理。
"""
import socket
import time
import urllib.request

import pytest

import pstk_helpers as K

PICS = "https://pics.stk-test.example"


def _nothing_written(paths):
    assert not paths.json.exists() and K.listing(paths.dir / "sfw") == set() and K.stray_temps(paths) == []


# ── 成功 ─────────────────────────────────────────────────────────────────────

def test_成功_文件名取链接sha1_url去首尾空白_ContentType写html也照文件头收下(c, paths, net):
    data = net.png("/img/a.png", ctype="text/html; charset=utf-8")
    url = PICS + "/img/a.png"
    item = K.ok_item(K.link(c, "  %s  " % url))
    assert item["file"] == "000_%s.png" % K.sha10(url.encode()) and item["url"] == url and item["size"] == len(data)
    assert (paths.dir / "sfw" / item["file"]).read_bytes() == data


def test_成功_条目键顺序url_label_path_size(c, paths, net):
    data = net.png("/img/a.png")
    url = PICS + "/img/a.png"
    item = K.ok_item(K.link(c, url, label="链接的"))
    entry = K.load(paths)["sfw"][-1]
    assert entry == {"url": url, "label": "链接的", "path": str(paths.dir / "sfw" / item["file"]), "size": len(data)}
    assert list(entry) == ["url", "label", "path", "size"]


def test_成功_请求头Accept与浏览器样式UserAgent_只发一次(c, paths, net):
    net.png("/img/a.png")
    K.ok_item(K.link(c, PICS + "/img/a.png"))
    assert len(net.requests) == 1
    h = net.requests[0]["headers"]
    assert h["x-stk-host"] == "pics.stk-test.example" and h["accept"] == "image/*,*/*;q=0.8"
    assert h["user-agent"].startswith("Mozilla/5.0") and "Python" not in h["user-agent"]


def test_默认端口写出来也放行_路径里的中文按UTF8转义(c, paths, net):
    net.png("/a.png", "a")
    net.png("/%E8%A1%A8%E6%83%85.png", "b")
    K.ok_item(K.link(c, "http://pics.stk-test.example:80/a.png"))
    K.ok_item(K.link(c, "https://pics.stk-test.example:443/表情.png", label="第二张"))
    assert [r["route"] for r in net.requests] == ["/a.png", "/%E8%A1%A8%E6%83%85.png"]


def test_解析到公网v6地址_放行(c, paths, net):
    net.png("/ok.png")
    assert K.ok_item(K.link(c, "http://v6pub.stk-test.example/ok.png"))["size"] > 0


# ── 建连（D2）：每跳只解析一次，addr 就是解析给的地址 ─────────────────────────

def test_D2_每跳resolve恰好一次_open_url收到的addr就是解析结果(c, paths, net):
    net.png("/img/a.png")
    K.ok_item(K.link(c, PICS + "/img/a.png"))
    assert net.resolved == ["pics.stk-test.example"]
    assert [o["addr"] for o in net.opened] == ["93.184.216.34"]


def test_D2_跳转时每一跳各解析一次_addr逐跳对应(c, paths, net):
    net.route("/r1", "redirect", 302, "http://cdn.stk-test.example/r2")
    net.png("/r2")
    K.ok_item(K.link(c, PICS + "/r1"))
    assert net.resolved == ["pics.stk-test.example", "cdn.stk-test.example"]
    assert [(o["host"], o["addr"]) for o in net.opened] == [("pics.stk-test.example", "93.184.216.34"),
                                                             ("cdn.stk-test.example", "93.184.216.35")]


def test_D2_缺省open_url按addr直连_Host头是原主机名带端口_不解析原主机名(store, fake, monkeypatch):
    data = fake.png("/x.png")
    seen = []
    real_gai = socket.getaddrinfo

    def spy(host, *a, **kw):
        seen.append(host)
        if host == "img.example.test":
            raise socket.gaierror(socket.EAI_NONAME, "不许解析原主机名")
        return real_gai(host, *a, **kw)
    monkeypatch.setattr(socket, "getaddrinfo", spy)
    req = urllib.request.Request("http://img.example.test:%d/x.png" % fake.port)
    resp = store.open_url(req, 5, "127.0.0.1")
    try:
        body = resp.read()
    finally:
        resp.close()
    assert body == data and len(fake.requests) == 1
    assert fake.requests[0]["headers"]["host"] == "img.example.test:%d" % fake.port
    assert "img.example.test" not in seen


# ── 链接本身不合规（不解析、不发请求）────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "ftp://pics.stk-test.example/a.png", "javascript:alert(1)", "pics.stk-test.example/a.png", "data:image/png;base64,AA",
    "http://user:pw@pics.stk-test.example/a.png", "http://user@pics.stk-test.example/a.png",
    "http://pics.stk-test.example:8080/a.png", "https://pics.stk-test.example:8443/a.png",
    "http://pics.stk-test.example:99999/a.png", "http://pics.stk-test.example:x/a.png",
    "http:///a.png", "http://pics.stk-test.example/a b.png", "http://pics.stk-test.example/a\tb.png",
    "http://[::1/a.png", "https://pics.stk-test.example/" + "a" * 2020,
], ids=["ftp", "javascript", "no_scheme", "data", "userpass", "user", "port8080", "port8443", "port_range",
        "port_text", "no_host", "space", "tab", "bad_bracket", "too_long"])
def test_链接本身不合规_422_bad_url_解析与请求都是零次(c, paths, net, url):
    K.err(K.link(c, url), "bad_url", 422)
    assert net.resolved == [] and net.requests == []
    _nothing_written(paths)


def test_链接长度恰好2048_过了核对真的去取(c, paths, net):
    ok = PICS + "/" + "a" * (2048 - len(PICS) - 1)
    assert len(ok) == 2048
    K.err(K.link(c, ok), "fetch_failed", 502)   # 假服务器回 404
    assert len(net.requests) == 1


def test_链接长度2049_bad_url_不发请求(c, paths, net):
    bad = PICS + "/" + "a" * (2049 - len(PICS) - 1)
    assert len(bad) == 2049
    K.err(K.link(c, bad), "bad_url", 422)
    assert net.resolved == [] and net.requests == []


# ── 主机名核对（不解析、不发请求）────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/a.png", "http://[::1]/a.png", "http://10.0.0.1/a.png", "http://93.184.216.34/a.png",
    "http://2130706433/a.png", "http://0x7f.0.0.1/a.png", "http://127.1/a.png", "http://[::ffff:127.0.0.1]/a.png",
    "http://localhost/a.png", "http://LOCALHOST./a.png", "http://foo.localhost/a.png", "http://printer.local/a.png",
    "http://intranet/a.png", "http://１２７.０.０.１/a.png",
], ids=["v4", "v6", "private_v4", "public_v4", "decimal", "hex", "short", "mapped", "localhost", "localhost_dot",
        "sub_localhost", "dot_local", "no_dot", "fullwidth"])
def test_直接写IP_本机名_不带点的主机名_422_blocked_host_解析零次(c, paths, net, url):
    K.err(K.link(c, url), "blocked_host", 422)
    assert net.resolved == [] and net.requests == []
    _nothing_written(paths)


# ── 解析结果核对 ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("host", ["inner", "loop", "v6loop", "cgnat", "mapped", "mixed", "ll", "mcast", "zero", "ula",
                                  "v6ll", "reserved"])
def test_解析到私有或本机等地址_422_blocked_host_请求零次(c, paths, net, host):
    net.png("/a.png")
    K.err(K.link(c, "https://%s.stk-test.example/a.png" % host), "blocked_host", 422,
          message="这个链接指向本机或内网地址，不能下载")
    assert net.requests == [] and net.opened == []
    _nothing_written(paths)


@pytest.mark.parametrize("host", ["fake", "fake2"], ids=["198.18.0.9", "198.19.255.1"])
def test_D1_解析到198_18网段_公开版不放行_blocked_host_请求零次(c, paths, net, host):
    net.png("/ok.png")
    K.err(K.link(c, "http://%s.stk-test.example/ok.png" % host), "blocked_host", 422,
          message="这个链接指向本机或内网地址，不能下载")
    assert net.requests == [] and net.opened == []
    _nothing_written(paths)


def test_解析不了_502_fetch_failed_找不到这个网站(c, paths, net):
    K.err(K.link(c, "https://nowhere.stk-test.example/a.png"), "fetch_failed", 502,
          message="下载失败：找不到这个网站")
    assert net.requests == []


# ── 跳转 ─────────────────────────────────────────────────────────────────────

def test_跳转三次以内成功_每一跳都重新解析(c, paths, net):
    net.route("/r1", "redirect", 302, "http://cdn.stk-test.example/r2")
    net.route("/r2", "redirect", 301, "/r3")
    net.route("/r3", "redirect", 307, "https://hop.stk-test.example/final.png")
    data = net.png("/final.png")
    url = PICS + "/r1"
    item = K.ok_item(K.link(c, url))
    assert item["url"] == url and item["file"] == "000_%s.png" % K.sha10(url.encode()) and item["size"] == len(data)
    assert [r["route"] for r in net.requests] == ["/r1", "/r2", "/r3", "/final.png"]
    assert net.hosts_seen() == ["pics.stk-test.example", "cdn.stk-test.example", "cdn.stk-test.example",
                                "hop.stk-test.example"]
    assert net.resolved == ["pics.stk-test.example", "cdn.stk-test.example", "cdn.stk-test.example",
                            "hop.stk-test.example"]


def test_第四次跳转_502_跳转超过3次_第5个地址没被请求(c, paths, net):
    for i in range(1, 5):
        net.route("/r%d" % i, "redirect", 302, "/r%d" % (i + 1))
    net.png("/r5")
    K.err(K.link(c, PICS + "/r1"), "fetch_failed", 502, message="下载失败：跳转超过 3 次")
    assert [r["route"] for r in net.requests] == ["/r1", "/r2", "/r3", "/r4"]
    _nothing_written(paths)


@pytest.mark.parametrize("location,code", [
    ("http://127.0.0.1/a.png", "blocked_host"), ("http://inner.stk-test.example/a.png", "blocked_host"),
    ("http://localhost/a.png", "blocked_host"), ("file:///etc/hosts", "bad_url"),
    ("http://pics.stk-test.example:8080/a.png", "bad_url"), ("ftp://pics.stk-test.example/a.png", "bad_url"),
], ids=["ip", "private", "localhost", "file", "port", "ftp"])
def test_跳向被拦地址_422_被拦_后续不请求(c, paths, net, location, code):
    net.route("/start", "redirect", 302, location)
    net.png("/a.png")
    K.err(K.link(c, PICS + "/start"), code, 422)
    assert [r["route"] for r in net.requests] == ["/start"]
    _nothing_written(paths)


def test_跳转没给地址_502_对方要求跳转但没给地址(c, paths, net):
    net.route("/r", "body", 302, {}, b"")
    K.err(K.link(c, PICS + "/r"), "fetch_failed", 502, message="下载失败：对方要求跳转但没给地址（HTTP 302）")


# ── 对方的错误、超大、超时、内容不是图片 ─────────────────────────────────────

@pytest.mark.parametrize("status", [404, 500, 403])
def test_对方回错误码_502_fetch_failed_带状态码(c, paths, net, status):
    net.route("/x.png", "body", status, {}, b"err")
    K.err(K.link(c, PICS + "/x.png"), "fetch_failed", 502, message="下载失败：对方回了 HTTP %d" % status)
    _nothing_written(paths)


def test_没声明长度的超大流被中止_413_too_large(c, paths, net):
    net.route("/big.gif", "huge", 11 * K.MiB, False)
    K.err(K.link(c, PICS + "/big.gif"), "too_large", 413, message="图片超过 10 MiB")
    _nothing_written(paths)


def test_声明的长度超过10MiB_不读正文就回413(c, paths, net):
    net.route("/big.gif", "huge", 20 * K.MiB, True)
    t0 = time.monotonic()
    K.err(K.link(c, PICS + "/big.gif"), "too_large", 413)
    assert time.monotonic() - t0 < 5


def test_恰好10MiB的图放行(c, paths, net):
    data = K.png("edge", K.MAX_IMG - 8)
    net.route("/edge.png", "body", 200, {}, data)
    assert K.ok_item(K.link(c, PICS + "/edge.png"))["size"] == K.MAX_IMG


@pytest.fixture
def short_timeouts(store, monkeypatch):
    monkeypatch.setattr(store, "IO_TIMEOUT", 0.5)
    monkeypatch.setattr(store, "TOTAL_TIMEOUT", 1.5)


@pytest.mark.parametrize("spec", [("stall_headers",), ("stall_body",), ("drip_body", 0.2), ("drip_headers", 0.2)],
                         ids=["no_headers", "body_stalls", "body_drips", "headers_drip"])
def test_超时_504_timeout_整体期限加1秒内返回_什么都不写(c, paths, net, short_timeouts, spec):
    net.route("/slow.gif", *spec)
    t0 = time.monotonic()
    K.err(K.link(c, PICS + "/slow.gif"), "timeout", 504, prefix="下载超时")
    assert time.monotonic() - t0 < 1.5 + 1.0 + 1.0   # 整体期限 + 1 秒 + 测试自身开销 1 秒
    _nothing_written(paths)


@pytest.mark.parametrize("body,message,contains", [
    (b"<!doctype html><html><head><title>t</title></head></html>", "这个链接不是图片本身，请复制图片的地址", None),
    (b"\n  <html>", "这个链接不是图片本身，请复制图片的地址", None),
    (K.HEIC, "HEIC 不支持，请先转成 JPG", None),
    (b"plain text", None, "只支持"),
], ids=["html", "html_ws", "heic", "text"])
def test_内容不是图片_422_unsupported_type(c, paths, net, body, message, contains):
    net.route("/p", "body", 200, {"Content-Type": "image/png"}, body)
    K.err(K.link(c, PICS + "/p"), "unsupported_type", 422, message=message, contains=contains)
    _nothing_written(paths)


# ── 查重、库的状态、字段先后 ─────────────────────────────────────────────────

def test_同一链接前后带空白再加_409_duplicate_下载次数不变(c, paths, net):
    net.png("/a.png")
    K.ok_item(K.link(c, PICS + "/a.png", label="第一张"))
    b = K.err(K.link(c, " %s/a.png " % PICS, group="nsfw", category="占位类"), "duplicate", 409)
    assert b["existing"] == {"group": "sfw", "label": "第一张"}
    assert len(net.requests) == 1


def test_下载后内容与库里已有的相同_409_duplicate(c, paths, net):
    data = net.png("/a.png", "same")
    net.route("/b.png", "body", 200, {}, data)
    K.ok_item(K.link(c, PICS + "/a.png", label="第一张"))
    b = K.err(K.link(c, PICS + "/b.png"), "duplicate", 409)
    assert b["existing"] == {"group": "sfw", "label": "第一张"}
    assert len(K.load(paths)["sfw"]) == 1


def test_库坏了先报_500_library_broken_不解析不下载(c, paths, net):
    paths.json.write_bytes(b"{broken")
    net.png("/a.png")
    K.err(K.link(c, PICS + "/a.png"), "library_broken", 500)
    assert net.resolved == [] and net.requests == [] and paths.json.read_bytes() == b"{broken"


@pytest.mark.parametrize("body,message", [
    ({"group": "sfw", "label": "占位"}, "缺链接"),
    ({"group": "sfw", "label": "占位", "url": "  "}, "缺链接"),
    ({"group": "sfw", "label": "占位", "url": 5}, "缺链接"),
    ({"group": "sfw", "label": "", "url": PICS + "/z.png"}, None),
], ids=["no_url", "blank_url", "url_int", "empty_label"])
def test_链接入参不合规_422_invalid(c, paths, net, body, message):
    K.err(c.post(K.LINK, json=body), "invalid", 422, message=message)
    assert net.resolved == [] and not paths.json.exists()


@pytest.mark.parametrize("kw", [{"url": "ftp://x", "label": ""},
                                {"url": PICS + "/a.png", "group": "nsfw", "category": None}],
                         ids=["label_before_url", "category_before_fetch"])
def test_字段先于链接核对_422_invalid_不解析不请求(c, paths, net, kw):
    K.err(K.link(c, **kw), "invalid", 422)
    assert net.resolved == [] and net.requests == []


# ── 设了代理：缺省 open_url 走代理、addr 为 None、解析核对照做 ──────────────────

@pytest.fixture
def proxied(net, store, monkeypatch):
    """换回缺省 open_url（外面包一层只记 addr），进程环境里的 http 代理指向假服务器。"""
    addrs = []

    def spy(req, timeout, addr):
        addrs.append(addr)
        return net.real_open(req, timeout, addr)
    monkeypatch.setattr(store, "open_url", spy)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:%d" % net.port)
    net.addrs = addrs
    return net


def test_设了代理_经代理取图_代理收到完整地址_addr为None(c, paths, proxied):
    data = proxied.png("/via-proxy.png")
    item = K.ok_item(K.link(c, "http://pics.stk-test.example/via-proxy.png"))
    assert item["size"] == len(data)
    assert proxied.requests[0]["line"] == "http://pics.stk-test.example/via-proxy.png"
    assert proxied.addrs == [None]


def test_设了代理_仍解析一次主机名(c, paths, proxied):
    proxied.png("/via-proxy.png")
    K.ok_item(K.link(c, "http://pics.stk-test.example/via-proxy.png"))
    assert proxied.resolved == ["pics.stk-test.example"]


def test_设了代理_解析到内网仍422_blocked_host_请求零次(c, paths, proxied):
    proxied.png("/a.png")
    K.err(K.link(c, "http://inner.stk-test.example/a.png"), "blocked_host", 422)
    assert proxied.requests == [] and proxied.addrs == []


def test_设了代理_缺省open_url不自动跟随跳转(c, paths, proxied):
    proxied.route("/r", "redirect", 302, "http://127.0.0.1/a.png")   # 若被自动跟随，会经代理取到 /a.png
    proxied.png("/a.png")
    K.err(K.link(c, "http://pics.stk-test.example/r"), "blocked_host", 422)
    assert [r["route"] for r in proxied.requests] == ["/r"]
