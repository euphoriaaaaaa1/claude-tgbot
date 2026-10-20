# -*- coding: utf-8 -*-
"""管理台的生命周期接口（INTERFACE §2.4）：只做 HTTP 翻译。

显示名、启停、新建、切换、归档、恢复、删除、校验令牌、新建表单、操作记录的受理检查与后台各步在
``adapters/lifecycle_flow.py`` 与 ``adapters/ops.py``。分层同 ``console_routes.py``：本文件只许导入
``adapters`` 包、Flask 与标准库；登录门、跨站校验、供应商模式可达面都在 ``moments/web.py`` 对本蓝图生效。
两个蓝图互不导入。

生活设定预置清单（``GET /api/console/life_presets``）在 T16 补上；它只读 ``configs/life_presets.json``，
不碰适配器，失败也按约定恒回 200（status=missing/invalid 放在响应体里）。
"""
import logging
import re

from flask import Blueprint, jsonify, request

from adapters import ops
from adapters.base import display_name_error
from adapters.guard import scrub
from adapters.lifecycle_flow import (INTERNAL_MESSAGE, Reject, TelegramCheckFailed, check_token, create_form,
                                     fail_body, get_display_name, list_archives, log_id, save_display_name,
                                     set_enabled, start_archive, start_create, start_delete, start_restore,
                                     start_switch, use_telegram_get_me)
from moments import telegram_check
from moments.request_body import BodyError, json_object

import life_presets

lifecycle_bp = Blueprint("lifecycle", __name__)
_log = logging.getLogger(__name__)

# 新建/切换/归档/删除请求体上限 64 KiB（INTERFACE §0.3）；人设全文的 256 KiB
# 上限由适配器在 JSON 解析后单独校验，不能据此放宽请求体。
MAX_LIFECYCLE_BODY = 64 * 1024
MAX_CREATE_BODY = MAX_LIFECYCLE_BODY
_LIMIT_RE = re.compile(r"[0-9]{1,2}")


def _tg_get_me(token, *, rate_key):
    """把 getMe 客户端的三类结果翻成编排层认得的固定形状（adapters 不导入 moments）。"""
    try:
        return telegram_check.get_me(token, rate_key=rate_key)
    except telegram_check.RateLimited:
        raise TelegramCheckFailed("too_many", 429, telegram_check.TOO_MANY_MESSAGE) from None
    except telegram_check.TokenRejected:
        raise TelegramCheckFailed("token_rejected", 422, telegram_check.REJECTED_MESSAGE) from None
    except telegram_check.TelegramUnreachable:
        raise TelegramCheckFailed("telegram_unreachable", 503, telegram_check.UNREACHABLE_MESSAGE) from None


use_telegram_get_me(_tg_get_me)


def _rate_key() -> str:
    """限速键：登录会话 cookie（登录门保证有）；没有时退回固定键，同进程共用。"""
    return telegram_check.rate_key_of(request.cookies.get("cb_provider"))


def _scrub_all(value):
    """回给页面前的每段文字过 guard.scrub（INTERFACE §0.4）。"""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {_scrub_all(k): _scrub_all(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_all(v) for v in value]
    return value


def _fail(code: str, status: int, message: str, fields=None):
    return jsonify(fail_body(code, message, fields)), status


def _json_body(limit: int) -> dict:
    """请求体：大小上限 → JSON 内容类型 → JSON 对象。内容不进日志。"""
    try:
        return json_object(limit)
    except BodyError as e:
        raise Reject(e.code, e.http_status, e.message) from None


def _display_name_body() -> str:
    """请求体只许 {"display_name": 字符串}；返回去掉首尾空白后的值。显示名不进日志。"""
    body = _json_body(MAX_LIFECYCLE_BODY)
    if any(k != "display_name" for k in body):
        raise Reject("invalid", 422, "只许修改 display_name")
    err = display_name_error(body.get("display_name"))
    if err:
        raise Reject("invalid", 422, err, {"display_name": err})
    return body["display_name"].strip()


# ── 显示名（INTERFACE §2.4）：停用的 bot 也能读写 ─────────────────────────────

@lifecycle_bp.route("/api/console/bots/<backend>/<bot_id>/profile", methods=["GET"])
def api_get_profile(backend, bot_id):
    try:
        return jsonify(_scrub_all({"ok": True, "display_name": get_display_name(backend, bot_id)}))
    except Reject as r:
        return _fail(r.code, r.http_status, r.message, r.fields)
    except Exception as e:
        _log.error("console 读显示名失败 %r/%r：%s", log_id(backend), log_id(bot_id), type(e).__name__)
        return _fail("internal", 500, INTERNAL_MESSAGE)


@lifecycle_bp.route("/api/console/bots/<backend>/<bot_id>/profile", methods=["PUT"])
def api_set_profile(backend, bot_id):
    try:
        value = _display_name_body()
        out = {"ok": True}
        out.update(save_display_name(backend, bot_id, value))
        return jsonify(_scrub_all(out))
    except Reject as r:
        return _fail(r.code, r.http_status, r.message, r.fields)
    except Exception as e:
        _log.error("console 保存显示名失败 %r/%r：%s", log_id(backend), log_id(bot_id), type(e).__name__)
        return _fail("internal", 500, INTERNAL_MESSAGE)


# ── 启停（INTERFACE §2.4）：请求体 {"enabled": 布尔, "confirm_other_side_unknown"?: true} ──

@lifecycle_bp.route("/api/console/bots/<backend>/<bot_id>/enabled", methods=["POST"])
def api_set_enabled(backend, bot_id):
    try:
        body = _json_body(MAX_LIFECYCLE_BODY)
        out = {"ok": True}
        out.update(set_enabled(backend, bot_id, body.get("enabled"),
                               confirm_other_side_unknown=body.get("confirm_other_side_unknown") is True))
        return jsonify(out)
    except Reject as r:
        return _fail(r.code, r.http_status, r.message, r.fields)
    except Exception as e:
        _log.error("console 启停失败 %r/%r：%s", log_id(backend), log_id(bot_id), type(e).__name__)
        return _fail("internal", 500, INTERNAL_MESSAGE)


# ── 校验令牌（INTERFACE §2.4）：同步接口，最长 10 秒；令牌不进日志、不回显 ──────────

@lifecycle_bp.route("/api/console/telegram/check", methods=["POST"])
def api_check_telegram():
    try:
        body = _json_body(MAX_LIFECYCLE_BODY)
        out = {"ok": True}
        out.update(check_token(body.get("telegram_token"), _rate_key()))
        return jsonify(_scrub_all(out))
    except Reject as r:
        return _fail(r.code, r.http_status, r.message, r.fields)
    except Exception as e:
        _log.error("console 校验令牌失败：%s", type(e).__name__)
        return _fail("internal", 500, INTERNAL_MESSAGE)


# ── 新建表单数据（INTERFACE §2.4）：不分底层，只读 ───────────────────────────────

@lifecycle_bp.route("/api/console/create_form", methods=["GET"])
def api_create_form():
    try:
        out = {"ok": True}
        out.update(create_form())
        return jsonify(_scrub_all(out))
    except Exception as e:
        _log.error("console 读新建表单失败：%s", type(e).__name__)
        return _fail("internal", 500, INTERNAL_MESSAGE)


# ── 生活设定预置清单（INTERFACE §2.5）：不分底层、只读、每次现读文件；除登录门外恒回 200 ──

@lifecycle_bp.route("/api/console/life_presets", methods=["GET"])
def api_life_presets():
    out = {"ok": True}
    out.update(life_presets.load())   # load() 自己兜住一切读取与校验错误，不抛
    return jsonify(out)


# ── 新建（INTERFACE §2.4）：受理检查全过回 202，后台任务执行 ─────────────────────

@lifecycle_bp.route("/api/console/bots", methods=["POST"])
def api_create_bot():
    try:
        body = _json_body(MAX_CREATE_BODY)
        return accepted(start_create(body, _rate_key()))
    except Reject as r:
        return _fail(r.code, r.http_status, r.message, r.fields)
    except Exception as e:
        _log.error("console 新建受理失败：%s", type(e).__name__)
        return _fail("internal", 500, INTERNAL_MESSAGE)


# ── 切换底层（INTERFACE §2.4）：受理检查全过回 202，后台任务执行 ──────────────────

@lifecycle_bp.route("/api/console/bots/<backend>/<bot_id>/switch", methods=["POST"])
def api_switch(backend, bot_id):
    try:
        body = _json_body(MAX_LIFECYCLE_BODY)
        return accepted(start_switch(backend, bot_id, body, _rate_key()))
    except Reject as r:
        return _fail(r.code, r.http_status, r.message, r.fields)
    except Exception as e:
        _log.error("console 切换受理失败 %r/%r：%s", log_id(backend), log_id(bot_id), type(e).__name__)
        return _fail("internal", 500, INTERNAL_MESSAGE)


# ── 操作记录（INTERFACE §2.4）：只读两条 + “重新收拾” ────────────────────────────

def accepted(op_id: str):
    """后台任务受理成功的回包：202 ``{"ok", "op_id", "op_url"}``。"""
    return jsonify({"ok": True, "op_id": op_id, "op_url": "/api/console/ops/" + op_id}), 202


def _check_op_id(op_id: str) -> None:
    if ops.OP_ID_RE.fullmatch(op_id) is None:
        raise Reject("invalid", 422, ops.BAD_OP_ID_MESSAGE)


@lifecycle_bp.route("/api/console/ops", methods=["GET"])
def api_list_ops():
    try:
        raw = request.args.get("limit")
        if raw is None:
            limit = ops.DEFAULT_LIMIT
        elif _LIMIT_RE.fullmatch(raw) and 1 <= int(raw) <= ops.MAX_LIMIT:
            limit = int(raw)
        else:
            raise Reject("invalid", 422, ops.LIMIT_MESSAGE)
        return jsonify({"ok": True, "ops": ops.list_ops(active=request.args.get("active") == "1", limit=limit)})
    except Reject as r:
        return _fail(r.code, r.http_status, r.message, r.fields)
    except Exception as e:
        _log.error("console 读操作列表失败：%s", type(e).__name__)
        return _fail("internal", 500, INTERNAL_MESSAGE)


@lifecycle_bp.route("/api/console/ops/<op_id>", methods=["GET"])
def api_get_op(op_id):
    try:
        # 只读查询不因编号格式拒绝：格式不对与查无此编号一样按 op_not_found 404，
        # 避免把“不存在的操作”暴露成 422（INTERFACE §2.4 的 404 op_not_found）。
        op = ops.get_op(op_id)
        if op is None:
            raise Reject(ops.OpNotFound.code, ops.OpNotFound.http_status, ops.NOT_FOUND_MESSAGE)
        return jsonify({"ok": True, "op": op})
    except Reject as r:
        return _fail(r.code, r.http_status, r.message, r.fields)
    except Exception as e:
        _log.error("console 读操作记录 %r 失败：%s", log_id(op_id), type(e).__name__)
        return _fail("internal", 500, INTERNAL_MESSAGE)


@lifecycle_bp.route("/api/console/ops/<op_id>/resolve", methods=["POST"])
def api_resolve_op(op_id):
    """顺序：编号格式（422）→ 请求体 → 记录在不在（404）→ 是不是 needs_attention（422）→ 锁（409）。"""
    try:
        _check_op_id(op_id)
        if request.content_length:
            _json_body(MAX_LIFECYCLE_BODY)
        try:
            ops.resolve(op_id)
        except (ops.OpNotFound, ops.NotResolvable) as e:
            raise Reject(e.code, e.http_status, e.message)
        except ops.LifecycleBusy as e:
            raise Reject("busy", 409, e.message)
        return accepted(op_id)
    except Reject as r:
        return _fail(r.code, r.http_status, r.message, r.fields)
    except Exception as e:
        _log.error("console 重新收拾 %r 失败：%s", log_id(op_id), type(e).__name__)
        return _fail("internal", 500, INTERNAL_MESSAGE)


# ── 归档、归档清单、恢复、删除（INTERFACE §2.4）：受理检查全过回 202 ─────────────────

@lifecycle_bp.route("/api/console/bots/<backend>/<bot_id>/archive", methods=["POST"])
def api_archive(backend, bot_id):
    try:
        _json_body(MAX_LIFECYCLE_BODY)
        return accepted(start_archive(backend, bot_id))
    except Reject as r:
        return _fail(r.code, r.http_status, r.message, r.fields)
    except Exception as e:
        _log.error("console 归档受理失败 %r/%r：%s", log_id(backend), log_id(bot_id), type(e).__name__)
        return _fail("internal", 500, INTERNAL_MESSAGE)


@lifecycle_bp.route("/api/console/bots/<backend>/<bot_id>/delete", methods=["POST"])
def api_delete(backend, bot_id):
    try:
        body = _json_body(MAX_LIFECYCLE_BODY)
        return accepted(start_delete(backend, bot_id, body.get("confirm")))
    except Reject as r:
        return _fail(r.code, r.http_status, r.message, r.fields)
    except Exception as e:
        _log.error("console 删除受理失败 %r/%r：%s", log_id(backend), log_id(bot_id), type(e).__name__)
        return _fail("internal", 500, INTERNAL_MESSAGE)


@lifecycle_bp.route("/api/console/archives/<backend>/<archive_id>/restore", methods=["POST"])
def api_restore(backend, archive_id):
    try:
        _json_body(MAX_LIFECYCLE_BODY)
        return accepted(start_restore(backend, archive_id))
    except Reject as r:
        return _fail(r.code, r.http_status, r.message, r.fields)
    except Exception as e:
        _log.error("console 恢复受理失败 %r/%r：%s", log_id(backend), log_id(archive_id), type(e).__name__)
        return _fail("internal", 500, INTERNAL_MESSAGE)


@lifecycle_bp.route("/api/console/archives", methods=["GET"])
def api_archives():
    try:
        out = {"ok": True}
        out.update(list_archives())
        return jsonify(out)
    except Exception as e:
        _log.error("console 读归档清单失败：%s", type(e).__name__)
        return _fail("internal", 500, INTERNAL_MESSAGE)
