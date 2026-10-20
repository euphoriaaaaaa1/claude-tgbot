# -*- coding: utf-8 -*-
"""总览页与总览接口（INTERFACE §2.2、§2.3）。

只做两件事：提供 ``/console`` 页面壳，和把各底层的 ``meta()/providers()/bots()`` 汇总成
``GET /api/console/overview`` 的回体。分层硬约束：本文件只许导入 ``adapters`` 包、Flask 与
标准库，不得导入任何底层实现；登录门、跨站校验、供应商模式可达面都在 ``moments/web.py``。

生命周期写接口在 ``moments/lifecycle_routes.py``，两个蓝图互不导入。总览里的文字来自底层，
出口前统一过 ``adapters.guard.scrub``；底层多给的键不转给页面。
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from urllib.parse import quote

from flask import Blueprint, abort, jsonify, redirect, render_template, request

from adapters import AdapterError, AdapterUnavailable, UNAVAILABLE_TEXT, registry, valid_bot_id
from adapters.base import person_of, polling_of
from adapters.guard import scrub
from adapters.lifecycle_flow import conflict_of, fail_body

console_bp = Blueprint("console", __name__)
_log = logging.getLogger(__name__)

# 总览读一种底层的总时限（秒）：dsh 一侧最坏要等三次 5 秒，进程内一侧没有超时。
# 没有总时限的话页面反复刷新会把线程堆起来；超时的那一种按 down 显示。
OVERVIEW_TIMEOUT = 12
# 总览接口的 500 文案按 INTERFACE §2.3 固定；生命周期接口沿用编排层的文案。
OVERVIEW_INTERNAL_MESSAGE = "服务器内部错误"
PROVIDERS_ERROR_TEXT = "读供应商清单时出错了"

_BOT_DEFAULTS = (("bot_id", ""), ("display_name", ""), ("state", "broken"), ("state_text", ""),
                 ("provider", None), ("provider_source", "default"), ("pending", None), ("detail", {}))


def _fail(code: str, status: int, message: str, fields=None):
    return jsonify(fail_body(code, message, fields)), status


def _scrubbed(value):
    """回给页面前，把每段文字（含字典的键）过一遍 ``guard.scrub``；数字、布尔、null 原样不动。"""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {_scrubbed(k): _scrubbed(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrubbed(v) for v in value]
    return value


# ── 页面 ───────────────────────────────────────────────────────────────────

@console_bp.route("/console")
def console_page():
    # 页面只是壳；选中哪只由页面自己读 ?b=<底层>/<编号>。服务端不把 b 写进页面。
    return render_template("console.html")


def _selection_ok(backend: str, bot_id: str) -> bool:
    """旧地址带来的选中是否合规：底层在启用名单里、编号合 valid_bot_id。"""
    return backend in registry.enabled_backends() and valid_bot_id(bot_id)


@console_bp.route("/console/bots")
def bots_page():
    # 原 bot 管理页已并进管理台：302 过去；?b=<底层>/<编号> 合规才原样带上，不合规就丢掉。
    parts = request.args.get("b", "").split("/")
    if len(parts) == 2 and _selection_ok(parts[0], parts[1]):
        return redirect(manage_url(parts[0], parts[1]), code=302)
    return redirect("/console", code=302)


def manage_url(backend: str, bot_id: str) -> str:
    """管理台选中某只的地址；两段各自百分号编码（与页面里 ConsoleUX.manageUrl 同一形状）。"""
    return "/console?b=%s/%s" % (quote(backend, safe=""), quote(bot_id, safe=""))


@console_bp.route("/console/bots/<backend>/<bot_id>/life")
def life_page(backend, bot_id):
    # 旧生活设定地址已并进管理台：合规就 302 并选中这只，不合规 404。
    if not _selection_ok(backend, bot_id):
        abort(404)
    return redirect(manage_url(backend, bot_id), code=302)


# ── 总览 ───────────────────────────────────────────────────────────────────

def _normalize_bot(backend: str, raw) -> dict:
    """一条 bot 只留 INTERFACE §2.3 的已知键；底层多给的键不转给页面。"""
    bot = {"backend": backend}
    src = raw if isinstance(raw, dict) else {}
    for key, default in _BOT_DEFAULTS:
        bot[key] = src.get(key, default)
    bot["bot_id"] = str(bot["bot_id"])
    username = src.get("username")
    bot["person"] = person_of({"bot_id": bot["bot_id"], "person": src.get("person")})
    bot["username"] = username if isinstance(username, str) and username else None
    bot["enabled"] = src.get("enabled") if isinstance(src.get("enabled"), bool) else None
    bot["polling"] = polling_of(src)
    moved = src.get("moved_to")
    bot["moved_to"] = moved if isinstance(moved, str) and moved else None
    return bot


def _past(deadline) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _load_backend(name: str, deadline=None):
    """一种底层的 meta / providers / bots；它的任何失败只落在它自己这一组上。

    返回 (组, bot 列表)。meta、bots 失败整组按不可用；只是 providers 失败时不连累整组：
    供应商为空、原因放进 providers_message，bot 照常取。
    """
    entry = {"backend": name, "label": registry.label(name), "status": "ok", "message": "",
             "capabilities": {}, "effort_levels": [], "providers": [], "default": None,
             "providers_message": ""}
    bots = []
    try:
        ad = registry.adapter(name)
        meta = ad.meta()
        caps = meta.get("capabilities") if isinstance(meta.get("capabilities"), dict) else {}
        entry["capabilities"] = {str(k): v is True for k, v in caps.items()}
        entry["effort_levels"] = [e for e in (meta.get("effort_levels") or []) if isinstance(e, str)]
        if _past(deadline):
            raise AdapterUnavailable("down")
        providers_message = ""
        try:
            provs = ad.providers()
            entry["providers"] = provs.get("providers") if isinstance(provs.get("providers"), list) else []
            entry["default"] = provs.get("default") if isinstance(provs.get("default"), dict) else None
        except (AdapterUnavailable, AdapterError) as e:
            entry.update(providers=[], default=None)
            providers_message = e.message
        except Exception as e:
            _log.error("console 总览：底层 %s 读取供应商清单失败：%s", name, type(e).__name__)
            entry.update(providers=[], default=None)
            providers_message = PROVIDERS_ERROR_TEXT
        if _past(deadline):
            raise AdapterUnavailable("down")
        bots = [_normalize_bot(name, b) for b in ad.bots()]
        entry["providers_message"] = providers_message
    except AdapterUnavailable as e:
        entry.update(status=e.status, message=e.message, capabilities={}, effort_levels=[],
                     providers=[], default=None)
        bots = []
    except AdapterError as e:
        entry.update(status="error", message=e.message, capabilities={}, effort_levels=[],
                     providers=[], default=None)
        bots = []
    except Exception as e:
        _log.error("console 总览：底层 %s 读取失败：%s", name, type(e).__name__)
        entry.update(status="error", message="出错了", capabilities={}, effort_levels=[],
                     providers=[], default=None)
        bots = []
    return entry, bots


def _timed_out_entry(name: str) -> tuple:
    return ({"backend": name, "label": registry.label(name), "status": "down",
             "message": UNAVAILABLE_TEXT["down"], "capabilities": {}, "effort_levels": [],
             "providers": [], "default": None, "providers_message": ""}, [])


def _probe_all(names: list) -> list:
    """各底层并行读取，共用一个总时限：一边慢不拖另一边，超时的那一边按 down 显示。"""
    deadline = time.monotonic() + OVERVIEW_TIMEOUT
    ex = ThreadPoolExecutor(max_workers=max(1, len(names)))
    futures = [ex.submit(_load_backend, name, deadline) for name in names]
    ex.shutdown(wait=False)
    out = []
    for name, fut in zip(names, futures):
        try:
            out.append(fut.result(timeout=max(0.0, deadline - time.monotonic())))
        except FutureTimeout:
            # 结果留在被放弃的 future 里，不会写回已经返回的响应；再挂回调取走异常。
            fut.add_done_callback(lambda f: f.exception())
            out.append(_timed_out_entry(name))
    return out


@console_bp.route("/api/console/overview", methods=["GET"])
def api_overview():
    try:
        results = _probe_all(registry.enabled_backends())
        backends, bots = [], []
        for entry, blist in results:
            backends.append(entry)
            bots.extend(blist)
        for b in bots:
            b["conflict"] = conflict_of(b, [o for o in bots if o.get("backend") != b.get("backend")])
        return jsonify(_scrubbed({"backends": backends, "bots": bots}))
    except Exception as e:
        _log.error("console 总览失败：%s", type(e).__name__)
        return _fail("internal", 500, OVERVIEW_INTERNAL_MESSAGE)
