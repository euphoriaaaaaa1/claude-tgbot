# -*- coding: utf-8 -*-
"""生命周期编排层（INTERFACE §2.4、§4.8、§5、§9）。

两条适配器（claude / dsh）与壳之间只经本模块与 ``adapters.ops``：
受理请求时只做不发网络请求就能查的校验（路径参数、能力键、请求体字段、生命周期锁）；
bot 在不在、另一边状态都放在后台步骤里。写接口同步返回 ``202``，进度查 ops。

与 CBL 版本的差异（公开版按 INTERFACE 收敛）：
- 不再包含群跟随、朋友圈清理与 CBL 专用群清单逻辑；归档/恢复/删除由适配器自己完成多步操作，
  引擎只负责停用、提交点、复核与回退。
- 不写任务记录之外的快照文件；归档的 shell_meta 只含 op_id / was_enabled / username。
- 对外 op 形状按 INTERFACE §2.4（``state`` ∈ running / done / failed / needs_attention）。

不依赖 Flask；令牌只经 ``SecretToken`` 在内存里传递。
"""
import logging
import os

from . import claude_files, ops, registry
from .base import (AdapterError, AdapterUnavailable, BAD_ARCHIVE_ID, BAD_SHELL_META, BOT_ID_MESSAGE,
                   CONFIRM_RULE, ENABLED_TYPE_MESSAGE, PERSONA_BOTH_MESSAGE, PERSONA_TEMPLATE_MISSING,
                   PERSONA_TEXT_MESSAGE, PROFILE_SAVED, PROFILE_UNCHANGED, TTS_MODELS, UNAVAILABLE_TEXT,
                   VOICE_ID_RE, VOICE_KEYS, chat_id_error, display_name_error, language_error,
                   new_bot_id_error, person_of, polling_of, same_bot, shell_meta_ok, valid_archive_id,
                   valid_bot_id)
from .guard import TOKEN_FORMAT_MESSAGE, TOKEN_RE, SecretToken, scrub

_log = logging.getLogger(__name__)

INTERNAL_MESSAGE = "服务器内部错误，请稍后再试"
UNSUPPORTED_MESSAGE = "这个底层不提供这项操作"
BAD_ID_LOG = "编号不合规"
UNAVAILABLE_HTTP = {
    "down": (502, "backend_down"),
    "error": (502, "backend_down"),
    "unconfigured": (503, "backend_unconfigured"),
    "unauthorized": (503, "backend_unconfigured"),
    "not_installed": (404, "unknown_backend"),
}


class Reject(Exception):
    """壳拒绝这次请求；蓝图把它翻成 ``fail_body`` 与 ``http_status``。"""

    def __init__(self, code: str, http_status: int, message: str, fields=None):
        super().__init__(message)
        self.code, self.http_status, self.message = code, http_status, message
        self.fields = fields if isinstance(fields, dict) else {}


def fail_body(code: str, message: str, fields=None) -> dict:
    """同步失败的形状（INTERFACE §0.4 第一族）；每段文字先打码。"""
    body = {"ok": False, "code": code, "message": scrub(message)}
    if fields:
        body["fields"] = {k: scrub(v) for k, v in fields.items()}
    return body


def log_id(value) -> str:
    return value if valid_bot_id(value) else BAD_ID_LOG


def unavailable_to_reject(e: AdapterUnavailable) -> Reject:
    status, code = UNAVAILABLE_HTTP.get(e.status, (502, "backend_down"))
    return Reject(code, status, e.message)


def call(fn):
    """调一次适配器：单次被拒（AdapterError）原样透传码与状态，整侧不可用按表翻。"""
    try:
        return fn()
    except AdapterError as e:
        raise Reject(e.code, e.http_status, e.message, getattr(e, "fields", None))
    except AdapterUnavailable as e:
        raise unavailable_to_reject(e)


CONFLICT_MESSAGE = "这只 bot 在两边同时在收消息，现在只能停用其中一边"


def conflict_of(bot, others) -> bool:
    if polling_of(bot) is not True:
        return False
    return any(isinstance(o, dict) and same_bot(bot, o) and polling_of(o) for o in (others or ()))


def _conflict_on_other_side(backend: str, me: dict) -> bool:
    if not polling_of(me):
        return False
    for name in registry.enabled_backends():
        if name == backend:
            continue
        try:
            others = registry.adapter(name).bots()
        except (AdapterUnavailable, AdapterError):
            continue
        except Exception as e:
            _log.warning("console 两边冲突核对：底层 %r 读取失败：%s", log_id(name), type(e).__name__)
            continue
        if conflict_of(me, others):
            return True
    return False


def prepare(backend: str, bot_id: str, check_duplicate: bool = True, *, check_busy: bool = True,
            check_conflict: bool = True):
    """公共前置：bot_id 合规 → 底层在名单 → 底层可用 → 生命周期操作中 → 两边同时在收消息。"""
    if not valid_bot_id(bot_id):
        raise Reject("invalid", 422, BOT_ID_MESSAGE)
    ad, caps, levels = _adapter_caps(backend)
    if check_busy:
        me = None
        try:
            me = next((b for b in ad.bots() if isinstance(b, dict) and str(b.get("bot_id")) == bot_id), None)
        except (AdapterUnavailable, AdapterError):
            me = None
        person = person_of(me) if me is not None else bot_id
        if ops.any_person_busy() and ops.person_busy(person):
            raise Reject("busy", 409, ops.PERSON_BUSY_MESSAGE)
        if me is not None and check_conflict and _conflict_on_other_side(backend, me):
            raise Reject("conflict", 409, CONFLICT_MESSAGE)
    return ad, caps, levels


def _adapter_caps(backend: str):
    if backend not in registry.enabled_backends():
        raise Reject("unknown_backend", 404, "没有这种底层")
    try:
        ad = registry.adapter(backend)
        meta = ad.meta()
    except AdapterUnavailable as e:
        raise unavailable_to_reject(e)
    except AdapterError as e:
        raise Reject("backend_down", 502, e.message)
    meta = meta if isinstance(meta, dict) else {}
    caps = meta.get("capabilities") if isinstance(meta.get("capabilities"), dict) else {}
    levels = [e for e in (meta.get("effort_levels") or []) if isinstance(e, str)]
    return ad, caps, levels


# ── 显示名（INTERFACE §2.4）：按当前底层读写 ─────────────────────────────────

def _profile_adapter(backend: str, bot_id: str, write: bool = True):
    ad, caps, _levels = prepare(backend, bot_id, check_duplicate=False, check_busy=write)
    if caps.get("bot.profile") is not True:
        raise Reject("unsupported", 501, UNSUPPORTED_MESSAGE)
    return ad, caps


def get_display_name(backend: str, bot_id: str) -> str:
    ad, _caps = _profile_adapter(backend, bot_id, write=False)
    result = call(lambda: ad.get_profile(bot_id))
    name = result.get("display_name") if isinstance(result, dict) else None
    return name if isinstance(name, str) else bot_id


def save_display_name(backend: str, bot_id: str, value: str) -> dict:
    """改显示名；值与当前值一致时不调写入。返回 ``display_name`` / ``message``。"""
    ad, _caps = _profile_adapter(backend, bot_id)
    current = call(lambda: ad.get_profile(bot_id))
    current = current.get("display_name") if isinstance(current, dict) else None
    if isinstance(current, str) and current.strip() == value:
        return {"display_name": current, "message": PROFILE_UNCHANGED}
    result = call(lambda: ad.set_profile(bot_id, value))
    result = result if isinstance(result, dict) else {}
    name = result.get("display_name") if isinstance(result.get("display_name"), str) else value
    return {"display_name": name, "message": PROFILE_SAVED}


# ── 启停（INTERFACE §2.4）：同步接口，不建任务记录，但要拿生命周期锁 ────────────────
OTHER_SIDE_RUNNING = "另一边的同一只 bot（%s/%s）还在收消息，先停那边"
OTHER_SIDE_UNKNOWN = "%s 现在连不上，没法确认那边没有同一只 bot 在跑；确认没有的话再点一次"
OTHER_SIDE_UNKNOWN_KEPT = "%s 现在连不上，没法确认那边没有同一只 bot 在跑。确认没有的话，可以在页面上手动启用"
UNKNOWN_BOT_MESSAGE = "这个底层没有这只 bot"
SWITCH_MOVED = "这只 bot 已切到 %s，要用这边请点“切换底层”"
SWITCH_MOVED_PENDING = "这只 bot 正在切换底层，要用这边请点“切换底层”"


def check_enabled_value(value) -> bool:
    if not isinstance(value, bool):
        raise Reject("invalid", 422, ENABLED_TYPE_MESSAGE)
    return value


def set_enabled(backend: str, bot_id: str, enabled: bool, confirm_other_side_unknown: bool = False) -> dict:
    """校验 → 生命周期锁（409 busy）→ 启用时做另一边检查 → 调适配器。返回 enabled / running / message。"""
    enabled = check_enabled_value(enabled)
    ad, caps, _levels = prepare(backend, bot_id, check_duplicate=False, check_busy=False)
    if caps.get("bot.enable") is not True:
        raise Reject("unsupported", 501, UNSUPPORTED_MESSAGE)
    me = next((b for b in call(ad.bots) if isinstance(b, dict) and str(b.get("bot_id")) == bot_id), None)
    if me is None:
        raise Reject("unknown_bot", 404, UNKNOWN_BOT_MESSAGE)
    try:
        lock = ops.acquire(ops.new_op_id("enable" if enabled else "disable"), persons=(person_of(me),))
    except ops.LifecycleBusy as e:
        raise Reject("busy", 409, e.message)
    with lock:
        if enabled:
            moved = me.get("moved_to")
            if isinstance(moved, str) and moved:
                raise Reject("moved", 409, SWITCH_MOVED_PENDING if moved == "pending"
                             else SWITCH_MOVED % moved)
            _check_other_side(backend, me, confirm_other_side_unknown is True)
        try:
            result = call(lambda: ad.set_enabled(bot_id, enabled))
        except NotImplementedError:
            raise Reject("unsupported", 501, UNSUPPORTED_MESSAGE)
    result = result if isinstance(result, dict) else {}
    return {"enabled": result.get("enabled") is True, "running": result.get("running") is True,
            "message": scrub(result.get("message") or "")}


def _check_other_side(backend: str, me: dict, confirmed: bool, *, rollback: bool = False) -> None:
    unknown = []
    for name in registry.enabled_backends():
        if name == backend:
            continue
        try:
            others = registry.adapter(name).bots()
        except (AdapterUnavailable, AdapterError):
            unknown.append(name)
            continue
        except Exception as e:
            _log.warning("console 启用前读 %s 的名单失败：%s", name, type(e).__name__)
            unknown.append(name)
            continue
        for b in others if isinstance(others, list) else []:
            if isinstance(b, dict) and same_bot(me, b) and polling_of(b):
                raise Reject("other_side_running", 409, OTHER_SIDE_RUNNING % (name, b.get("bot_id")))
    if unknown and not confirmed:
        text = OTHER_SIDE_UNKNOWN_KEPT if rollback else OTHER_SIDE_UNKNOWN
        raise Reject("other_side_unknown", 409, text % "、".join(registry.label(n) for n in unknown))


# ── 归档、恢复、删除（INTERFACE §2.4、§5.2）：受理只做本地校验，其余在后台步骤 ─────────────
ARCHIVE_DONE = "已归档，可以在“已归档”里恢复"
DELETE_DONE = "已删除本地数据。Telegram 上的 bot 照样能用，以后可以用新的人设和对应的 bot 令牌再加回来"
RESTORE_DONE = "已恢复，保持停用"
ALREADY_STOPPED = "本来就是停用的"
STARTED_BACK = "已按原状态启回"
STAYED_STOPPED = "归档前就是停用的，保持停用"
KEPT_PREFIX = "没有自动改回启用："
NO_ARCHIVE = "没有这份归档"
UNDO_COMMITTED = "这一步已经做成，撤不回；请点‘重新收拾’"
WIPE_PENDING = "已移进废纸篓，但令牌没抹干净；点‘重新收拾’会再抹一次"
REFRESHED = "已刷新"


def _lifecycle_caps(backend: str, cap: str):
    ad, caps, _levels = _adapter_caps(backend)
    if caps.get(cap) is not True:
        raise Reject("unsupported", 501, UNSUPPORTED_MESSAGE)
    return ad


def _submit(kind: str, backend: str, *, bot_id=None, archive_id=None, target=None, token=None,
            data=None, payload=None, rate_key=None) -> str:
    try:
        lock = ops.acquire(ops.new_op_id(kind), persons=(bot_id,) if bot_id else ())
    except ops.LifecycleBusy as e:
        raise Reject("busy", 409, e.message)
    try:
        return ops.submit(lock, kind, backend=backend, bot_id=bot_id, person=bot_id, archive_id=archive_id,
                          target=target, token=token, data=data, payload=payload, rate_key=rate_key)
    except BaseException:
        lock.release()
        raise


def start_archive(backend: str, bot_id: str) -> str:
    prepare_ids(backend, bot_id)
    _lifecycle_caps(backend, "bot.archive")
    return _submit("archive", backend, bot_id=bot_id)


def start_delete(backend: str, bot_id: str, confirm) -> str:
    """confirm 必须与路径里的编号逐字相同，否则 422 且什么都不做。"""
    prepare_ids(backend, bot_id)
    _lifecycle_caps(backend, "bot.delete")
    if not isinstance(confirm, str) or confirm != bot_id:
        raise Reject("invalid", 422, CONFIRM_RULE, {"confirm": CONFIRM_RULE})
    return _submit("delete", backend, bot_id=bot_id)


def start_restore(backend: str, archive_id: str) -> str:
    if backend not in registry.enabled_backends():
        raise Reject("unknown_backend", 404, "没有这种底层")
    if not valid_archive_id(archive_id):
        raise Reject("invalid", 422, BAD_ARCHIVE_ID)
    _lifecycle_caps(backend, "bot.archive")
    return _submit("restore", backend, archive_id=archive_id)


def prepare_ids(backend: str, bot_id: str) -> None:
    if not valid_bot_id(bot_id):
        raise Reject("invalid", 422, BOT_ID_MESSAGE)
    if backend not in registry.enabled_backends():
        raise Reject("unknown_backend", 404, "没有这种底层")


def list_archives() -> dict:
    """``{"archives": [...], "backends": [...]}``；一边读不出不影响另一边。"""
    rows, backends = [], []
    for name in registry.enabled_backends():
        status, message = "ok", ""
        try:
            ad = registry.adapter(name)
            caps = ad.meta().get("capabilities")
            if not isinstance(caps, dict) or caps.get("bot.archive") is not True:
                status, message = "unsupported", UNSUPPORTED_MESSAGE
            else:
                items = ad.list_archives()
                rows += [_archive_row(name, a) for a in items if isinstance(a, dict)]
        except AdapterUnavailable as e:
            status, message = e.status, e.message
        except AdapterError as e:
            status, message = "error", e.message
        except NotImplementedError:
            status, message = "unsupported", UNSUPPORTED_MESSAGE
        except Exception as e:
            _log.error("console 读 %r 的归档清单失败：%s", log_id(name), type(e).__name__)
            status, message = "error", UNAVAILABLE_TEXT["error"]
        backends.append({"backend": name, "status": status, "message": scrub(message)})
    rows.sort(key=lambda r: (r["archived_at"] or "", r["archive_id"] or ""), reverse=True)
    return {"archives": rows, "backends": backends}


def _text_or_none(value):
    return scrub(value) if isinstance(value, str) else None


def _archive_row(backend: str, a: dict) -> dict:
    meta = a.get("shell_meta") if isinstance(a.get("shell_meta"), dict) else {}
    was = meta.get("was_enabled")
    status = a.get("status") if a.get("status") in ("complete", "incomplete") else None
    return {"backend": backend, "archive_id": _text_or_none(a.get("archive_id")),
            "bot_id": _text_or_none(a.get("bot_id")), "display_name": _text_or_none(a.get("display_name")),
            "person": _text_or_none(a.get("person")), "archived_at": _text_or_none(a.get("archived_at")),
            "was_enabled": was if isinstance(was, bool) else None, "status": status}


# ── 步骤共用的小工具 ─────────────────────────────────────────────────────────

def _adapter_of(task):
    return call(lambda: registry.adapter(task.backend))


def _find_bot(ad, bot_id):
    bots = call(ad.bots)
    return next((b for b in bots if isinstance(b, dict) and str(b.get("bot_id")) == bot_id), None) \
        if isinstance(bots, list) else None


def _enabled_of(bot: dict) -> bool:
    enabled = bot.get("enabled")
    return enabled if isinstance(enabled, bool) else bot.get("state") != "stopped"


def _me(task) -> dict:
    return {"bot_id": task.bot_id, "person": task.person or task.bot_id, "username": task.data.get("username")}


def _write(fn):
    try:
        return ops.write_call(fn)
    except AdapterUnavailable as e:
        if e.status == "error":
            raise ops.ResultUnknown(ops.UNKNOWN_MESSAGE % e.message, code="backend_down", http_status=502,
                                    reason=e.message) from None
        raise unavailable_to_reject(e) from None


def _write_lifecycle(fn):
    """多步失败（LifecycleError，带 state）→ StepFailed / StepStuck；单次拒绝与结果未知原样交引擎。"""
    try:
        return _write(fn)
    except Exception as e:
        state = getattr(e, "state", None)
        code, status = getattr(e, "code", None), getattr(e, "http_status", None)
        if state is None or not isinstance(code, str) or not isinstance(status, int):
            raise
        message = scrub(getattr(e, "message", None) or str(e))
        if state in ("unchanged", "rolled_back"):
            raise ops.StepFailed(code, status, message, rolled_back=state == "rolled_back") from None
        raise ops.StepStuck(message, code=code, http_status=status) from None


def _note_substeps(task, steps) -> None:
    for s in steps if isinstance(steps, list) else ():
        if isinstance(s, dict) and s.get("status") == "failed":
            message = s.get("message") if isinstance(s.get("message"), str) else ""
            if message:
                task.warn(message)


def _resolved(ad, task):
    try:
        out = ad.resolve(task.rec)
    except NotImplementedError:
        return None
    return out if isinstance(out, dict) else {"state": "needs_attention"}


def _start_back(task):
    """按原状态启回；检查不过或启动没成功都保持停用（安全状态）。"""
    try:
        _check_other_side(task.backend, _me(task), False, rollback=True)
        ad = _adapter_of(task)
        ad.set_enabled(task.bot_id, True, op_id=task.op_id)
    except (Reject, AdapterError, AdapterUnavailable) as e:
        return KEPT_PREFIX + scrub(e.message)
    except Exception as e:
        _log.error("操作 %s 启回 %r 出错：%s", task.op_id, log_id(task.bot_id), type(e).__name__)
        return KEPT_PREFIX + UNAVAILABLE_TEXT["error"]
    return None


# ── 各步 ───────────────────────────────────────────────────────────────────

def _stop_run(task):
    ad = _adapter_of(task)
    me = _find_bot(ad, task.bot_id)
    if me is None:
        raise ops.StepFailed("unknown_bot", 404, UNKNOWN_BOT_MESSAGE)
    enabled = _enabled_of(me)
    task.claim(person=person_of(me))
    username = me.get("username")
    task.data.update(was_enabled=enabled, username=username if isinstance(username, str) and username else None)
    task.save()
    if not enabled and not polling_of(me):
        raise ops.StepSkipped(ALREADY_STOPPED)
    try:
        out = _write(lambda: ad.set_enabled(task.bot_id, False, op_id=task.op_id))
    except AdapterError as e:
        note = _start_back(task) if enabled else None
        raise ops.StepFailed(e.code, e.http_status, scrub(e.message), rolled_back=enabled, note=note)
    return scrub((out or {}).get("message") or "已停用")


def _stop_check(task):
    me = _find_bot(registry.adapter(task.backend), task.bot_id)
    return True if me is not None and not polling_of(me) else None


def _stop_undo(task):
    if not task.data.get("was_enabled"):
        return STAYED_STOPPED
    note = _start_back(task)
    return ops.Kept(note) if note else STARTED_BACK


def _archive_shell_meta(task) -> dict:
    username = task.data.get("username")
    return {"op_id": task.op_id, "was_enabled": task.data.get("was_enabled") is True,
            "username": username if isinstance(username, str) and username else None}


def _archive_run(task):
    ad = _adapter_of(task)
    meta = _archive_shell_meta(task)
    if not shell_meta_ok(meta):
        raise ops.StepFailed("invalid", 422, BAD_SHELL_META)
    out = _write_lifecycle(lambda: ad.archive(task.bot_id, meta, op_id=task.op_id)) or {}
    steps = out.get("steps") if isinstance(out.get("steps"), list) else []
    task.result.update(archive_id=out.get("archive_id"), message=ARCHIVE_DONE, steps=steps)
    _note_substeps(task, steps)
    return "已移到归档区（%s）" % out.get("archive_id")


def _archive_check(task):
    ad = registry.adapter(task.backend)
    out = _resolved(ad, task)
    if out is not None:
        if out.get("state") == "committed":
            task.result.update(archive_id=out.get("archive_id"), message=ARCHIVE_DONE)
            return True
        return False if out.get("state") in ("rolled_back", "unchanged") else None
    for a in ad.list_archives():
        meta = a.get("shell_meta") if isinstance(a, dict) else None
        if isinstance(meta, dict) and meta.get("op_id") == task.op_id and isinstance(a.get("archive_id"), str):
            task.result.update(archive_id=a["archive_id"], message=ARCHIVE_DONE)
            return True
    return False if _find_bot(ad, task.bot_id) is not None else None


def _commit_undo(task):
    ad = _adapter_of(task)
    out = _resolved(ad, task)
    if out is None:
        verdict = _CHECKS[task.kind](task)
        if verdict is False:
            return "底层没有做成，不用撤"
        raise ops.StepFailed("step_failed", 500, UNDO_COMMITTED)
    if out.get("state") in ("rolled_back", "unchanged"):
        return scrub(out.get("message") or "已撤回")
    raise ops.StepFailed("step_failed", 500, scrub(out.get("message") or UNDO_COMMITTED))


def _refresh_run(task):
    try:
        import bots_registry
        bots_registry.reload()
    except Exception as e:
        raise ops.StepFailed("step_failed", 500, "端口表没刷新（%s）" % type(e).__name__) from None
    return REFRESHED


# ── 恢复与删除 ──────────────────────────────────────────────────────────────

def _restore_run(task):
    """提交点：先从归档清单找到这份（没有 → 404 unknown_archive），再调适配器恢复（停用态）。"""
    ad = _adapter_of(task)
    if not task.data.get("restored"):
        items = call(ad.list_archives)
        item = next((a for a in items if isinstance(a, dict) and a.get("archive_id") == task.archive_id), None) \
            if isinstance(items, list) else None
        if item is None:
            raise ops.StepFailed("unknown_archive", 404, NO_ARCHIVE)
        meta = item.get("shell_meta") if isinstance(item.get("shell_meta"), dict) else {}
        task.data["was_enabled"] = meta.get("was_enabled") is True
        task.data["username"] = meta.get("username") if isinstance(meta.get("username"), str) else None
        bot_id = item.get("bot_id")
        if valid_bot_id(bot_id):
            task.claim(bot_id=bot_id, person=person_of(item))
        task.save()
    out = _write_lifecycle(lambda: ad.restore(task.archive_id, op_id=task.op_id)) or {}
    _restored(task, out)
    return RESTORE_DONE


def _restored(task, out: dict) -> None:
    out = out if isinstance(out, dict) else {}
    bot_id = out.get("bot_id")
    if valid_bot_id(bot_id):
        task.claim(bot_id=bot_id)
    steps = out.get("steps") if isinstance(out.get("steps"), list) else []
    task.data["restored"] = True
    task.result.update(backend=task.backend, bot_id=bot_id or task.bot_id,
                       enabled=False, running=False, message=RESTORE_DONE, steps=steps)
    task.save()


def _restore_check(task):
    ad = registry.adapter(task.backend)
    out = _resolved(ad, task)
    if out is not None:
        if out.get("state") == "committed":
            _restored(task, out)
            return True
        return False if out.get("state") in ("rolled_back", "unchanged") else None
    still = any(isinstance(a, dict) and a.get("archive_id") == task.archive_id for a in ad.list_archives())
    present = bool(task.bot_id) and _find_bot(ad, task.bot_id) is not None
    if present and not still:
        _restored(task, {"bot_id": task.bot_id})
        return True
    return False if still and not present else None


def _delete_run(task):
    ad = _adapter_of(task)
    if task.data.get("deleted"):
        out = _resolved(ad, task)
        if out is None:
            return "已移进废纸篓"
        if out.get("state") == "committed" and out.get("wiped") is not False:
            _deleted(task, out)
            return "已移进废纸篓，令牌已抹掉"
        raise ops.StepStuck(scrub(out.get("message") or WIPE_PENDING), committed=True)
    out = _write_lifecycle(lambda: ad.delete(task.bot_id, task.bot_id, op_id=task.op_id)) or {}
    _deleted(task, out)
    if out.get("wiped") is False:
        raise ops.StepStuck(scrub(out.get("message") or WIPE_PENDING), committed=True)
    return "已移进废纸篓"


def _deleted(task, out: dict) -> None:
    out = out if isinstance(out, dict) else {}
    steps = out.get("steps") if isinstance(out.get("steps"), list) else []
    task.data["deleted"] = True
    task.result.update(message=DELETE_DONE,
                       trash=out.get("trash") if isinstance(out.get("trash"), (str, bool)) else None,
                       trash_path=out.get("trash_path") if isinstance(out.get("trash_path"), (str, bool)) else None,
                       steps=steps)
    task.save()


def _delete_check(task):
    ad = registry.adapter(task.backend)
    out = _resolved(ad, task)
    if out is not None:
        if out.get("state") == "committed":
            _deleted(task, out)
            return True
        return False if out.get("state") in ("rolled_back", "unchanged") else None
    if _find_bot(ad, task.bot_id) is None:
        _deleted(task, {})
        return True
    return False


# ── 新建（INTERFACE §2.4） ───────────────────────────────────────────────────

NO_CREATE = "这个底层还不能在管理台新建"
CREATE_TTS_MODELS = ("s1", "s2-pro", "s2.1-pro", "s2.1-pro-free", "drama-3")
CREATE_ID_RULE = "小写字母开头，只含小写字母、数字、下划线，2 到 32 个字符"
CREATE_BODY_KEYS = frozenset(("backend", "bot_id", "display_name", "telegram_token", "chat_id",
                              "persona_template", "persona_text", "voice", "enabled",
                              "confirm_other_side_unknown", "features"))
FEATURES_FIELD_MESSAGE = "features 必须是八项功能的布尔对象或键数组"
FEATURE_KEYS = ("voice", "image", "stickers", "moments", "proactive", "life", "relationship", "groups")
CREATE_STARTED = "已新建并启动"
CREATE_STOPPED = "已新建，保持停用"
CREATE_NOT_UP = "已新建，但调度器还没起来；看门狗会在 10 分钟内再试"
CREATE_BAD_TAKE = "令牌不在内存里了，请重新提交"


class TelegramCheckFailed(Exception):
    def __init__(self, code: str, http_status: int, message: str):
        super().__init__(message)
        self.code, self.http_status, self.message = code, http_status, message


_telegram_get_me = None


def use_telegram_get_me(fn) -> None:
    """登记 getMe 客户端（由 8767 壳导入时注入；测试可注入假客户端）。"""
    global _telegram_get_me
    _telegram_get_me = fn


def _telegram(token, rate_key: str) -> dict:
    if _telegram_get_me is None:
        raise TelegramCheckFailed("internal", 500, INTERNAL_MESSAGE)
    try:
        return _telegram_get_me(token, rate_key=rate_key)
    except TelegramCheckFailed:
        raise
    except Exception:
        raise TelegramCheckFailed("internal", 500, INTERNAL_MESSAGE) from None


def _parse_create_token(value):
    if not isinstance(value, str):
        return None
    value = value.strip()
    return SecretToken(value) if TOKEN_RE.fullmatch(value) else None


def _template_ids(ad) -> set:
    try:
        items = ad.templates()
    except (AdapterError, AdapterUnavailable, NotImplementedError):
        return set()
    except Exception:
        return set()
    return {i["id"] for i in items if isinstance(i, dict) and isinstance(i.get("id"), str)} \
        if isinstance(items, list) else set()


def _clean_voice(raw):
    if raw is None:
        return {}, None
    if not isinstance(raw, dict):
        return None, "voice 必须是键值表"
    extra = [k for k in raw if k not in VOICE_KEYS]
    if extra:
        return None, "voice 里不能有 %s" % extra[0]
    out = {}
    voice_id = raw.get("voice_id")
    if voice_id is not None and voice_id != "":
        if not isinstance(voice_id, str) or VOICE_ID_RE.fullmatch(voice_id) is None:
            return None, "语音编号只能用字母、数字、下划线、连字符，1 到 64 个字符"
        out["voice_id"] = voice_id
    model = raw.get("tts_model")
    if model is not None and model != "":
        if model not in TTS_MODELS:
            return None, "语音模型只能是 %s 之一" % "、".join(TTS_MODELS)
        out["tts_model"] = model
    for key in ("text_language", "voice_language"):
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            return None, "语言必须是字符串"
        value = value.strip()
        if not value:
            continue
        err = language_error(value)
        if err:
            return None, err
        out[key] = value
    return out, None


def _clean_create_features(raw):
    if isinstance(raw, dict):
        picked = []
        for key, value in raw.items():
            if key not in FEATURE_KEYS or not isinstance(value, bool):
                return None
            if value:
                picked.append(key)
        return picked
    if isinstance(raw, list):
        picked = []
        for key in raw:
            if not isinstance(key, str) or key not in FEATURE_KEYS or key in picked:
                return None
            picked.append(key)
        return picked
    return None


def validate_create_body(ad, body: dict):
    fields = {}
    for key in body:
        if key not in CREATE_BODY_KEYS:
            fields[key] = "有不认识的键：%s" % key
    bot_id = body.get("bot_id")
    err = new_bot_id_error(bot_id)
    if err:
        fields["bot_id"] = err
    display_name = bot_id
    if "display_name" in body:
        err = display_name_error(body.get("display_name"))
        if err:
            fields["display_name"] = err
        else:
            display_name = body["display_name"].strip()
    token = _parse_create_token(body.get("telegram_token"))
    if token is None:
        fields["telegram_token"] = TOKEN_FORMAT_MESSAGE
    chat_id = body.get("chat_id")
    err = chat_id_error(chat_id)
    if err:
        fields["chat_id"] = err
    persona = {}
    tid = body.get("persona_template")
    tpl_given = isinstance(tid, str) and tid.strip() != ""
    text = body.get("persona_text")
    txt_given = "persona_text" in body and (not isinstance(text, str) or text.strip() != "" or not tpl_given)
    if tpl_given and txt_given:
        fields["persona"] = PERSONA_BOTH_MESSAGE
    elif tpl_given:
        if tid not in _template_ids(ad):
            fields["persona_template"] = PERSONA_TEMPLATE_MISSING
        else:
            persona["persona_template"] = tid
    elif txt_given:
        try:
            size = len(text.encode("utf-8")) if isinstance(text, str) else -1
        except UnicodeEncodeError:
            size = -1
        if not isinstance(text, str) or not text.strip() or size > 256 * 1024:
            fields["persona_text"] = PERSONA_TEXT_MESSAGE
        else:
            persona["persona_text"] = text
    voice = {}
    if "voice" in body:
        voice, err = _clean_voice(body.get("voice"))
        if err:
            fields["voice"] = err
    features = None
    if "features" in body:
        features = _clean_create_features(body.get("features"))
        if features is None:
            fields["features"] = FEATURES_FIELD_MESSAGE
    enabled = body.get("enabled", True)
    if not isinstance(enabled, bool):
        fields["enabled"] = "enabled 必须是 true 或 false"
    confirm = body.get("confirm_other_side_unknown", False)
    if not isinstance(confirm, bool):
        fields["confirm_other_side_unknown"] = "confirm_other_side_unknown 必须是 true 或 false"
    if fields:
        raise Reject("invalid", 422, "入参不合规", fields)
    spec = {"bot_id": bot_id, "person": bot_id, "display_name": display_name,
            "telegram_username": "", "telegram_user_id": 0, "chat_id": chat_id,
            "enabled": enabled, "purpose": "new"}
    spec.update(persona)
    if voice:
        spec["voice"] = voice
    if features is not None and getattr(ad, "backend", "") == "claude":
        spec["features"] = features
    return spec, token, confirm


def _username_owner(username: str):
    if not isinstance(username, str) or not username:
        return None
    for name in registry.enabled_backends():
        try:
            bots = registry.adapter(name).bots()
        except (AdapterError, AdapterUnavailable):
            continue
        except Exception:
            continue
        for bot in bots if isinstance(bots, list) else ():
            known = bot.get("username") if isinstance(bot, dict) else None
            if isinstance(known, str) and known and known.lower() == username.lower():
                return {"backend": name, "bot_id": str(bot.get("bot_id"))}
    return None


def check_token(token_text, rate_key: str) -> dict:
    token = _parse_create_token(token_text)
    if token is None:
        raise Reject("invalid", 422, TOKEN_FORMAT_MESSAGE, {"telegram_token": TOKEN_FORMAT_MESSAGE})
    try:
        out = _telegram(token, rate_key)
    except TelegramCheckFailed as e:
        raise Reject(e.code, e.http_status, e.message) from None
    username = out.get("username")
    return {"username": username, "bot_user_id": out.get("bot_user_id"), "in_use_by": _username_owner(username)}


def create_form() -> dict:
    backends, templates = [], {}
    for name in registry.enabled_backends():
        entry = {"backend": name, "label": registry.label(name), "can_create": False, "reason": ""}
        ad, caps = None, {}
        try:
            ad = registry.adapter(name)
            meta = ad.meta()
            caps = meta.get("capabilities") if isinstance(meta.get("capabilities"), dict) else {}
            if caps.get("bot.create") is not True:
                entry["reason"] = NO_CREATE
            else:
                entry["can_create"] = True
        except AdapterUnavailable as e:
            entry["reason"] = e.message or UNAVAILABLE_TEXT.get(e.status, UNAVAILABLE_TEXT["error"])
        except AdapterError as e:
            entry["reason"] = e.message or UNAVAILABLE_TEXT["error"]
        except Exception:
            entry["reason"] = UNAVAILABLE_TEXT["error"]
        backends.append(entry)
        items = []
        if ad is not None:
            try:
                items = ad.templates()
            except Exception:
                items = []
        templates[name] = [i for i in items if isinstance(i, dict)
                           and isinstance(i.get("id"), str) and isinstance(i.get("label"), str)] \
            if isinstance(items, list) else []
    default_chat_id = None
    if "claude" in registry.enabled_backends():
        try:
            fn = getattr(registry.adapter("claude"), "chat_id_default", None)
            if callable(fn):
                default_chat_id = fn()
        except Exception:
            default_chat_id = None
    return {"backends": backends, "templates": templates, "tts_models": list(CREATE_TTS_MODELS),
            "id_rule": CREATE_ID_RULE, "defaults": {"chat_id": default_chat_id}}


def start_create(body: dict, rate_key: str) -> str:
    if not isinstance(body, dict):
        raise Reject("invalid", 422, "请求体必须是 JSON 对象")
    backend = body.get("backend")
    if not isinstance(backend, str) or backend not in registry.enabled_backends():
        raise Reject("unknown_backend", 404, "没有这种底层")
    ad, caps, _levels = _adapter_caps(backend)
    if caps.get("bot.create") is not True:
        raise Reject("unsupported", 501, UNSUPPORTED_MESSAGE)
    spec, token, confirm = validate_create_body(ad, body)
    _precheck_create_exists(backend, spec["bot_id"], confirm)
    try:
        lock = ops.acquire(ops.new_op_id("create"), persons=(spec["bot_id"],))
    except ops.LifecycleBusy as e:
        raise Reject("busy", 409, e.message)
    spec["op_id"] = lock.op_id
    try:
        return ops.submit(lock, "create", backend=backend, bot_id=spec["bot_id"], person=spec["bot_id"],
                          token=token, rate_key=rate_key, payload=spec,
                          data={"confirm_other_side_unknown": confirm, "token_source": "form"})
    except BaseException:
        lock.release()
        raise


def _precheck_create_exists(backend: str, bot_id: str, confirm: bool) -> None:
    """受理期查重（INTERFACE §2.4）：编号已被占用 → 409 exists，不再生成 op。

    与后台 ``_create_check_unique`` 同一口径；两侧读不到时，目标侧按不可用报错，
    另一侧在未确认时 409 other_side_unknown，确认后才交给后台继续。
    """
    for name in registry.enabled_backends():
        try:
            ad = registry.adapter(name)
            bots = ad.bots()
        except (AdapterUnavailable, AdapterError) as e:
            if name == backend:
                if isinstance(e, AdapterUnavailable):
                    raise unavailable_to_reject(e)
                raise Reject(e.code, e.http_status, e.message, getattr(e, "fields", None))
            if not confirm:
                raise Reject("other_side_unknown", 409, _side_unknown(name))
            continue
        except Exception:
            if name == backend:
                raise Reject("backend_down", 502, INTERNAL_MESSAGE)
            if not confirm:
                raise Reject("other_side_unknown", 409, _side_unknown(name))
            continue
        for item in bots if isinstance(bots, list) else ():
            if isinstance(item, dict) and str(item.get("bot_id")) == bot_id:
                raise Reject("exists", 409, "编号 %s 已被占用：%s 侧已有" % (bot_id, name))
    if backend == "claude" and _claude_residue(bot_id):
        raise Reject("exists", 409, "编号 %s 已被占用：claude 侧已有" % bot_id)


def _side_unknown(name: str) -> str:
    return "%s 现在连不上，没法确认那边没有同一只 bot 在跑；确认没有的话再点一次" % registry.label(name)


def _adapter_failure(e):
    if isinstance(e, AdapterUnavailable):
        r = unavailable_to_reject(e)
        return ops.StepFailed(r.code, r.http_status, r.message)
    if isinstance(e, AdapterError):
        return ops.StepFailed(e.code, e.http_status, e.message)
    return ops.StepFailed("step_failed", 500, INTERNAL_MESSAGE)


def _config_dir() -> str:
    return os.environ.get("CLAUDEBOT_CONFIG_DIR") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "configs")


def _channels_dir() -> str:
    return os.environ.get("CLAUDEBOT_CHANNELS_ROOT") or os.path.join(
        os.path.expanduser("~"), ".claude", "channels")


def _claude_residue(bot_id: str) -> bool:
    """claude 侧本地残留：yml、频道目录、停用标记、已迁出标记。"""
    home = os.path.expanduser("~")
    paths = [os.path.join(_config_dir(), bot_id + ".yml"), os.path.join(_channels_dir(), bot_id),
             os.path.join(home, ".claude", "dispatcher", ".disabled", bot_id),
             os.path.join(home, ".claude", "dispatcher", ".moved", bot_id)]
    return any(os.path.lexists(p) for p in paths)


def _create_check_unique(task):
    confirm = task.data.get("confirm_other_side_unknown") is True
    bot_id = task.bot_id
    for name in registry.enabled_backends():
        try:
            ad = registry.adapter(name)
            bots = ad.bots()
        except (AdapterUnavailable, AdapterError) as e:
            if name == task.backend:
                raise _adapter_failure(e)
            if not confirm:
                raise ops.StepFailed("other_side_unknown", 409, _side_unknown(name))
            continue
        except Exception:
            if name == task.backend:
                raise ops.StepFailed("step_failed", 500, INTERNAL_MESSAGE)
            if not confirm:
                raise ops.StepFailed("other_side_unknown", 409, _side_unknown(name))
            continue
        for item in bots if isinstance(bots, list) else ():
            if isinstance(item, dict) and (str(item.get("bot_id")) == bot_id or person_of(item) == bot_id):
                raise ops.StepFailed("exists", 409, "编号 %s 已被占用：%s 侧已有" % (bot_id, name))
        try:
            archives = ad.list_archives()
        except NotImplementedError:
            archives = []
        except AdapterError as e:
            if e.http_status == 404 and e.code == "unknown":
                archives = []
            elif name == task.backend:
                raise _adapter_failure(e)
            elif not confirm:
                raise ops.StepFailed("other_side_unknown", 409, _side_unknown(name))
            else:
                archives = []
        except AdapterUnavailable as e:
            if name == task.backend:
                raise _adapter_failure(e)
            if not confirm:
                raise ops.StepFailed("other_side_unknown", 409, _side_unknown(name))
            archives = []
        except Exception:
            if name == task.backend:
                raise ops.StepFailed("step_failed", 500, INTERNAL_MESSAGE)
            if not confirm:
                raise ops.StepFailed("other_side_unknown", 409, _side_unknown(name))
            archives = []
        for item in archives if isinstance(archives, list) else ():
            if isinstance(item, dict) and str(item.get("bot_id")) == bot_id:
                raise ops.StepFailed("exists", 409, "编号 %s 已被占用：%s 侧已归档" % (bot_id, name))
    if task.backend == "claude" and _claude_residue(bot_id):
        raise ops.StepFailed("exists", 409, "编号 %s 已被占用：claude 侧已有" % bot_id)
    return "查重通过"


def _create_verify(task):
    if task.token is None:
        raise ops.StepFailed("step_failed", 500, CREATE_BAD_TAKE)
    try:
        out = _telegram(task.token, task.rate_key or "_anonymous")
    except TelegramCheckFailed as e:
        raise ops.StepFailed(e.code, e.http_status, e.message) from None
    task.data["username"] = out.get("username")
    task.data["telegram_user_id"] = out.get("bot_user_id")
    if isinstance(getattr(task, "payload", None), dict):
        task.payload["telegram_username"] = out.get("username") or ""
        task.payload["telegram_user_id"] = out.get("bot_user_id") or 0
    task.save()
    return "令牌有效（@%s）" % out.get("username")


def _create_token_unique(task):
    username = task.data.get("username")
    if not isinstance(username, str) or not username:
        raise ops.StepFailed("token_rejected", 422, "Telegram 不认这个令牌，请到 BotFather 复制完整的令牌")
    for name in registry.enabled_backends():
        try:
            bots = registry.adapter(name).bots()
        except (AdapterError, AdapterUnavailable):
            continue
        except Exception:
            continue
        for item in bots if isinstance(bots, list) else ():
            known = item.get("username") if isinstance(item, dict) else None
            if isinstance(known, str) and known and known.lower() == username.lower():
                raise ops.StepFailed("token_in_use", 409, "这个 Telegram bot（@%s）已经是 %s/%s 在用"
                                     % (username, name, str(item.get("bot_id"))))
    return "用户名没有被占用"


def _created(task, out: dict) -> None:
    out = out if isinstance(out, dict) else {}
    enabled = out.get("enabled") if isinstance(out.get("enabled"), bool) else task.data.get("enabled", True)
    running = out.get("running") is True
    message = CREATE_STOPPED if not enabled else (CREATE_STARTED if running else CREATE_NOT_UP)
    steps = out.get("steps") if isinstance(out.get("steps"), list) else []
    task.rec["result"] = {"backend": task.backend, "bot_id": out.get("bot_id") or task.bot_id,
                          "person": out.get("person") or task.bot_id,
                          "username": out.get("username") or task.data.get("username"),
                          "enabled": enabled, "running": running, "message": message, "steps": steps}
    for warning in out.get("warnings") or ():
        if isinstance(warning, str) and warning:
            task.warn(warning)
    task.data["created"] = True
    task.save()


def _create_run(task):
    spec = getattr(task, "payload", None)
    if not isinstance(spec, dict):
        raise ops.StepFailed("step_failed", 500, CREATE_BAD_TAKE)
    ad = _adapter_of(task)
    try:
        out = _write_lifecycle(lambda: ad.create(spec, task.token))
    finally:
        task.token = None
    _created(task, out)
    return task.result["message"]


def _create_check(task):
    ad = registry.adapter(task.backend)
    out = _resolved(ad, task)
    if out is not None:
        if out.get("state") == "committed":
            _created(task, out)
            return True
        state = out.get("state")
        if state in ("rolled_back", "unchanged"):
            message = out.get("message")
            inner = task.rec.get("_internal")
            if isinstance(inner, dict) and isinstance(message, str) and message:
                inner["failure"] = {"code": "step_failed", "http_status": 500, "message": scrub(message)}
                if state == "rolled_back":
                    inner["changed"] = True
            return False
        return None
    try:
        me = _find_bot(ad, task.bot_id)
    except Reject:
        return None
    if me is None:
        return False
    _created(task, {"bot_id": task.bot_id, "person": person_of(me), "username": me.get("username"),
                    "enabled": _enabled_of(me), "running": polling_of(me)})
    return True


def _create_voice(task):
    spec = getattr(task, "payload", None)
    voice = spec.get("voice") if isinstance(spec, dict) else None
    if task.backend != "dsh":
        raise ops.StepSkipped("claude 侧新建时已写入语音设置")
    if not isinstance(voice, dict) or not voice:
        raise ops.StepSkipped("没有给语音设置")
    try:
        ad = registry.adapter("dsh")
        caps = ad.meta().get("capabilities")
        if not isinstance(caps, dict) or caps.get("bot.voice") is not True:
            raise ops.StepSkipped("底层不提供语音设置")
        ad.set_voice(task.bot_id, voice)
    except ops.StepSkipped:
        raise
    except Exception as e:
        raise ops.StepFailed("step_failed", 500, "语音没设置上，可以到语音块再设一次（%s）"
                             % type(e).__name__) from None
    return "语音设置已保存"


# ── 切换底层（INTERFACE §5） ─────────────────────────────────────────────────

SWITCH_TARGET_FIELD = "要切到另一种底层"
SWITCH_CONFLICT = "两边同时在收消息（%s/%s），先停一边"
SWITCH_OTHER_RUNNING = "另一边的同一只 bot（%s/%s）还在收消息，先停那边"
SWITCH_TOKEN_REQUIRED = "另一边还没有这只 bot，程序没能自动取到它的令牌（%s），请填它的 Telegram 令牌"
SWITCH_TOKEN_NO_REASON = "源边没有交出令牌"
SWITCH_TOKEN_MISMATCH = "这个令牌对应的是 @%s，不是这只 bot（@%s）"
SWITCH_EXISTS = "目标边已经有编号 %s 的配置，但不是同一只 bot，没法切换"
SWITCH_BAD_TARGET_ID = "编号 %s 不合 %s 的规则，没法切换"


def _switch_source_chat_id(task, me: dict):
    value = me.get("chat_id")
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return value
    if task.backend != "claude":
        return None
    try:
        cfg = claude_files._load_yml(os.path.join(_config_dir(), task.bot_id + ".yml")) or {}
    except Exception:
        return None
    value = cfg.get("chat_id")
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return value
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    return None


def _switch_source_channel_name(task) -> str:
    if task.backend == "claude":
        try:
            cfg = claude_files._load_yml(os.path.join(_config_dir(), task.bot_id + ".yml")) or {}
        except Exception:
            cfg = {}
        path = cfg.get("bot_channel_path")
        if isinstance(path, str) and os.path.isabs(path):
            return os.path.basename(os.path.normpath(path))
    return task.bot_id


def _switch_meta(ad) -> dict:
    meta = call(ad.meta)
    return meta.get("capabilities") if isinstance(meta, dict) and isinstance(meta.get("capabilities"), dict) else {}


def start_switch(backend: str, bot_id: str, body: dict, rate_key: str) -> str:
    if not isinstance(body, dict):
        raise Reject("invalid", 422, "请求体必须是 JSON 对象")
    prepare_ids(backend, bot_id)
    to = body.get("to")
    names = registry.enabled_backends()
    if not isinstance(to, str) or to == backend or to not in names:
        raise Reject("invalid", 422, SWITCH_TARGET_FIELD, {"to": SWITCH_TARGET_FIELD})
    token = None
    if body.get("telegram_token") is not None:
        token = _parse_create_token(body.get("telegram_token"))
        if token is None:
            raise Reject("invalid", 422, TOKEN_FORMAT_MESSAGE, {"telegram_token": TOKEN_FORMAT_MESSAGE})
    for name in (backend, to):
        ad, caps, _levels = _adapter_caps(name)
        del ad
        if caps.get("bot.enable") is not True:
            raise Reject("unsupported", 501, UNSUPPORTED_MESSAGE)
    _ad, target_caps, _levels = _adapter_caps(to)
    if target_caps.get("bot.group_chat") is not True:
        raise Reject("unsupported", 501, "目标底层不提供群设置，没法把群带过去")
    # 受理前先确认源侧真有这只 bot：未知 bot 是 404，不是一条注定失败的后台任务。
    src_ad = call(lambda: registry.adapter(backend))
    src = _find_bot(src_ad, bot_id)
    if src is None:
        raise Reject("unknown_bot", 404, UNKNOWN_BOT_MESSAGE)
    # 已经迁出（或正在迁出）的 bot 再切一次，同步就返回 409；否则会建一条注定失败的后台任务。
    moved = src.get("moved_to")
    if isinstance(moved, str) and moved:
        raise Reject("moved", 409,
                     SWITCH_MOVED_PENDING if moved == "pending" else SWITCH_MOVED % moved)
    return _submit("switch", backend, bot_id=bot_id, target={"backend": to, "bot_id": bot_id},
                   token=token, rate_key=rate_key,
                   data={"token_source": "form" if token is not None else "handoff"})


def _switch_check(task) -> str:
    src_ad = _adapter_of(task)
    dst_ad = call(lambda: registry.adapter(task.target_backend))
    src_bots = call(src_ad.bots)
    dst_bots = call(dst_ad.bots)
    src_bots = src_bots if isinstance(src_bots, list) else []
    dst_bots = dst_bots if isinstance(dst_bots, list) else []
    me = next((b for b in src_bots if isinstance(b, dict) and str(b.get("bot_id")) == task.bot_id), None)
    if me is None:
        raise ops.StepFailed("unknown_bot", 404, UNKNOWN_BOT_MESSAGE)
    src_person = person_of(me)
    task.claim(person=src_person)
    moved = me.get("moved_to")
    if isinstance(moved, str) and moved:
        raise ops.StepFailed("moved", 409, SWITCH_MOVED_PENDING if moved == "pending" else SWITCH_MOVED % moved)
    src_polling = polling_of(me)
    hit = next((b for b in dst_bots if isinstance(b, dict) and same_bot(me, b)), None)
    if hit is not None and polling_of(hit):
        if src_polling:
            raise ops.StepFailed("conflict", 409, CONFLICT_MESSAGE)
        raise ops.StepFailed("other_side_running", 409,
                             SWITCH_OTHER_RUNNING % (task.target_backend, str(hit.get("bot_id"))))
    target_bot_id = str(hit.get("bot_id")) if hit is not None else (
        src_person if task.target_backend == "claude" else task.bot_id)
    if hit is None:
        if not valid_bot_id(target_bot_id):
            raise ops.StepFailed("invalid", 422, SWITCH_BAD_TARGET_ID % (target_bot_id, task.target_backend))
        caps = _switch_meta(dst_ad)
        if caps.get("bot.create") is not True:
            raise ops.StepFailed("unsupported", 501, UNSUPPORTED_MESSAGE)
        clash = next((b for b in dst_bots if isinstance(b, dict) and str(b.get("bot_id")) == target_bot_id), None)
        if clash is not None:
            raise ops.StepFailed("exists", 409, SWITCH_EXISTS % target_bot_id)
    task.data.update(
        source_person=src_person,
        source_username=me.get("username") if isinstance(me.get("username"), str) else None,
        source_display_name=str(me.get("display_name") or task.bot_id),
        source_chat_id=_switch_source_chat_id(task, me),
        source_polling=src_polling,
        target_bot_id=target_bot_id,
        target_person=person_of(hit) if hit is not None else src_person,
        target_username=(hit.get("username") if hit is not None and isinstance(hit.get("username"), str) else None),
        target_existing=hit is not None,
    )
    task.result["from"] = {"backend": task.backend, "bot_id": task.bot_id}
    task.result["to"] = {"backend": task.target_backend, "bot_id": target_bot_id}
    task.rec["target"] = {"backend": task.target_backend, "bot_id": target_bot_id}
    if task.data.get("target_existing") is True:
        task.result["token_source"] = "none"
    task.result.setdefault("imported", {"messages": 0, "promises": 0})
    task.save()
    return "两边名单都核对过"


def _switch_take_token(task) -> str:
    if task.data.get("target_existing") is True:
        raise ops.StepSkipped("目标边已有同一只 bot，不需要令牌")
    token = task.token
    token_source = "form"
    if token is None:
        token_source = "handoff"
        src_ad = _adapter_of(task)
        try:
            token = src_ad.take_token(task.bot_id, task)
        except AdapterUnavailable as e:
            r = unavailable_to_reject(e)
            raise ops.StepFailed(r.code, r.http_status, r.message) from None
        except (AdapterError, NotImplementedError) as e:
            reason = scrub(getattr(e, "message", None) or SWITCH_TOKEN_NO_REASON)
            raise ops.StepFailed("token_required", 422, SWITCH_TOKEN_REQUIRED % reason) from None
        except Exception:
            raise ops.StepFailed("token_required", 422, SWITCH_TOKEN_REQUIRED % SWITCH_TOKEN_NO_REASON) from None
        if not isinstance(token, SecretToken):
            raise ops.StepFailed("token_required", 422, SWITCH_TOKEN_REQUIRED % SWITCH_TOKEN_NO_REASON)
        task.token = token
    try:
        out = _telegram(token, task.rate_key or "_anonymous")
    except TelegramCheckFailed as e:
        raise ops.StepFailed(e.code, e.http_status, e.message) from None
    username = out.get("username") if isinstance(out.get("username"), str) else None
    src_username = task.data.get("source_username")
    if isinstance(src_username, str) and src_username and username \
            and src_username.lower() != username.lower():
        raise ops.StepFailed("token_mismatch", 422, SWITCH_TOKEN_MISMATCH % (username, src_username))
    task.data["telegram_username"] = username
    task.data["telegram_user_id"] = out.get("bot_user_id")
    task.data["token_source"] = token_source
    task.result["token_source"] = token_source
    task.save()
    if not (isinstance(src_username, str) and src_username):
        return "令牌有效（@%s）；源边的用户名未知，无法核对用户名" % username
    return "令牌有效（@%s）" % username


def _switch_create_run(task) -> str:
    if task.data.get("target_existing") is True:
        raise ops.StepSkipped("目标边已有同一只 bot，直接用")
    token = task.token
    if token is None:
        raise ops.StepFailed("step_failed", 500, ops.TOKEN_LOST_MESSAGE)
    spec = {"bot_id": task.data.get("target_bot_id"), "person": task.data.get("target_person"),
            "display_name": task.data.get("source_display_name"),
            "telegram_username": task.data.get("telegram_username") or "",
            "telegram_user_id": task.data.get("telegram_user_id") or 0,
            "chat_id": task.data.get("source_chat_id"), "enabled": False,
            "purpose": "switch_in", "op_id": task.op_id}
    ad = call(lambda: registry.adapter(task.target_backend))
    try:
        out = _write_lifecycle(lambda: ad.create(spec, token)) or {}
    finally:
        task.token = None
    bot_id = out.get("bot_id") if isinstance(out.get("bot_id"), str) else spec["bot_id"]
    task.data["target_bot_id"] = bot_id
    task.data["target_person"] = out.get("person") or spec["person"]
    task.result["to"] = {"backend": task.target_backend, "bot_id": bot_id}
    task.save()
    return "已在 %s 新建目标，保持停用" % task.target_backend


def _switch_create_check(task):
    ad = call(lambda: registry.adapter(task.target_backend))
    try:
        bots = ad.bots()
    except (AdapterError, AdapterUnavailable):
        return None
    if not isinstance(bots, list):
        return None
    bot_id = task.data.get("target_bot_id")
    return any(isinstance(b, dict) and str(b.get("bot_id")) == str(bot_id) for b in bots)


def _switch_create_undo(task):
    """提交点前回退：目标边一律回停用（新建的配置保留，与 CBL 口径一致）。"""
    bot_id = task.data.get("target_bot_id")
    if not isinstance(bot_id, str) or not bot_id:
        return "目标边没有新建"
    try:
        ad = registry.adapter(task.target_backend)
        ad.set_enabled(bot_id, False, op_id=task.op_id, moved_to=None)
    except AdapterError as e:
        if e.code == "unknown_bot":
            return "目标边没有新建"
        raise ops.StepFailed("step_failed", 500, "目标边没能停下（%s）" % scrub(e.message)) from None
    except (AdapterUnavailable, Reject) as e:
        raise ops.StepFailed("step_failed", 500, "目标边没能停下（%s）" % scrub(e.message)) from None
    return "目标边已停用"


def _switch_clear_and_restart(task):
    ad = _adapter_of(task)
    notes = []
    try:
        ad.set_enabled(task.bot_id, False, op_id=task.op_id, moved_to=None)
    except (Reject, AdapterError, AdapterUnavailable) as e:
        notes.append("已迁出标记没清掉（%s）" % scrub(e.message))
    if task.data.get("source_polling") is True:
        try:
            _check_other_side(task.backend, _me(task), False, rollback=True)
            ad.set_enabled(task.bot_id, True, op_id=task.op_id)
        except (Reject, AdapterError, AdapterUnavailable) as e:
            notes.append(KEPT_PREFIX + scrub(e.message))
    return "；".join(notes) if notes else None


def _switch_stop_source(task) -> str:
    ad = _adapter_of(task)
    try:
        _write(lambda: ad.set_enabled(task.bot_id, False, op_id=task.op_id, moved_to="pending"))
    except ops.ResultUnknown:
        raise
    except (AdapterError, Reject) as e:
        note = _switch_clear_and_restart(task)
        raise ops.StepFailed(e.code, e.http_status, e.message, rolled_back=True, note=note) from None
    return "源边已停，记下已迁出（进行中）"


def _switch_stop_source_check(task):
    ad = _adapter_of(task)
    try:
        me = _find_bot(ad, task.bot_id)
    except Reject:
        return None
    return None if me is None else (not polling_of(me))


SWITCH_STAYED_STOPPED = "源边本来就是停用的，保持停用"


def _switch_source_polling_now(task):
    try:
        me = _find_bot(_adapter_of(task), task.bot_id)
    except Reject:
        return None
    return None if me is None else polling_of(me)


def _switch_stop_source_undo(task):
    ad = _adapter_of(task)
    notes = []
    try:
        ad.set_enabled(task.bot_id, False, op_id=task.op_id, moved_to=None)
    except (Reject, AdapterError, AdapterUnavailable) as e:
        notes.append("已迁出标记没清掉（%s）" % scrub(e.message))
    polling = task.data.get("source_polling")
    if not isinstance(polling, bool):
        polling = _switch_source_polling_now(task)
    started = False
    if polling is True:
        try:
            _check_other_side(task.backend, _me(task), False, rollback=True)
            ad.set_enabled(task.bot_id, True, op_id=task.op_id)
            started = True
        except (Reject, AdapterError, AdapterUnavailable) as e:
            notes.append(KEPT_PREFIX + scrub(e.message))
    elif polling is None:
        notes.append("没法确认源边原来在不在跑，没有自动启用")
    inner = task.rec.get("_internal")
    prev = inner.get("final_attention") if isinstance(inner, dict) else None
    prev = prev if isinstance(prev, str) and prev else None
    if notes or prev:
        src_polling, dst_polling = _switch_polling_now(task)
        parts = [p for p in (prev, ("源边没有完全回到切换前：" + "；".join(notes)) if notes else None) if p]
        text = "；".join(parts) + "；源边现在%s在收消息，目标边现在%s在收消息" % (
            "没" if src_polling is False else "可能还", "没" if dst_polling is False else "可能还")
        if isinstance(inner, dict):
            inner["final_attention"] = scrub(text)
        return "已尽量撤回"
    return STARTED_BACK if started else SWITCH_STAYED_STOPPED


def _switch_polling_now(task):
    try:
        src_me = _find_bot(_adapter_of(task), task.bot_id)
    except Reject:
        src_me = None
    try:
        dst_me = _find_bot(call(lambda: registry.adapter(task.target_backend)), task.data.get("target_bot_id"))
    except Reject:
        dst_me = None
    return (polling_of(src_me) if src_me is not None else None,
            polling_of(dst_me) if dst_me is not None else None)


def _switch_start_target(task) -> str:
    dst_ad = call(lambda: registry.adapter(task.target_backend))
    bot_id = task.data.get("target_bot_id")
    me = _find_bot(dst_ad, bot_id)
    if me is not None and isinstance(me.get("moved_to"), str) and me["moved_to"]:
        _write(lambda: dst_ad.set_enabled(bot_id, False, op_id=task.op_id, moved_to=None))
    extra = {}
    caps = _switch_meta(dst_ad)
    if task.target_backend == "dsh" and caps.get("bot.switch_in") is True:
        extra["import_from"] = _switch_source_channel_name(task)
    try:
        out = _write(lambda: dst_ad.set_enabled(bot_id, True, op_id=task.op_id, **extra)) or {}
    except ops.ResultUnknown:
        raise
    except (AdapterError, Reject) as e:
        raise ops.StepFailed("step_failed", 500, e.message) from None
    if out.get("enabled") is True and out.get("running") is False:
        return "目标边已启用，调度器还没起来"
    return "目标边已启用"


def _switch_start_target_check(task):
    try:
        me = _find_bot(call(lambda: registry.adapter(task.target_backend)), task.data.get("target_bot_id"))
    except Reject:
        return None
    return None if me is None else polling_of(me)


def _switch_start_target_undo(task):
    bot_id = task.data.get("target_bot_id")
    if not isinstance(bot_id, str) or not bot_id:
        return "目标边没有新建"
    try:
        ad = registry.adapter(task.target_backend)
        ad.set_enabled(bot_id, False, op_id=task.op_id, moved_to=None)
    except AdapterError as e:
        if e.code == "unknown_bot":
            return "目标边没有新建"
        raise ops.StepFailed("step_failed", 500, "目标边没能停下（%s）" % scrub(e.message)) from None
    except (AdapterUnavailable, Reject) as e:
        raise ops.StepFailed("step_failed", 500, "目标边没能停下（%s）" % scrub(e.message)) from None
    return "目标边已停用"


def _switch_verify(task) -> str:
    src_me = _find_bot(_adapter_of(task), task.bot_id)
    dst_me = _find_bot(call(lambda: registry.adapter(task.target_backend)), task.data.get("target_bot_id"))
    if src_me is None or dst_me is None:
        raise ops.StepFailed("step_failed", 500, "两边名单里找不到这只 bot，没法核对")
    if polling_of(src_me):
        raise ops.StepFailed("step_failed", 500, "源边还在收消息，这次切换没有完成")
    if not polling_of(dst_me):
        raise ops.StepFailed("step_failed", 500, "目标边还没起来，这次切换没有完成")
    task.result["message"] = "已切到 %s" % task.target_backend
    task.result.setdefault("imported", {"messages": 0, "promises": 0})
    task.save()
    return "目标边在收消息，源边已停"


def _switch_mark_moved(task) -> str:
    ad = _adapter_of(task)
    _write(lambda: ad.set_enabled(task.bot_id, False, op_id=task.op_id, moved_to=task.target_backend))
    return "源边已记迁出到 %s" % task.target_backend


def _switch_refresh(task) -> str:
    try:
        import bots_registry
        bots_registry.reload()
    except Exception as e:
        raise ops.StepFailed("step_failed", 500, "端口表没刷新（%s）" % type(e).__name__) from None
    return REFRESHED


# ── 步骤登记 ────────────────────────────────────────────────────────────────

_CHECKS = {"archive": _archive_check, "restore": _restore_check, "delete": _delete_check}

ops.register("archive", (
    ops.Step("stop", _stop_run, undo=_stop_undo, check=_stop_check),
    ops.Step("archive", _archive_run, undo=_commit_undo, check=_archive_check, commit=True),
    ops.Step("refresh", _refresh_run, warn_only=True),
))
ops.register("restore", (
    ops.Step("restore", _restore_run, undo=_commit_undo, check=_restore_check, commit=True),
    ops.Step("refresh", _refresh_run, warn_only=True),
))
ops.register("delete", (
    ops.Step("stop", _stop_run, undo=_stop_undo, check=_stop_check),
    ops.Step("delete", _delete_run, undo=_commit_undo, check=_delete_check, commit=True),
    ops.Step("refresh", _refresh_run, warn_only=True),
))
ops.register("create", (
    ops.Step("check_unique", _create_check_unique),
    ops.Step("verify_token", _create_verify, needs_token=True),
    ops.Step("check_token_unique", _create_token_unique),
    ops.Step("create", _create_run, check=_create_check, commit=True, needs_token=True),
    ops.Step("voice", _create_voice, warn_only=True),
))
ops.register("switch", (
    ops.Step("check", _switch_check),
    ops.Step("take_token", _switch_take_token),
    ops.Step("create_target", _switch_create_run, undo=_switch_create_undo, check=_switch_create_check),
    ops.Step("stop_source", _switch_stop_source, undo=_switch_stop_source_undo, check=_switch_stop_source_check),
    ops.Step("start_target", _switch_start_target, undo=_switch_start_target_undo, check=_switch_start_target_check),
    ops.Step("verify", _switch_verify, commit=True),
    ops.Step("mark_moved", _switch_mark_moved),
    ops.Step("refresh", _switch_refresh, warn_only=True),
))
