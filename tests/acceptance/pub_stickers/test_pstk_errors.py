# -*- coding: utf-8 -*-
"""跨接口的错误契约与约束（接口文件 §0、§1.4-1.6 请求体上限；行为清单 12 的请求体部分、14、29）：
请求体上限、库坏了五个接口同口径、未预料异常不回显、日志不记敏感内容、模块的依赖边界。"""
import ast
import io
import json
import logging
import os
import sys

import pytest

import pstk_helpers as K

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
ITEM = K.item_api("sfw", "000_aaaaaaaaaa.gif")


# ── 请求体上限 ───────────────────────────────────────────────────────────────

LIMITS = [("POST", K.UPLOAD, 14 * K.MiB), ("POST", K.LINK, 64 * 1024), ("PATCH", ITEM, 64 * 1024)]


@pytest.mark.parametrize("method,path,limit", LIMITS, ids=["upload", "link", "patch"])
def test_请求体比上限多1字节_413_请求体太大_不建目录文件(c, paths, method, path, limit):
    r = c.open(path, method=method, data=K.sized('{"group": "bad", "label": ""', limit + 1),
               content_type="application/json")
    assert r.status_code == 413
    assert r.get_json() == {"ok": False, "code": "too_large", "message": "请求体太大"}
    assert not paths.json.exists()


@pytest.mark.parametrize("method,path,limit", LIMITS, ids=["upload", "link", "patch"])
def test_请求体恰好等于上限_过了大小这关(c, paths, method, path, limit):
    r = c.open(path, method=method, data=K.sized('{"group": "bad", "label": ""', limit),
               content_type="application/json")
    b = r.get_json(silent=True) or {}
    assert r.status_code in (404, 422) and b.get("ok") is False and b.get("code") in ("invalid", "not_found"), \
        (r.status_code, b)
    assert not paths.json.exists()


def test_没有声明长度的请求体_读到上限就停_413(c, paths):
    data = K.sized('{"group": "sfw", "label": "占位"', 64 * 1024 + 100).encode()
    r = c.post(K.LINK, input_stream=io.BytesIO(data), content_type="application/json",
               environ_overrides={"wsgi.input_terminated": True})
    assert r.status_code == 413 and r.get_json()["code"] == "too_large"


# ── 库坏了（行为 14）────────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path,kind", [
    ("GET", K.LIST, "list"), ("POST", K.UPLOAD, "upload"), ("POST", K.LINK, "link"),
    ("PATCH", ITEM, "patch"), ("DELETE", ITEM, "delete"),
], ids=["list", "upload", "link", "patch", "delete"])
def test_库是坏JSON_五个接口都500_library_broken_文件逐字节不变(c, paths, method, path, kind):
    paths.json.write_bytes(b'{"sfw": [}')
    body = {"list": None, "upload": K.upload_body(K.gif("x")),
            "link": {"group": "sfw", "label": "占位", "url": "https://pics.stk-test.example/a.png"},
            "patch": {"label": "x"}, "delete": {}}[kind]
    r = c.get(path) if body is None else c.open(path, method=method, json=body)
    K.err(r, "library_broken", 500, contains="不是合法的 JSON")
    assert paths.json.read_bytes() == b'{"sfw": [}'


# ── 未预料的异常（行为 29）──────────────────────────────────────────────────

SENTINEL = "BOOM-SENTINEL-6b2e"
SECRET = "secret-trace-9d1f"


@pytest.fixture
def decode_explodes(paths, monkeypatch):
    """目录文件里带哨兵串；任何 JSON 解码器碰到它就抛 RuntimeError（与解析错误无关的意外异常）。
    请求体的 JSON 不带哨兵，照常解析。"""
    K.write_lib(paths, {"sfw": [], "nsfw": [], "note": SENTINEL})
    real = json.decoder.JSONDecoder.decode

    def decode(self, s, *a, **kw):
        if isinstance(s, str) and SENTINEL in s:
            raise RuntimeError(SECRET)
        return real(self, s, *a, **kw)
    monkeypatch.setattr(json.decoder.JSONDecoder, "decode", decode)


@pytest.mark.parametrize("method,path,kind", [
    ("GET", K.LIST, "list"), ("POST", K.UPLOAD, "upload"), ("POST", K.LINK, "link"),
    ("PATCH", ITEM, "patch"), ("DELETE", ITEM, "delete"),
], ids=["list", "upload", "link", "patch", "delete"])
def test_未预料异常_500_internal固定文案_不回显异常文字(c, paths, decode_explodes, method, path, kind):
    body = {"list": None, "upload": K.upload_body(K.gif("x")),
            "link": {"group": "sfw", "label": "占位", "url": "https://pics.stk-test.example/a.png"},
            "patch": {"label": "x"}, "delete": {}}[kind]
    r = c.get(path) if body is None else c.open(path, method=method, json=body)
    assert r.status_code == 500 and r.get_json() == K.INTERNAL
    assert SECRET not in r.get_data(as_text=True)


# ── 日志（行为 29）──────────────────────────────────────────────────────────

def test_日志不记base64_标签_链接路径与查询_只记主机名(c, paths, net, store, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    data = K.gif("LOG-SENTINEL-DATA-" * 4)
    b64 = K.b64(data)
    K.upload(c, b64, label="LABEL-SENTINEL-41ad")
    K.upload(c, b64, label="LABEL-SENTINEL-41ad")   # 重复
    K.upload(c, K.b64(b"\x00\x00\x00\x18ftypheic" + b"PAYLOAD-SENTINEL" * 8), label="LABEL-SENTINEL-41ad")
    net.png("/PATH-SENTINEL-7c1e/a.png")
    secret_url = "https://pics.stk-test.example/PATH-SENTINEL-7c1e/a.png?token=QUERY-SENTINEL-55"
    assert K.link(c, secret_url, label="链接").status_code == 200
    K.link(c, "https://pics.stk-test.example/PATH-SENTINEL-7c1e/404.png?token=QUERY-SENTINEL-55&b=1", label="链接")

    def crash(req, timeout, addr):
        raise RuntimeError("boom " + req.full_url)
    monkeypatch.setattr(store, "open_url", crash)
    K.link(c, secret_url + "&c=1", label="链接")
    text = caplog.text
    for s in (b64[:24], b64[-24:], "PAYLOAD-SENTINEL", "PATH-SENTINEL", "QUERY-SENTINEL", "LABEL-SENTINEL"):
        assert s not in text, "日志里出现了不该记的内容：%s" % s[:16]
    assert "pics.stk-test.example" in text


# ── 依赖边界与不写死本机路径（静态）──────────────────────────────────────────

def _imports(path):
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), path)
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mod = "." * node.level + (node.module or "")   # 相对导入保留前导点，不会被当成标准库
            out |= {mod} | {"%s.%s" % (mod.rstrip("."), a.name) if node.module else mod + a.name for a in node.names}
    return out


STORE = os.path.join(REPO, "moments", "sticker_store.py")
ROUTES = os.path.join(REPO, "moments", "sticker_routes.py")


def test_sticker_store只用标准库加仓内platform_lock():
    for n in _imports(STORE):
        root = n.split(".")[0]
        assert root in sys.stdlib_module_names or root == "platform_lock", "sticker_store 导入了：%s" % n


def test_sticker_store不直接导入fcntl或msvcrt_平台锁经platform_lock():
    roots = {n.split(".")[0] for n in _imports(STORE)}
    assert "fcntl" not in roots and "msvcrt" not in roots, roots
    assert "platform_lock" in roots


def test_sticker_store不写死本机路径_缺省路径只有两处():
    with open(STORE, encoding="utf-8") as fh:
        src = fh.read()
    homes = {n.value for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Constant) and isinstance(n.value, str)
             and n.value.startswith("~")}
    assert "/Users/" not in src and "/home/" not in src
    assert homes == {"~/.claude/channels/_shared/stickers.json", "~/resource/stickers"}


def test_sticker_routes只导入flask_标准库与仓内moments模块():
    for n in _imports(ROUTES):
        root = n.split(".")[0]
        assert n.startswith(".") or root in sys.stdlib_module_names or root in ("flask", "moments", "platform_lock"), n
