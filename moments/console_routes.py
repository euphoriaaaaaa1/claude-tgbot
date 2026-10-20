# -*- coding: utf-8 -*-
"""总览页与总览接口（INTERFACE §2.2、§2.3）。

只做两件事：提供 ``/console`` 页面壳，和把各底层的 ``meta()/providers()/bots()`` 汇总成
``GET /api/console/overview`` 的回体。分层硬约束：本文件只许导入 ``adapters`` 包、Flask 与
标准库，不得导入任何底层实现；登录门、跨站校验、供应商模式可达面都在 ``moments/web.py``。

生命周期写接口在 ``moments/lifecycle_routes.py``，两个蓝图互不导入。总览里的文字来自底层，
出口前统一过 ``adapters.guard.scrub``；底层多给的键不转给页面。
"""
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from urllib.parse import quote

from flask import Blueprint, abort, jsonify, redirect, render_template, request

from adapters import AdapterError, AdapterUnavailable, UNAVAILABLE_TEXT, group_config, party_state, registry, valid_bot_id
from adapters.base import (GROUP_INTRO_MAX, GROUP_ITEM_MAX, GROUP_LIST_MAX, GROUP_USERNAME_RE, TTS_MODELS,
                           person_of, polling_of, valid_chat_id, valid_group_chat_id)
from adapters.guard import scrub
from adapters.lifecycle_flow import (INTERNAL_MESSAGE, Reject, call as _call, conflict_of, fail_body,
                                     prepare as _prepare)
from moments.request_body import BodyError, json_object

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
    return render_template("console.html", tts_models=list(TTS_MODELS))


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


# ── 生活设定 / 人设 / 语音（INTERFACE §2.5，T16）──────────────────────────────
# 写法照 CBL：登录门、来源核对在 moments/web.py；能力键不是 true → 501 且不调适配器；
# 底层的错误原样透传。用户写的值与版本号原样回给页面，其余文字照常打码。

MAX_LIFE_BODY = 256 * 1024
MAX_PERSONA_BODY = 1024 * 1024
MAX_VOICE_BODY = 16 * 1024
_LIFE_RAW_KEYS = frozenset({"values", "display_name"})
_PROFILE_PARTS = {
    "persona": ("bot.persona", "人设", "get_persona", "set_persona", MAX_PERSONA_BODY,
                frozenset({"text", "version"})),
    "voice": ("bot.voice", "语音设置", "get_voice", "set_voice", MAX_VOICE_BODY,
              frozenset({"voice_id", "tts_model", "text_language", "voice_language"})),
}


def _scrubbed_except(body: dict, raw_keys) -> dict:
    """同 ``_scrubbed``，但顶层 ``raw_keys`` 里的键原样不动：那些是用户自己写的内容或页面会原样回存的值。"""
    out = _scrubbed({k: v for k, v in body.items() if k not in raw_keys})
    out.update((k, body[k]) for k in raw_keys if k in body)
    return out


def _body(limit: int) -> dict:
    try:
        return json_object(limit)
    except BodyError as e:
        raise Reject(e.code, e.http_status, e.message) from None


def _life_adapter(backend: str, bot_id: str, write: bool = True):
    ad, caps, _levels = _prepare(backend, bot_id, check_busy=write)
    if caps.get("bot.life_settings") is not True:
        raise Reject("unsupported", 501, "这个底层不提供生活设定")
    return ad


def _display_name_of(ad, bot_id: str, result: dict) -> str:
    name = result.get("display_name")
    if isinstance(name, str) and name:
        return name
    try:
        for b in ad.bots():
            if isinstance(b, dict) and str(b.get("bot_id")) == bot_id and isinstance(b.get("display_name"), str):
                return b["display_name"] or bot_id
    except (AdapterError, AdapterUnavailable):
        pass
    except Exception as e:  # noqa: BLE001
        _log.error("console 生活设定：读 %s 的显示名失败：%s", bot_id, type(e).__name__)
    return bot_id


@console_bp.route("/api/console/bots/<backend>/<bot_id>/life", methods=["GET"])
def api_get_life(backend, bot_id):
    try:
        ad = _life_adapter(backend, bot_id, write=False)
        result = _call(lambda: ad.get_life(bot_id))
        result = result if isinstance(result, dict) else {}
        fields = result.get("fields") if isinstance(result.get("fields"), list) else []
        values = result.get("values") if isinstance(result.get("values"), dict) else {}
        return jsonify(_scrubbed_except({"ok": True, "backend": backend, "bot_id": bot_id,
                                         "display_name": _display_name_of(ad, bot_id, result),
                                         "fields": fields, "values": values}, _LIFE_RAW_KEYS))
    except Reject as r:
        return _fail(r.code, r.http_status, r.message, r.fields)
    except Exception as e:  # noqa: BLE001
        _log.error("console 读生活设定失败 %r/%r：%s", backend, bot_id, type(e).__name__)
        return _fail("internal", 500, INTERNAL_MESSAGE)


@console_bp.route("/api/console/bots/<backend>/<bot_id>/life", methods=["PUT"])
def api_set_life(backend, bot_id):
    try:
        body = _body(MAX_LIFE_BODY)
        values = body.get("values")
        if not isinstance(values, dict):
            raise Reject("invalid", 422, "请求体必须带 values 对象")
        ad = _life_adapter(backend, bot_id)
        result = _call(lambda: ad.set_life(bot_id, values))
        out = {"ok": True}
        out.update(result if isinstance(result, dict) else {})
        out["ok"] = True
        return jsonify(_scrubbed_except(out, _LIFE_RAW_KEYS))
    except Reject as r:
        return _fail(r.code, r.http_status, r.message, r.fields)
    except Exception as e:  # noqa: BLE001
        _log.error("console 保存生活设定失败 %r/%r：%s", backend, bot_id, type(e).__name__)
        return _fail("internal", 500, INTERNAL_MESSAGE)


def _profile(part: str, backend: str, bot_id: str, write: bool):
    cap, what, getter, setter, limit, raw_keys = _PROFILE_PARTS[part]
    try:
        payload = _body(limit) if write else None
        ad, caps, _levels = _prepare(backend, bot_id, check_busy=write)
        if caps.get(cap) is not True:
            raise Reject("unsupported", 501, "这个底层不提供%s" % what)
        if write:
            result = _call(lambda: getattr(ad, setter)(bot_id, payload))
        else:
            result = _call(lambda: getattr(ad, getter)(bot_id))
        out = {"ok": True}
        out.update(result if isinstance(result, dict) else {})
        out["ok"] = True
        return jsonify(_scrubbed_except(out, raw_keys))
    except Reject as r:
        return _fail(r.code, r.http_status, r.message, r.fields)
    except Exception as e:  # noqa: BLE001
        _log.error("console %s%s失败 %r/%r：%s", "保存" if write else "读", what, backend, bot_id,
                   type(e).__name__)
        return _fail("internal", 500, INTERNAL_MESSAGE)


@console_bp.route("/api/console/bots/<backend>/<bot_id>/persona", methods=["GET"])
def api_get_persona(backend, bot_id):
    return _profile("persona", backend, bot_id, write=False)


@console_bp.route("/api/console/bots/<backend>/<bot_id>/persona", methods=["PUT"])
def api_set_persona(backend, bot_id):
    return _profile("persona", backend, bot_id, write=True)


@console_bp.route("/api/console/bots/<backend>/<bot_id>/voice", methods=["GET"])
def api_get_voice(backend, bot_id):
    return _profile("voice", backend, bot_id, write=False)


@console_bp.route("/api/console/bots/<backend>/<bot_id>/voice", methods=["PUT"])
def api_set_voice(backend, bot_id):
    return _profile("voice", backend, bot_id, write=True)


# ── 群聊、导演、聚会（INTERFACE §2.13，T16）────────────────────────────────────
# 群清单在 adapters.group_config，聚会状态在 adapters.party_state；成员身份经各底层适配器的
# groups / set_groups 读写。进退群串行执行，避免两次交错互相覆盖。

MAX_GROUP_ADD_BODY = 4 * 1024
MAX_DIRECTOR_BODY = 1024
MAX_PARTY_BODY = 4 * 1024
MAX_MEMBER_BODY = 4 * 1024
_GROUP_ADD_KEYS = frozenset({"chat_id", "allowFrom"})
_DIRECTOR_KEYS = frozenset({"director"})
_PARTY_KEYS = frozenset({"on", "exclude", "note"})
_GROUP_BODY_KEYS = frozenset({"backend", "bot_id", "join", "intro", "username"})
_GROUP_OP_LOCK = threading.Lock()
BUSY_MESSAGE = "别的操作正在改群清单，请稍后再试"
INVALID_FIELDS_MESSAGE = "入参不合规"
PARTY_INVALID_FIELDS = "请求体有不合规的字段"
FEATURE_OFF_MESSAGE = "群聊功能关着，先在群聊块打开它"


def _group_config_reject(e: group_config.GroupConfigError):
    if e.code == "busy":
        return Reject("busy", 409, group_config.BUSY_MESSAGE)
    if e.code == "lock_failed":
        return Reject("lock_failed", 500, e.message)
    if e.code == "unreadable":
        return Reject("internal", 500, group_config.UNREADABLE_MESSAGE)
    if e.code == "write":
        return Reject("internal", 500, group_config.NOT_WRITTEN_MESSAGE)
    return Reject("invalid", 422, e.message)


def _allow_from_ok(value) -> bool:
    return (isinstance(value, list) and 1 <= len(value) <= GROUP_LIST_MAX
            and all(isinstance(item, str) and 1 <= len(item) <= GROUP_ITEM_MAX for item in value))


def _group_list() -> list:
    try:
        return group_config.load() or []
    except group_config.GroupConfigError as e:
        raise Reject("internal", 500, str(e))


def _group_bots(strict: bool = False) -> list:
    """能力 bot.group_chat 为 true 的每种底层的全部 bot 与各自的群设置。"""
    out = []
    for name in registry.enabled_backends():
        try:
            ad = registry.adapter(name)
            caps = ad.meta().get("capabilities")
        except (AdapterUnavailable, AdapterError):
            if strict:
                raise Reject("backend_down", 502, "读不出%s的群设置" % registry.label(name))
            continue
        if not isinstance(caps, dict) or caps.get("bot.group_chat") is not True:
            continue
        try:
            bots = [b for b in ad.bots() if isinstance(b, dict) and valid_bot_id(str(b.get("bot_id") or ""))]
        except (AdapterUnavailable, AdapterError):
            if strict:
                raise Reject("backend_down", 502, "读不出%s的 bot 名单" % registry.label(name))
            continue
        for b in bots:
            rec = {"backend": name, "bot_id": str(b["bot_id"]),
                   "display_name": str(b.get("display_name") or b["bot_id"]),
                   "groups": {}, "error": None}
            try:
                got = ad.groups(rec["bot_id"])
                rec["groups"] = got.get("groups") if isinstance(got, dict) and isinstance(got.get("groups"), dict) else {}
            except (AdapterUnavailable, AdapterError) as e:
                if strict:
                    raise Reject("backend_down", 502, "读不出 %s 的群设置：%s" % (rec["display_name"], e.message))
                rec["error"] = e.message
            out.append(rec)
    return out


def _member_ref(r) -> dict:
    return {"backend": r["backend"], "bot_id": r["bot_id"], "display_name": r["display_name"]}


def _default_allow_from() -> list:
    """群清单新增时缺省 allowFrom：取各底层 bot 条目里给的私聊编号（正整数）。"""
    ids = []
    for name in registry.enabled_backends():
        try:
            for b in registry.adapter(name).bots():
                if isinstance(b, dict):
                    cid = b.get("chat_id")
                    if isinstance(cid, int) and not isinstance(cid, bool):
                        cid = str(cid)
                    if isinstance(cid, str) and cid.isdigit() and cid:
                        ids.append(cid)
        except (AdapterUnavailable, AdapterError):
            continue
    return sorted(set(ids))


@console_bp.route("/api/console/bots/<backend>/<bot_id>/groups/discovered", methods=["GET"])
def api_groups_discovered(backend, bot_id):
    """INTERFACE §2.6：刚发现的群（只读）；能力键不是 true → 501，不调适配器。"""
    try:
        ad, caps, _levels = _prepare(backend, bot_id, check_duplicate=False, check_busy=False,
                                     check_conflict=False)
        if caps.get("bot.groups_discovered") is not True:
            raise Reject("unsupported", 501, "这个底层不提供群发现")
        result = _call(lambda: ad.discovered_groups(bot_id, include_joined=request.args.get("include_joined") == "true"))
        out = {"ok": True}
        out.update(result if isinstance(result, dict) else {})
        return jsonify(_scrubbed(out))
    except Reject as r:
        return _fail(r.code, r.http_status, r.message, r.fields)
    except Exception as e:  # noqa: BLE001
        _log.error("console 读群发现失败 %r/%r：%s", backend, bot_id, type(e).__name__)
        return _fail("internal", 500, INTERNAL_MESSAGE)


@console_bp.route("/api/console/groups", methods=["GET"])
def api_groups():
    try:
        groups = _group_list()
        recs = _group_bots(strict=False) if groups else []
        return jsonify(_scrubbed({
            "groups": [{"chat_id": g["chat_id"],
                        "director": "on" if group_config.director_on(g["chat_id"]) else "off",
                        "party": party_state.public_state(g["chat_id"]),
                        "members": [_member_ref(r) for r in recs if g["chat_id"] in r["groups"]]}
                       for g in groups],
            "unreadable": [dict(_member_ref(r), message=r["error"]) for r in recs if r["error"]],
            "default_allow_from": _default_allow_from(),
            "warnings": [],
        }))
    except Reject as r:
        return _fail(r.code, r.http_status, r.message, r.fields)
    except Exception as e:  # noqa: BLE001
        _log.error("console 读群成员失败：%s", type(e).__name__)
        return _fail("internal", 500, INTERNAL_MESSAGE)


@console_bp.route("/api/console/groups", methods=["POST"])
def api_add_group():
    try:
        body = _body(MAX_GROUP_ADD_BODY)
        if set(body) - _GROUP_ADD_KEYS:
            raise Reject("invalid", 422, "只许 chat_id 与 allowFrom")
        chat_id = valid_group_chat_id(body.get("chat_id"))
        if chat_id is None:
            raise Reject("invalid", 422, INVALID_FIELDS_MESSAGE, {"chat_id": "群号格式不对"})
        if "allowFrom" in body:
            allow = body["allowFrom"]
        else:
            allow = _default_allow_from()
        if not _allow_from_ok(allow):
            raise Reject("invalid", 422, INVALID_FIELDS_MESSAGE, {"allowFrom": "至少一个人；每项是 1 到 64 个字符的字符串"})
        try:
            result = group_config.add_group(chat_id, allow)
        except group_config.GroupConfigError as e:
            raise _group_config_reject(e)
        group = dict(result["group"])
        group["director"] = "on" if not result["created"] and group_config.director_on(chat_id) else "off"
        return jsonify({"ok": True, "created": result["created"], "group": group})
    except Reject as r:
        return _fail(r.code, r.http_status, r.message, r.fields)
    except Exception as e:  # noqa: BLE001
        _log.error("console 加群失败：%s", type(e).__name__)
        return _fail("internal", 500, INTERNAL_MESSAGE)


def _set_director(chat_id: str, value: str) -> None:
    target = group_config.director_path(chat_id)
    try:
        if value == "on":
            os.makedirs(group_config.director_dir(), mode=0o700, exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.close(fd)
        else:
            try:
                os.unlink(target)
            except FileNotFoundError:
                pass
    except OSError:
        _log.error("console 改导演开关失败 %r：文件写不进去", chat_id)
        raise Reject("internal", 500, "导演开关没有改成（文件状态不变）")


@console_bp.route("/api/console/groups/<chat_id>/director", methods=["PUT"])
def api_group_director(chat_id):
    try:
        body = _body(MAX_DIRECTOR_BODY)
        if set(body) - _DIRECTOR_KEYS:
            raise Reject("invalid", 422, "只许 director 一个键")
        value = body.get("director")
        if value not in ("on", "off"):
            raise Reject("invalid", 422, INVALID_FIELDS_MESSAGE, {"director": "只能是 on 或 off"})
        chat = valid_group_chat_id(chat_id)
        if chat is None:
            raise Reject("invalid", 422, INVALID_FIELDS_MESSAGE, {"chat_id": "群号格式不对"})
        if group_config.find(_group_list(), chat) is None:
            raise Reject("unknown_group", 404, "群清单里没有这个群")
        try:
            with group_config.lock():
                _set_director(chat, value)
        except group_config.GroupConfigError as e:
            raise _group_config_reject(e)
        return jsonify({"ok": True, "chat_id": chat, "director": value})
    except Reject as r:
        return _fail(r.code, r.http_status, r.message, r.fields)
    except Exception as e:  # noqa: BLE001
        _log.error("console 改导演开关失败 %r：%s", chat_id, type(e).__name__)
        return _fail("internal", 500, INTERNAL_MESSAGE)


@console_bp.route("/api/console/groups/<path:chat_id>/party", methods=["POST"])
def api_group_party(chat_id):
    try:
        body = _body(MAX_PARTY_BODY)
        fields = {}
        for key in set(body) - _PARTY_KEYS:
            fields[str(key)] = "只允许 on、exclude、note"
        on = body.get("on")
        if not isinstance(on, bool):
            fields["on"] = "on 必须是布尔"
        exclude = None
        if "exclude" in body:
            exclude = party_state.normalize_exclude(body["exclude"])
            if exclude is None:
                fields["exclude"] = "exclude 必须是 bot 编号列表，最多 20 个"
        note = None
        if "note" in body:
            note = party_state.normalize_note(body["note"])
            if note is None:
                fields["note"] = "note 最多 80 个字，不能换行"
        if fields:
            raise Reject("invalid", 422, PARTY_INVALID_FIELDS, fields)
        chat = valid_group_chat_id(chat_id)
        if chat is None:
            raise Reject("invalid", 422, PARTY_INVALID_FIELDS, {"chat_id": "群号格式不对"})
        if on:
            if group_config.find(_group_list(), chat) is None:
                raise Reject("unknown_group", 404, "群清单里没有这个群")
            try:
                party_state.open_party(chat, "console", exclude=exclude, note=note)
            except party_state.StateWriteError:
                raise Reject("internal", 500, "聚会模式没写成（状态文件不变）")
            return jsonify({"ok": True, "chat_id": chat, "party": party_state.public_state(chat)})
        try:
            party_state.close_party(chat)
        except party_state.StateWriteError:
            raise Reject("internal", 500, "聚会模式没写成（状态文件不变）")
        return jsonify({"ok": True, "chat_id": chat, "party": {"on": False}})
    except Reject as r:
        return _fail(r.code, r.http_status, r.message, r.fields)
    except Exception as e:  # noqa: BLE001
        _log.error("console 改聚会开关失败 %r：%s", chat_id, type(e).__name__)
        return _fail("internal", 500, INTERNAL_MESSAGE)


def _check_member_body(chat_id: str, body: dict) -> dict:
    extra = set(body) - _GROUP_BODY_KEYS
    if extra:
        raise Reject("invalid", 422, "请求体里有不认识的键：%s" % "、".join(sorted(map(str, extra))))
    if not valid_chat_id(chat_id):
        raise Reject("invalid", 422, "群号不合规：应为 - 后跟 1 到 20 位数字")
    if not isinstance(body.get("backend"), str) or not body["backend"]:
        raise Reject("invalid", 422, "backend 必须是非空字符串")
    if not isinstance(body.get("bot_id"), str):
        raise Reject("invalid", 422, "bot_id 必须是字符串")
    if not isinstance(body.get("join"), bool):
        raise Reject("invalid", 422, "join 必须是布尔")
    intro, username = body.get("intro"), body.get("username")
    if intro is not None and (not isinstance(intro, str) or len(intro) > GROUP_INTRO_MAX):
        raise Reject("invalid", 422, "介绍必须是字符串，最多 %d 个字" % GROUP_INTRO_MAX)
    if username not in (None, "") and not (isinstance(username, str) and GROUP_USERNAME_RE.fullmatch(username)):
        raise Reject("invalid", 422, "Telegram 用户名应为 5 到 32 位字母、数字或下划线，不带 @")
    return body


@console_bp.route("/api/console/group/<chat_id>/members", methods=["POST"])
def api_group_members(chat_id):
    try:
        body = _check_member_body(chat_id, _body(MAX_MEMBER_BODY))
        ad, caps, _levels = _prepare(body["backend"], body["bot_id"], check_duplicate=False,
                                     check_busy=True, check_conflict=False)
        if caps.get("bot.group_chat") is not True:
            raise Reject("unsupported", 501, "这个底层不提供进群、退群")
        if body["join"] and caps.get("bot.features") is True:
            enabled = True
            checker = getattr(ad, "group_chat_enabled", None)
            try:
                if checker is not None:
                    enabled = checker(body["bot_id"]) is True
                else:
                    got = _call(lambda: ad.get_features(body["bot_id"]))
                    feats = got.get("features") if isinstance(got, dict) else None
                    enabled = not (isinstance(feats, dict) and feats.get("groups") is False)
            except (AdapterError, AdapterUnavailable):
                enabled = True
            if not enabled:
                raise Reject("feature_off", 409, FEATURE_OFF_MESSAGE)
        if group_config.find(_group_list(), chat_id) is None:
            raise Reject("unknown_group", 404, "群清单里没有这个群")
        if not _GROUP_OP_LOCK.acquire(blocking=False):
            raise Reject("busy", 409, "另一个进群、退群操作正在进行，请稍后再试")
        try:
            policy = {}
            if body["join"] and isinstance(body.get("intro"), str) and body["intro"]:
                policy["intro"] = body["intro"]
            result = _call(lambda: ad.set_groups(body["bot_id"], {
                "chat_id": chat_id, "join": body["join"], "policy": policy}))
            warnings = []
            if body["join"] and body.get("username"):
                setter = getattr(ad, "set_username", None)
                if setter is not None:
                    try:
                        setter(body["bot_id"], body["username"])
                    except (AdapterError, AdapterUnavailable) as e:
                        warnings.append("用户名没写进去：%s" % e.message)
            out = {"ok": True, "chat_id": chat_id, "backend": body["backend"], "bot_id": body["bot_id"]}
            out.update(result if isinstance(result, dict) else {})
            if warnings:
                out["warnings"] = warnings
            return jsonify(_scrubbed(out))
        finally:
            _GROUP_OP_LOCK.release()
    except Reject as r:
        return _fail(r.code, r.http_status, r.message, r.fields)
    except Exception as e:  # noqa: BLE001
        _log.error("console 进退群失败 %r：%s", chat_id, type(e).__name__)
        return _fail("internal", 500, INTERNAL_MESSAGE)
