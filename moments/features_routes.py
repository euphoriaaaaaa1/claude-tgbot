"""功能开关接口（公开版 T08；INTERFACE §2.6）：``GET``、``PUT``
``/api/console/bots/<backend>/<bot_id>/features``。

分层同 ``console_routes`` / ``lifecycle_routes``：只导入 ``adapters`` 包与 ``moments.request_body``，
不碰任何底层模块；登录门、跨站核对、供应商模式可达面都在 ``moments/web.py`` 里对本蓝图生效。
"""
import logging

from flask import Blueprint, jsonify, request

from adapters.base import check_features_body
from adapters.lifecycle_flow import INTERNAL_MESSAGE, Reject, call as _call, prepare as _prepare
from moments import request_body
from moments.console_routes import _fail, _scrubbed

features_bp = Blueprint("features", __name__)
_log = logging.getLogger(__name__)

FEATURES_URL = "/api/console/bots/<backend>/<bot_id>/features"
MAX_FEATURES_BODY = 16 * 1024   # INTERFACE §2.6：上限 16 KiB
CAPABILITY = "bot.features"
UNSUPPORTED_MESSAGE = "这个底层不提供功能开关"


def _features_adapter(backend: str, bot_id: str, *, write: bool):
    """写接口查生命周期忙；能力键不是 true → 501，不调适配器。"""
    ad, caps, _levels = _prepare(backend, bot_id, check_duplicate=False, check_busy=write,
                                 check_conflict=False)
    if caps.get(CAPABILITY) is not True:
        raise Reject("unsupported", 501, UNSUPPORTED_MESSAGE)
    return ad


def _ok(result) -> dict:
    out = {"ok": True}
    out.update(result if isinstance(result, dict) else {})
    out["ok"] = True
    return out


@features_bp.route(FEATURES_URL, methods=["GET"])
def api_get_features(backend, bot_id):
    try:
        ad = _features_adapter(backend, bot_id, write=False)
        return jsonify(_scrubbed(_ok(_call(lambda: ad.get_features(bot_id)))))
    except Reject as r:
        return _fail(r.code, r.http_status, r.message, r.fields)
    except Exception as e:
        _log.error("console 读功能开关失败 %r/%r：%s", backend, bot_id, type(e).__name__)
        return _fail("internal", 500, INTERNAL_MESSAGE)


@features_bp.route(FEATURES_URL, methods=["PUT"])
def api_set_features(backend, bot_id):
    """次序：请求体大小 → 请求体形状 → 路径参数 → 生命周期进行中 → 能力键 → 适配器。
    日志只记 bot、改了哪些键与结果，不记请求体。"""
    try:
        try:
            body = request_body.json_object(MAX_FEATURES_BODY)
        except request_body.BodyError as e:
            raise Reject(e.code, e.http_status, e.message) from None
        changes = _call(lambda: check_features_body(body, None))
        ad = _features_adapter(backend, bot_id, write=True)
        out = _ok(_call(lambda: ad.set_features(bot_id, changes)))
        changed = out.get("changed") if isinstance(out.get("changed"), list) else []
        _log.info("功能开关已保存 %s/%s changed=%s applied=%s queued=%s skills_synced=%s", backend, bot_id,
                  ",".join(str(k) for k in changed) or "-", out.get("applied"), out.get("queued"),
                  out.get("skills_synced"))
        return jsonify(_scrubbed(out))
    except Reject as r:
        return _fail(r.code, r.http_status, r.message, r.fields)
    except Exception as e:
        _log.error("console 改功能开关失败 %r/%r：%s", backend, bot_id, type(e).__name__)
        return _fail("internal", 500, INTERNAL_MESSAGE)
