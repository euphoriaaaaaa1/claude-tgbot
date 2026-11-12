# -*- coding: utf-8 -*-
"""表情包管理页的壳：一个页面、一个图片路由、五个接口。

表情库的读写全在 moments/sticker_store.py；登录门、跨站核对、可达面闸门在 moments/web.py 里对本蓝图生效。
错误形状同总览页：{"ok": false, "code", "message"}，duplicate 另带 existing。
日志里不出现 base64 内容与请求体；链接只记主机名。
"""
import base64
import binascii
import logging

from flask import Blueprint, abort, jsonify, render_template, send_file

from moments import request_body
from moments import sticker_store as store

stickers_bp = Blueprint("stickers", __name__)
_log = logging.getLogger(__name__)

INTERNAL_MESSAGE = "服务器内部错误，请稍后再试"
MAX_UPLOAD_BODY = 14 * 1024 * 1024   # 10 MiB 的图转成 base64 约 13.4 MiB，再加 JSON 外壳
MAX_SMALL_BODY = 64 * 1024
_HTTP = {"invalid": 422, "too_large": 413, "unsupported_type": 422, "duplicate": 409, "not_found": 404,
         "ambiguous": 409, "bad_url": 422, "blocked_host": 422, "fetch_failed": 502, "timeout": 504,
         "library_broken": 500, "internal": 500}


class _Reject(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _fail(code: str, message: str, existing=None):
    body = {"ok": False, "code": code, "message": message}
    if existing:
        body["existing"] = existing
    return jsonify(body), _HTTP.get(code, 500)


def _store_fail(e: store.StickerError):
    return _fail(e.code, e.message, e.existing if e.code == "duplicate" else None)


def _json_body(limit: int) -> dict:
    """请求体：大小上限 → 内容类型 → JSON 对象，口径与各蓝图共用的 moments.request_body 相同。"""
    try:
        return request_body.json_object(limit)
    except request_body.BodyError as e:
        raise _Reject(e.code, e.message)


def _decode_data(data) -> bytes:
    """图片内容：标准 base64；带 ``data:...;base64,`` 前缀的也认。"""
    if not isinstance(data, str) or not data:
        raise _Reject("invalid", "缺图片内容")
    if data.startswith("data:"):
        head, sep, data = data.partition(",")
        if not sep or not head.endswith(";base64"):
            raise _Reject("invalid", "图片内容必须是 base64")
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise _Reject("invalid", "图片内容不是合法的 base64")


# ── 页面与图片 ───────────────────────────────────────────────────────────────

@stickers_bp.route("/console/stickers")
def stickers_page():
    return render_template("stickers.html")


# merge_slashes=False：带 // 的路径直接不匹配（回 404），不让框架先 308 跳到合并后的地址
@stickers_bp.route("/console/stickers/img/<group>/<name>", merge_slashes=False)
def sticker_image(group, name):
    """只发库里有条目、真实路径就在该档目录里、文件头认得出的图；其余一律 404。"""
    try:
        found = store.image_file(group, name)
    except Exception:
        _log.exception("表情包：取图失败 %s", group)
        found = None
    if found is None:
        abort(404)
    path, mimetype = found
    resp = send_file(path, mimetype=mimetype, conditional=True)
    resp.headers["Cache-Control"] = "private, max-age=3600"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# ── 接口 ─────────────────────────────────────────────────────────────────────

@stickers_bp.route("/api/console/stickers", methods=["GET"])
def api_list():
    try:
        out = {"ok": True}
        out.update(store.library())
        return jsonify(out)
    except store.StickerError as e:
        return _store_fail(e)
    except Exception:
        _log.exception("表情包：读列表失败")
        return _fail("internal", INTERNAL_MESSAGE)


@stickers_bp.route("/api/console/stickers/upload", methods=["POST"])
def api_upload():
    try:
        body = _json_body(MAX_UPLOAD_BODY)
        filename = body.get("filename")
        if not isinstance(filename, str):
            raise _Reject("invalid", "缺文件名")
        group, label, category = store.check_fields(body.get("group"), body.get("label"), body.get("category"))
        item = store.add(group, label, category, _decode_data(body.get("data")))
        return jsonify({"ok": True, "item": item})
    except _Reject as r:
        return _fail(r.code, r.message)
    except store.StickerError as e:
        return _store_fail(e)
    except Exception as e:   # 不记异常原文与请求体
        _log.error("表情包：上传出错（%s）", type(e).__name__)
        return _fail("internal", INTERNAL_MESSAGE)


@stickers_bp.route("/api/console/stickers/link", methods=["POST"])
def api_link():
    host = ""
    try:
        body = _json_body(MAX_SMALL_BODY)
        url = body.get("url")
        if not isinstance(url, str) or not url.strip():
            raise _Reject("invalid", "缺链接")
        host = store.url_host(url)
        item = store.add_from_url(body.get("group"), body.get("label"), body.get("category"), url)
        _log.info("表情包：按链接加入 %s host=%s", item["id"], host)
        return jsonify({"ok": True, "item": item})
    except _Reject as r:
        return _fail(r.code, r.message)
    except store.StickerError as e:
        if e.code in ("bad_url", "blocked_host", "fetch_failed", "timeout", "too_large", "unsupported_type"):
            _log.info("表情包：按链接添加未成 host=%s code=%s", host, e.code)
        return _store_fail(e)
    except Exception as e:   # 链接只记主机名：不记异常原文（可能带完整链接）
        _log.error("表情包：按链接添加出错 host=%s（%s）", host, type(e).__name__)
        return _fail("internal", INTERNAL_MESSAGE)


@stickers_bp.route("/api/console/stickers/<group>/<name>", methods=["PATCH"])
def api_patch(group, name):
    try:
        body = _json_body(MAX_SMALL_BODY)
        item = store.update(group, name, {k: body[k] for k in ("label", "category") if k in body})
        return jsonify({"ok": True, "item": item})
    except _Reject as r:
        return _fail(r.code, r.message)
    except store.StickerError as e:
        return _store_fail(e)
    except Exception:
        _log.exception("表情包：修改失败")
        return _fail("internal", INTERNAL_MESSAGE)


@stickers_bp.route("/api/console/stickers/<group>/<name>", methods=["DELETE"])
def api_delete(group, name):
    try:   # 页面会补一个 {} 体；本接口不读它
        return jsonify({"ok": True, "moved": store.remove(group, name)})
    except store.StickerError as e:
        return _store_fail(e)
    except Exception:
        _log.exception("表情包：移除失败")
        return _fail("internal", INTERNAL_MESSAGE)
