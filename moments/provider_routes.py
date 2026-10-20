"""provider 管理面板：蓝图 + R2–R10 接口（INTERFACE §2、§5、§6）。

HTTP 出口一律用脱敏形状；明文 api_key 只在 provider_config 内部与 set_bot_provider 入参出现。
"""
import http.client
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import provider_config as pc  # noqa: E402
from adapters import claude_proc  # noqa: E402
from bots_registry import is_enabled as _bot_enabled  # noqa: E402
from flask import Blueprint, jsonify, render_template, request  # noqa: E402

provider_bp = Blueprint("provider", __name__)


@provider_bp.before_request
def _reset_proxy_generation():
    import provider_proxy
    provider_proxy.take_request_generation()   # 线程复用：清掉上一个请求的残留
    pc.reset_request_warn_state()              # 同上：清残留 warning / 未验证标记（E-15）


@provider_bp.after_request
def _proxy_generation_header(resp):
    """9.2 表：本请求分配了代号（非纯 no-op）且不是 4xx 才带 X-Proxy-Generation。
    安全闸 500 发生在写入段之前，天然没有代号；自检失败的 500 带头。"""
    import provider_proxy
    g = provider_proxy.take_request_generation()
    if g is not None and not 400 <= resp.status_code < 500:
        resp.headers["X-Proxy-Generation"] = str(g)
    return resp

_MODELS_TIMEOUT = 8
_MAX_BODY = 1024 * 1024
# 一次拉取的分页上限。8 秒只是单次读超时：上游每页都说"还有下一页"时，没有这两道
# 上限循环永不结束，占住一个线程。页数取 50——封顶 500 条、常见页长 20 条时取满
# 要 25 页，留一倍余量；整体截止在每页开始前检查，最坏再多等一次读超时。
_MODELS_MAX_PAGES = 50
_MODELS_DEADLINE = 30
_UNSENDABLE = "请求无法发出：地址或密钥含非法字符"


def _bot_ids() -> set:
    """发现名单唯一来源：T04``bots_registry``（含停用 bot）。"""
    return pc._discovered_bot_ids()


def _stopped(bot_id: str) -> bool:
    cfg = pc._bot_cfg(bot_id)
    return cfg is not None and not _bot_enabled(cfg, bot_id)


def _restart_worker(bot_id: str, force: bool = False) -> dict:
    """T13 适配器：重启只经 dispatcher 本机 HTTP，不碰 tmux。状态收敛成面板四态的子集。"""
    try:
        res = claude_proc.restart_worker(bot_id, force=bool(force))
    except claude_proc.DispatcherError:
        return {"bot_id": bot_id, "status": "spawn_failed", "detail": "dispatcher 不可用"}
    except Exception:
        return {"bot_id": bot_id, "status": "spawn_failed", "detail": "重启失败"}
    ok = res.get("status") == "ok"
    return {"bot_id": bot_id, "status": "ok" if ok else "spawn_failed",
            "detail": res.get("message") or "", "restart": res}


def _settle(bot_id: str, res: dict) -> None:
    """重启结果记账：与 CBL settle 同语义，只保留目标仓可达的三态；记账失败不挡响应。"""
    status = res.get("status")
    try:
        if status == "ok":
            pc.set_applied(bot_id)
        elif status == "spawn_failed":
            pc.record_failure(bot_id, res.get("detail") or "")
        elif status == "stopped":
            pc.clear_pending(bot_id)
    except Exception:
        pass


def _json_body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else None


def _err_dict(msg, code=400, **extra):
    """错误体的字典形式（不依赖请求对象）：供抽出来的业务函数返回，路由再 jsonify。"""
    body = {"error": msg}
    body.update(extra)
    return body, code


def _err(msg, code=400, **extra):
    body, code = _err_dict(msg, code, **extra)
    return jsonify(body), code


def _err_coded(msg, http_status, reason):
    """带业务 code 的错误体（``_err`` 的 code 形参是 HTTP 状态码，不能复用）。"""
    return jsonify({"error": msg, "code": reason}), http_status


def _with_warning(payload: dict) -> dict:
    """E-15（审计条目修法二）：保存响应若有「地址未验证」warning 就带上（不拦截、不改 ok 语义）。

    条件性带键：正常路径响应形状与既有一字不差。
    """
    w = pc.take_request_warning()
    if w:
        payload["warning"] = w
        payload.setdefault("ok", True)
    return payload


def _bind_warning(entry: dict) -> str | None:
    """绑定时把 provider 条目上的「openai 地址未验证」标记转成面板可显示的 warning。

    标记由保存/开代理时的地址探针写入（探针 404）；显式覆盖地址或探针非 404 时被清除。
    """
    if entry.get("proxy") is True and entry.get("openai_addr_unverified") is True:
        return pc.WARN_OPENAI_UNVERIFIED
    return None


def _local_conflict_response(bot_id: str):
    """local 层命中 provider 键 → 结构化 409 的 (字典, 409)；未命中 → **None**（作前置守卫用）。

    ``keys`` 直接取自 ``local_provider_keys()``（数据驱动），**不解析** provider_config
    返回的文案——文案一改就会静默降级成 400、连 UI 要用的键名列表一起丢。
    """
    keys = pc.local_provider_keys(bot_id)
    if not keys:
        return None
    msg = ("%s 的 settings.local.json 里存在 provider 键（local 层优先于项目层），"
           "会覆盖面板写入，请先手工清理" % bot_id)
    return {"error": msg, "code": "local_conflict", "keys": keys}, 409


@provider_bp.errorhandler(pc.ProvidersLockTimeout)
def _providers_lock_timeout(_e):
    """跨进程文件锁等不到（批1-A）：结构化 409 + 契约文案，不落进 500 兜底。"""
    return jsonify({"error": "别的操作正在改供应商，请稍后再试",
                    "code": "provider_locked"}), 409


# ── R1 页面 ────────────────────────────────────────────────────────────────

@provider_bp.route("/provider")
def provider_page():
    return render_template("provider.html")


# ── R2 概览 ────────────────────────────────────────────────────────────────

@provider_bp.route("/api/providers", methods=["GET"])
def api_providers():
    try:
        data = pc.load_providers()
    except pc.ProvidersCorrupt:
        return _err("providers.json 解析失败", 500)

    providers = [pc._public_entry(entry) for entry in data.get("providers", [])]

    bindings = {}
    for bot_id in (data.get("bindings") or {}):
        bindings[bot_id] = pc.binding_status(bot_id)["binding"]

    glob_env = pc._read_env(pc.GLOBAL_SETTINGS_PATH)
    bots = pc.list_bots_status()
    known = {b["bot_id"] for b in bots}   # 发现名单：与页面的 bot 表同一份
    pending = pc.list_pending()
    dismissed = pc.load_dismissed()
    plan = _sync_plan(data, known)
    return jsonify({
        "providers": providers,
        "bindings": bindings,
        "bots": bots,
        "global": {
            "base_url": glob_env.get("ANTHROPIC_BASE_URL"),
            "model": glob_env.get("ANTHROPIC_MODEL"),
            "has_key": bool(glob_env.get("ANTHROPIC_AUTH_TOKEN") or glob_env.get("ANTHROPIC_API_KEY")),
        },
        "pending": [{"bot_id": e["bot_id"], "pending_since": e["pending_since"],
                     "fail_count": e["fail_count"], "next_retry_at": e["next_retry_at"],
                     "halted": e["halted"], "last_error": e["last_error"]}
                    for e in pending],
        # 以下四键是页面专用的"算好的结论"；上面五键的形状被既有契约锁定，不往里加键。
        "notices": [{"id": nid, "bot_id": e["bot_id"],
                     "kind": "halted" if e["halted"] else "pending",
                     "fail_count": e["fail_count"], "last_error": e["last_error"],
                     "pending_since": e["pending_since"]}
                    for e, nid in _live_notices(pending, known)
                    if dismissed.get(e["bot_id"]) != nid],
        "provider_details": _provider_details(data),
        "sync_plan": {"targets": [bot_id for bot_id, _, _, reason in plan if reason is None],
                      "skipped": [{"bot_id": bot_id, "reason": reason}
                                  for bot_id, _, _, reason in plan if reason is not None]},
        "converger": {"running": _converger_running()},
    })


def _live_notices(pending: list, known: set) -> list:
    """此刻应有的提示（**未考虑**是否已被关闭）：待重启列表里属于发现名单的项，各带标识。

    不属于发现名单的 bot 在页面的 bot 表里找不到，提示条让用户"点它的重启"就无从下手，
    所以不为它出提示（``pending`` 仍原样给全量，收敛器读的也是全量）。"""
    return [(e, pc.notice_id(e)) for e in pending if e["bot_id"] in known]


def _provider_details(data: dict) -> dict:
    """每条 provider 的补充信息：直连时实际写给 bot 的地址 + 逐模型明细。键 = provider 编号。"""
    from scripts.derive_endpoints import direct_anthropic_url
    out = {}
    for entry in data.get("providers", []):
        provider_id = entry.get("id")
        if not isinstance(provider_id, str):   # 手改坏的条目：不可作键，跳过而不是让整页 500
            continue
        base_url = entry.get("base_url")
        out[provider_id] = {
            "direct_base_url": direct_anthropic_url(base_url) if isinstance(base_url, str) else None,
            "models": pc.model_details(entry, data),
        }
    return out


_SKIP_PROVIDER_MISSING = "provider_missing"
_SKIP_BOT_UNKNOWN = "bot_unknown"
_SKIP_BOT_STOPPED = "bot_stopped"


def _sync_plan(data: dict, known: set) -> list:
    """「同步全部 bot」此刻会处理谁：对清单 ``bindings`` 逐只判定，命中第一条即定，顺序同清单。

    返回 ``[(bot_id, binding, entry, 跳过原因)]``，跳过原因为 None 表示会重写它的配置。
    概览的 ``sync_plan`` 与 ``api_sync_all`` 的实际动作出自这一处：页面说"会重写谁"和接口
    "实际重写谁"不会各说各话。"""
    providers = data.get("providers", [])
    plan = []
    for bot_id, binding in (data.get("bindings") or {}).items():
        entry = None
        if isinstance(binding, dict):
            entry = next((p for p in providers if p.get("id") == binding.get("provider_id")), None)
        if entry is None:
            reason = _SKIP_PROVIDER_MISSING
        elif bot_id not in known:
            reason = _SKIP_BOT_UNKNOWN
        elif _stopped(bot_id):
            reason = _SKIP_BOT_STOPPED
        else:
            reason = None
        plan.append((bot_id, binding, entry, reason))
    return plan


def _converger_running() -> bool:
    """本进程的自动重启此刻是否开着：看线程表，不看环境变量（与启动摘要行同一判据）。"""
    return any(t.name == "provider-converger" for t in threading.enumerate())


# ── 关闭提示条（INTERFACE-provider-ux §3）──────────────────────────────────

_NOTICE_FIELD_MAX = 200


@provider_bp.route("/api/notices/dismiss", methods=["POST"])
def api_dismiss_notice():
    """关闭一条提示：只记一笔"已关闭"，不改清单、不改任何 bot 的配置、不执行外部命令。

    标识必须等于该 bot **此刻**应有的提示标识才记：页面拿着旧标识来关（状态已经变了）
    不该把新提示一起关掉。"""
    pc._require_writable()   # 只读接缝形态：先于一切读取回 503
    body = _json_body()
    if body is None:
        return _err("请求体必须是 JSON 对象")
    bot_id, nid = body.get("bot_id"), body.get("id")
    for value in (bot_id, nid):
        if not isinstance(value, str) or not value or len(value) > _NOTICE_FIELD_MAX:
            return _err("bot_id 与 id 必须是非空字符串")
    # list_pending 对损坏的清单按空处理 → 落到"没有可关闭的提示"，不写任何文件
    live = dict((e["bot_id"], live_id) for e, live_id in _live_notices(pc.list_pending(), _bot_ids()))
    if live.get(bot_id) != nid:
        return jsonify({"ok": True, "dismissed": False})
    pc.dismiss_notice(bot_id, nid)
    return jsonify({"ok": True, "dismissed": True})


# ── 朋友圈模型（全部 bot 共用）：aux_llm ───────────────────────────────────

def _aux_err(msg, code):
    """aux 接口的错误体（INTERFACE-aux-llm-provider §9）：``{ok:false,error}`` + 4xx。"""
    return jsonify({"ok": False, "error": msg}), code


def _aux_models_of(data: dict, provider_id: str) -> list:
    entry = next((p for p in (data.get("providers") or [])
                  if isinstance(p, dict) and p.get("id") == provider_id), None)
    return pc._model_ids(entry) if entry else []


@provider_bp.route("/api/aux-llm", methods=["GET"])
def api_aux_llm_get():
    """回显当前 aux_llm 设置 + 供应商清单（脱敏，无密钥）+ 当前供应商的模型列表。"""
    try:
        data = pc.load_providers()
    except pc.ProvidersCorrupt:
        return _aux_err("providers.json 解析失败", 500)
    pid, model = pc.read_aux_llm()
    providers = [{"id": p.get("id"), "name": p.get("name")}
                 for p in (data.get("providers") or []) if isinstance(p, dict)]
    return jsonify({
        "provider_id": pid,
        "model": model,
        "providers": providers,
        "models": _aux_models_of(data, pid),
    })


@provider_bp.route("/api/aux-llm", methods=["POST"])
def api_aux_llm_set():
    """保存 aux_llm；``provider_id=""`` 恢复默认；供应商/模型不存在 → 4xx。"""
    body = _json_body()
    if body is None:
        return _aux_err("请求体必须是 JSON 对象", 400)
    pid = body.get("provider_id")
    pid = "" if pid is None else pid
    if not isinstance(pid, str):
        return _aux_err("provider_id 必须是字符串", 400)
    model = body.get("model")
    model = "" if model is None else model
    if not isinstance(model, str):
        return _aux_err("model 必须是字符串", 400)

    if pid == "":
        pc.set_global_aux_llm("", "")
        return jsonify({"ok": True})
    try:
        data = pc.load_providers()
    except pc.ProvidersCorrupt:
        return _aux_err("providers.json 解析失败", 500)
    entry = next((p for p in (data.get("providers") or [])
                  if isinstance(p, dict) and p.get("id") == pid), None)
    if entry is None:
        return _aux_err("供应商不存在", 404)
    if model not in pc._model_ids(entry):
        return _aux_err("模型不存在", 400)
    pc.set_global_aux_llm(pid, model)
    return jsonify({"ok": True})


# ── R3 / R4 / R5 provider 增删改 ───────────────────────────────────────────

@provider_bp.route("/api/providers", methods=["POST"])
def api_create_provider():
    body = _json_body()
    if body is None:
        return _err("请求体必须是 JSON 对象")
    entry, err = pc.upsert_provider(body)
    if err:
        return _err(err)
    return jsonify(_with_warning({"ok": True, "provider": entry})), 201


@provider_bp.route("/api/providers/<provider_id>", methods=["PUT"])
def api_update_provider(provider_id):
    body = _json_body()
    if body is None:
        return _err("请求体必须是 JSON 对象")
    try:
        entry, err = pc.upsert_provider(body, provider_id)
    except pc.ProviderConflict as e:   # 9.6：并发改动 / 统一绑定守卫，零写入
        out = {"error": str(e)}
        if e.bound_bots is not None:
            out["bound_bots"] = e.bound_bots
            out["code"] = "bound"
        return jsonify(out), 409
    if err:
        return _err(err, 404 if err == "provider not found" else 400)
    return jsonify(_with_warning({"provider": entry}))


@provider_bp.route("/api/providers/<provider_id>", methods=["DELETE"])
def api_delete_provider(provider_id):
    ok, err = pc.delete_provider(provider_id)   # delete_provider 内部已触发重建（§2.1）
    if not ok:
        if err == "provider not found":
            return _err(err, 404)
        return jsonify({"error": err, "code": "bound",
                        "bound_bots": pc.bound_bots(provider_id)}), 409
    return jsonify({"ok": True, "id": provider_id})


# ── R6 拉取模型 ────────────────────────────────────────────────────────────

def _bound_conflict(e):
    """统一绑定守卫 409 的响应形状（9.7.1，与 PUT 一致）：``{"error":…,"code":"bound"[,"bound_bots":[…]]}``。"""
    out = {"error": str(e), "code": "bound"}
    if e.bound_bots is not None:
        out["bound_bots"] = e.bound_bots
    return jsonify(out), 409


def _r6_name_collision(entry: dict, models: list, endpoints) -> bool:
    """按拉到的模型算 N_p，与其它 proxy=true provider 的注册名集合求交；只读不写、不发探针（review #2）。"""
    mine = pc._registered_names(dict(entry, models=models))   # 不跑判定链：避免每个未判定模型被探针两次
    try:
        others = pc.load_providers().get("providers") or []
    except pc.ProvidersCorrupt:
        return False
    return any(isinstance(o, dict) and o.get("proxy") is True and o.get("id") != entry.get("id")
               and mine & pc._registered_names(o) for o in others)


@provider_bp.route("/api/providers/models", methods=["POST"])
def api_list_models():
    body = _json_body()
    if body is None:
        return _err("请求体必须是 JSON 对象")

    provider_id = body.get("provider_id")
    has_pair = ("base_url" in body) or ("api_key" in body)
    if provider_id and has_pair:
        return _err("provider_id 与 base_url/api_key 只能给一组")

    if provider_id:
        try:
            data = pc.load_providers()
        except pc.ProvidersCorrupt:
            return _err("providers.json 解析失败", 500)
        entry = next((p for p in data.get("providers", []) if p.get("id") == provider_id), None)
        if entry is None:
            return _err("provider not found", 404)
        api_key = entry.get("api_key")
        if not api_key:
            return _err("该 provider 未存 api_key")
        base_url = entry.get("base_url")
        # 显式 OpenAI 侧地址同样要过 http/https 校验，不合格视为未设（回退会把同一把 key 发过去）
        openai_base_url = entry.get("openai_base_url")
        if not pc._valid_base_url(openai_base_url):
            openai_base_url = ""
    else:
        openai_base_url = ""
        base_url = body.get("base_url")
        api_key = body.get("api_key")
        # base_url 判据与 R3/R4 统一到 provider_config._valid_base_url（单一来源）
        if not pc._valid_base_url(base_url):
            return _err("base_url 必须是 http/https 开头的地址")
        if not api_key:
            return _err("api_key required")

    clear_fetch_contexts()   # 旁路先清零：本次没抽到就不会写进上一轮的残留
    models, error = _fetch_models(base_url, api_key, openai_base_url)
    if error:
        # 失败时响应只含三个既有键（INTERFACE-provider-protocol-level §2.4：删除顶层 protocols 空壳）。
        return jsonify({"ok": False, "models": [], "error": error})

    # E-25.3「看一眼不改配置」：带 provider_id 的**只读查询**（query_only=true，面板点开
    # 模型框走的就是它）只拉取并回 candidates —— 不覆盖 provider 的模型清单、不重建代理
    # 配置、也不跑写前守卫（守卫是写给"即将发生的写"的）。缺省（无此键）= 显式拉取，
    # 行为与此前一字不差：守卫 → 写回 → 重建（F4）。
    if provider_id and models and body.get("query_only") is not True:
        # 9.7.1 统一绑定守卫：写回 models **之前**判定（proxy=true 时），命中即 409 零写入。
        # 复用 provider_config._binding_guard。判定链已随模型级协议一并移除，此处不再跑探针。
        # 批1-C④：判定列表用**写回后的最终列表（并集）**，与 store_models 同口径，
        # 消除「守卫按拉到的、写回按并集」的误 409。
        final = pc.merge_models(entry, models)
        try:
            pc._binding_guard(provider_id, dict(entry), dict(entry, models=final), data)
        except pc.ProviderConflict as e:
            return _bound_conflict(e)
        if entry.get("proxy") is True and _r6_name_collision(entry, final, None):
            # 9.7（F 组）：拉到的模型使 N_p 与另一 proxy provider 相交 → 零写入、不重建
            return jsonify({"ok": False, "models": [],
                            "error": "客户端名或裸名在多个代理 provider 间重复，请先关闭其中一个"})
        pc.store_models(provider_id, models, take_fetch_contexts())   # 读改写在 provider_config 锁内完成
        # R6 成功即触发一次配置重建（F4）：拉到的新模型立即在实例里可路由。
        snap = _proxy_snapshot()
        if snap is not None:
            pc._sync_after_write(snap)
    return jsonify({"ok": True, "models": models, "error": None})


def _models_url(base_url: str) -> str:
    """``<base_url>`` + ``/v1/models``（已以 ``/v1`` 结尾则只加 ``/models``）。

    **先剥离 base_url 的 fragment 与 query**：原样拼接时 ``?``/``#`` 会把后缀吞掉——
    ``http://127.0.0.1:6379/x#`` 实际发出的是 ``GET /x``，请求路径完全由调用方控制
    （绕过"只访问用户填的 provider"这一层的路径约束）。
    """
    b = base_url.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    return b + "/models" if b.endswith("/v1") else b + "/v1/models"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None  # 3xx 视为失败（不跟随重定向）


def _fetch_models(base_url: str, api_key: str, openai_base_url: str = ""):
    """拉上游 ``/v1/models``。返回 ``(models, error)``；形状与既有完全一致（增强三项
    中的分页/上下文抽取走 ``_fetch_models_ctx`` 与旁路，R6 响应与元素形状零变化）。

    首次得到 404 且 OpenAI 侧地址（``effective_endpoints``）与 base_url 不同时，再用它回退请求一次
    （如 ``…/anthropic`` 下没有模型列表、根路径下才有）；其他错误不回退。两次都失败则拼接两次文案。
    """
    models, error, _ = _fetch_models_ctx(base_url, api_key, openai_base_url)
    return models, error


def _fetch_models_ctx(base_url: str, api_key: str, openai_base_url: str = ""):
    """同 ``_fetch_models``，另把当次抽到的上下文（id → int）一并返回（§5-2 旁路）。

    旁路同时落在**线程本地**（``take_fetch_contexts``），供只认两键返回值的 R6 视图取用。
    """
    clear_fetch_contexts()
    models, error = _fetch_models_once(base_url, api_key)
    if error != _status_error(404):
        return models, error, (current_fetch_contexts() if not error else {})
    from scripts.derive_endpoints import effective_endpoints
    openai_url = effective_endpoints(base_url, "", openai_base_url)[1]
    if openai_url == base_url.rstrip("/"):
        return models, error, {}
    models2, error2 = _fetch_models_once(openai_url, api_key)
    if error2:
        return None, "%s；改用 OpenAI 端点也失败：%s" % (error, error2), {}
    return models2, None, current_fetch_contexts()


# 上下文抽取（§5-2）：字段序固定，取区间内**首个**整数 → meta.guessed_context
_CONTEXT_FIELDS = ("max_input_tokens", "context_length", "context_window",
                   "max_context_length", "max_model_len", "input_token_limit")
_CONTEXT_MIN, _CONTEXT_MAX = 1024, 100000000
_CONTEXT_TEXT_MAX = 32   # 改上下文窗口时字符串写法的长度上限（按原样计，含首尾空白）
_MODELS_MAX = 500   # 与 provider_config 写回上限同口径（拉满即截断，不标注）

# 上下文旁路（批1-C）：线程本地——Flask 可能是多线程；绝不动 models[] 元素（恒 {id,label}）。
_FETCH_STATE = threading.local()


def clear_fetch_contexts() -> None:
    _FETCH_STATE.contexts = {}


def _stash_contexts(contexts) -> None:
    _FETCH_STATE.contexts = contexts if isinstance(contexts, dict) else {}


def current_fetch_contexts() -> dict:
    contexts = getattr(_FETCH_STATE, "contexts", None)
    return contexts if isinstance(contexts, dict) else {}


def take_fetch_contexts() -> dict:
    """取走最近一次拉取抽到的上下文（取后清零）。"""
    contexts = current_fetch_contexts()
    _FETCH_STATE.contexts = {}
    return contexts


def _extract_context(item: dict):
    """按字段序取第一个 1024–100000000 的整数；都不合格 → None（不凭空记）。"""
    for field in _CONTEXT_FIELDS:
        value = item.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if _CONTEXT_MIN <= value <= _CONTEXT_MAX:
            return value
    return None


def _fetch_models_once(base_url: str, api_key: str):
    """向 ``base_url`` 发一次 ``/v1/models`` 请求并解析。返回 ``(models, error)``。

    ``models`` 条目**对外恒为 ``{id, label}`` 两键**（INTERFACE §5-3 全等断言）；
    抽到的上下文走线程本地旁路（见 ``_fetch_models_once_ctx``），不进元素。
    """
    models, error, contexts = _fetch_models_once_ctx(base_url, api_key)
    _stash_contexts(contexts)
    return models, error


def _fetch_models_once_ctx(base_url: str, api_key: str):
    """一次 ``/v1/models`` 拉取（含 Anthropic 分页）。返回 ``(models, error, contexts)``。

    分页（§5-1）：认顶层 ``has_more``+``last_id``，``&after_id=<last_id>`` 续拉，
    **总上限 500**（到顶即停，不再翻下一页，与现状"静默截断"一致）；多页合并**去重保序**。
    翻满 ``_MODELS_MAX_PAGES`` 页或过了 ``_MODELS_DEADLINE`` 秒上游仍说还有下一页 → 按
    "上游分页异常"报错（不返回半截结果）。
    """
    # SSRF 面收敛：本路由是整个面板唯一的新增外联面，唯一访问控制是登录门（/api/* 未登录 401）。
    # 手段：只允许 http/https、拒 userinfo、不跟随重定向（3xx 判失败）、8 秒超时、响应 ≤1MiB、
    # 错误只回状态码类别（不回上游 body）。**不做内网 IP 黑名单** —— 本机/LAN 上的 provider
    # 是合法用法（NO_PROXY=127.0.0.1,localhost 是既有事实），黑名单会把正常用法一起砍掉。
    # 同时禁用环境代理（ProxyHandler({})），避免请求被 http_proxy 静默劫持到第三方。
    url = _models_url(base_url)
    from scripts import proxy_upstream   # 与探针同一浏览器式请求头基底（9.7，I 组）
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.ProxyHandler({}))
    out, seen, contexts = [], set(), {}
    after = None
    paging_error = "上游分页异常（翻了 %d 页或超过 %d 秒仍未结束）" % (_MODELS_MAX_PAGES, _MODELS_DEADLINE)
    deadline = time.monotonic() + _MODELS_DEADLINE
    for _ in range(_MODELS_MAX_PAGES):
        if time.monotonic() >= deadline:
            return (None, paging_error, {})
        try:
            page = url + ("?after_id=" + urllib.parse.quote(after, safe="") if after else "")
            req = urllib.request.Request(page, method="GET", headers=proxy_upstream.browser_headers({
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Authorization": "Bearer %s" % api_key,
            }))
            with opener.open(req, timeout=_MODELS_TIMEOUT) as resp:
                raw = resp.read(_MAX_BODY + 1)
        except urllib.error.HTTPError as e:
            return (None, _status_error(e.code), {})
        except (socket.timeout, TimeoutError):
            return (None, "超时（8 秒）", {})
        except urllib.error.URLError as e:
            msg = ("超时（8 秒）"
                   if isinstance(getattr(e, "reason", None), (socket.timeout, TimeoutError))
                   else "连接失败: %s" % type(getattr(e, "reason", e)).__name__)
            return (None, msg, {})
        except (http.client.HTTPException, OSError) as e:
            msg = "连接失败: %s" % type(e).__name__
            return (None, msg, {})
        except (ValueError, TypeError):
            # 请求组装不出来：地址含非 ASCII、密钥含换行或 latin-1 之外的字符、密钥不是字符串、
            # 上游给的翻页标识写不成 UTF-8——标准库在连接之前就抛，不属于上面任何一类。
            # 不带异常文字：里面是整个请求头的值，含明文密钥。
            return (None, _UNSENDABLE, {})
        if len(raw) > _MAX_BODY:
            return (None, "响应过大", {})
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return (None, "上游响应不是 JSON", {})
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            return (None, "上游响应不是 JSON", {})
        for it in items:
            if not isinstance(it, dict):
                continue
            mid = it.get("id")
            if not isinstance(mid, str) or not mid or mid in seen:
                continue
            seen.add(mid)
            ctx = _extract_context(it)
            if ctx is not None:
                contexts[mid] = ctx
            out.append({"id": mid, "label": it.get("display_name") or mid})
            if len(out) >= _MODELS_MAX:
                break
        if len(out) >= _MODELS_MAX:
            break                      # 到顶即停：不再翻下一页
        last_id = payload.get("last_id") if isinstance(payload, dict) else None
        if not (payload.get("has_more") and isinstance(last_id, str) and last_id and last_id != after):
            break                      # 没有下一页 / last_id 不前进（防死循环）
        after = last_id
    else:
        return (None, paging_error, {})   # 翻满页数上限，上游仍说还有下一页
    return out, None, contexts



def _status_error(code: int) -> str:
    if code == 401:
        return "上游返回 401（key 无效或无权限）"
    if code == 403:
        return "上游返回 403（无权限，或上游 CDN 拦截）"
    if code == 404:
        return "上游返回 404（该地址没有 /v1/models 端点）"
    return "上游返回 %s" % code


# ── 模型：候选（只读）/ 按勾选写回 / 单个删除 / 改上下文窗口（INTERFACE-provider-ux §4–§6）──
# 三个写接口都只做「过写闸 → 校验 → 调 provider_config 的模型级函数 → 映射错误码 → 级联」，
# 与 Telegram 菜单增删模型走的是同一组函数、同一条级联。模型名一律在请求体里（可含 / 与 :）。

def _coded(msg: str, status: int, code: str, **extra):
    """带 ``code`` 字段的错误响应（``_err`` 的第二个参数是状态码，放不了同名的 JSON 字段）。"""
    body = {"error": msg, "code": code}
    body.update(extra)
    return jsonify(body), status


def _model_in_use(in_use: list):
    """模型正被 bot 使用：409 + 名单。文案里带出每个模型与使用它的 bot，页面原样显示。"""
    detail = "；".join("%s（%s）" % (item["model"], "、".join(item["bots"])) for item in in_use)
    return _coded("模型正在被 bot 使用，不能移除：%s。请先给这些 bot 换模型" % detail,
                  409, "model_in_use", in_use=in_use)


def _provider_missing_response(provider_id: str):
    """模型级路由的前置：清单读得出且这条 provider 存在 → None，否则给出对应的错误响应。

    模型级函数的 ``missing`` 不区分「供应商不存在」与「模型不存在」，路由先自己确认前者。"""
    try:
        data = pc.load_providers()
    except pc.ProvidersCorrupt:
        return _err("providers.json 解析失败", 500)
    if pc._find(data, provider_id) is None:
        return _err("provider not found", 404)
    return None


@provider_bp.route("/api/providers/models/candidates", methods=["POST"])
def api_model_candidates():
    """多选对话框的候选：上游有哪些模型、清单里已有哪些、默认勾选谁。**零写入**。

    「默认勾选谁、哪些正被使用」在这里算好，页面只负责呈现。入参两种写法与既有拉取接口相同。"""
    body = _json_body()
    if body is None:
        return _err("请求体必须是 JSON 对象")
    provider_id = body.get("provider_id")
    if provider_id and (("base_url" in body) or ("api_key" in body)):
        return _err("provider_id 与 base_url/api_key 只能给一组")

    data, entry, openai_base_url = {}, None, ""
    if provider_id:
        try:
            data = pc.load_providers()
        except pc.ProvidersCorrupt:
            return _err("providers.json 解析失败", 500)
        entry = pc._find(data, provider_id)
        if entry is None:
            return _err("provider not found", 404)
        api_key = entry.get("api_key")
        if not api_key:
            return _err("该 provider 未存 api_key")
        base_url = entry.get("base_url")
        openai_base_url = entry.get("openai_base_url")
        if not pc._valid_base_url(openai_base_url):   # 与既有拉取同口径：不合格视为未设
            openai_base_url = ""
    else:
        base_url, api_key = body.get("base_url"), body.get("api_key")
        if not pc._valid_base_url(base_url):
            return _err("base_url 必须是 http/https 开头的地址")
        if not isinstance(api_key, str) or not api_key:   # 数组、数字之类：与缺失同样对待
            return _err("api_key required")

    models, error, contexts = _fetch_models_ctx(base_url, api_key, openai_base_url)
    clear_fetch_contexts()   # 线程本地的旁路只给写回形态用；这里用返回值，不留残留
    if error:
        return jsonify({"ok": False, "error": error, "candidates": []})

    listed = pc._entry_models_by_id(entry) if entry else {}
    manual = set(pc._meta_manual_models(entry)) if entry else set()
    contexts = contexts if isinstance(contexts, dict) else {}

    def candidate(model_id: str, label: str, upstream: bool) -> dict:
        context = contexts.get(model_id) if upstream else None
        return {"id": model_id, "label": label,
                "context": context if pc._valid_context_value(context) else None,
                "upstream": upstream, "listed": model_id in listed, "manual": model_id in manual,
                "used_by": pc._model_users(data, provider_id, model_id) if entry else [],
                "selected": model_id in listed}   # 已有的默认勾选，新出现的默认不勾

    fetched = pc._norm_model_items(models)[:_MODELS_MAX]   # 归一、去重保序、封顶
    candidates = [candidate(m["id"], m["label"], True) for m in fetched]
    seen = {m["id"] for m in fetched}
    candidates += [candidate(model_id, item["label"], False)
                   for model_id, item in listed.items() if model_id not in seen]
    return jsonify({"ok": True, "error": None, "provider_id": provider_id if entry else None,
                    "candidates": candidates, "base": pc._model_ids(entry) if entry else [],
                    "limit": _MODELS_MAX, "truncated": len(models or []) >= _MODELS_MAX})


_SET_MODELS_ERRORS = {
    "bad_models": ("models 必须是数组（不超过 500 项），元素是模型名或带 id 的对象", 400, "bad_models"),
    "bad_ctx": ("context 必须是 1024–100000000 的整数", 400, "bad_context"),
    "bad_base": ("base 必须是字符串数组", 400, "bad_base"),
    "changed": ("模型列表已被别处改动，请重新拉取", 409, "models_changed"),
    "name_dup": (pc._ADM_NAME_DUP, 409, "name_conflict"),
}


@provider_bp.route("/api/providers/<provider_id>/models", methods=["PUT"])
def api_set_models(provider_id):
    """对话框点「确定」：该 provider 的模型列表 = 提交的集合。"""
    pc._require_writable()   # 只读接缝形态：先于一切读取回 503
    body = _json_body()
    if body is None:
        return _err("请求体必须是 JSON 对象")
    result, err = pc.set_models(provider_id, body.get("models"), body.get("base"))
    if err == "in_use":
        return _model_in_use(result)
    if err == "corrupt":
        return _err("providers.json 解析失败", 500)
    if err == "missing":
        return _err("provider not found", 404)
    if err:
        return _coded(*_SET_MODELS_ERRORS[err])
    pc._cascade_provider_change(provider_id)
    return jsonify({"ok": True, "provider_id": provider_id, "models": result["models"],
                    "added": result["added"], "removed": result["removed"]})


def _model_arg(body: dict):
    model = body.get("model")
    return model if isinstance(model, str) and model else None


@provider_bp.route("/api/providers/<provider_id>/models/remove", methods=["POST"])
def api_remove_model(provider_id):
    pc._require_writable()
    body = _json_body()
    if body is None:
        return _err("请求体必须是 JSON 对象")
    model = _model_arg(body)
    if model is None:
        return _coded("model 必须是非空字符串", 400, "bad_model")
    missing = _provider_missing_response(provider_id)
    if missing is not None:
        return missing
    result, err = pc.del_model(provider_id, model)
    if err == "missing":
        return _coded("该供应商下没有这个模型", 404, "model_not_found")
    if err and err.startswith("bound:"):
        # 名单按清单重算（bot 名理论上可以含逗号，不去拆错误码）；清单恰在此刻变了才退回拆分
        bots = (pc._model_users(pc._load_providers_safe(), provider_id, model)
                or err[len("bound:"):].split(","))
        return _model_in_use([{"model": model, "bots": bots}])
    if err:
        return _err("providers.json 解析失败", 500)
    pc._cascade_provider_change(provider_id)
    return jsonify({"ok": True, "provider_id": provider_id, "model": model, "count": result["count"]})


@provider_bp.route("/api/providers/<provider_id>/models/context", methods=["POST"])
def api_set_model_context(provider_id):
    pc._require_writable()
    body = _json_body()
    if body is None:
        return _err("请求体必须是 JSON 对象")
    model = _model_arg(body)
    if model is None:
        return _coded("model 必须是非空字符串", 400, "bad_model")
    # 必须在这里就判出合法整数：set_model_ctx 收到 None 不报错，会把 null 写进清单
    raw = body.get("context")
    if isinstance(raw, str):
        # 先限长再解析：合法写法远短于此，超长串不必交给数值转换
        context = pc.parse_context(raw) if len(raw) <= _CONTEXT_TEXT_MAX else None
    else:
        context = raw if pc._valid_context_value(raw) else None
    if context is None:
        return _coded("上下文窗口写法不对：填整数，或 200k、1m 这样的写法，范围 1024–100000000",
                      400, "bad_context")
    missing = _provider_missing_response(provider_id)
    if missing is not None:
        return missing
    _, err = pc.set_model_ctx(provider_id, model, context)
    if err == "missing":
        return _coded("该供应商下没有这个模型", 404, "model_not_found")
    if err:
        return _err("providers.json 解析失败", 500)
    pc._cascade_provider_change(provider_id)
    return jsonify({"ok": True, "provider_id": provider_id, "model": model, "context": context})



def _restart_and_finalize(bot_id: str, ok_payload: dict, queued_payload: dict,
                          previous_binding, settings_backup: str | None):
    res = _restart_worker(bot_id)
    status = res["status"]
    if status in ("ok", "queued", "spawn_failed", "unrestartable"):
        _settle(bot_id, res)  # 唯一记账入口（I-3）；R8 已删 binding 时全是无操作
    if status == "ok":
        ok_payload["effective"] = pc.effective_provider(bot_id)
        ok_payload["restart"] = res
        return ok_payload, 200
    if status in ("queued", "spawn_failed", "unrestartable"):
        queued_payload["restart"] = res
        queued_payload["effective"] = pc.effective_provider(bot_id)
        if status == "unrestartable":  # 写盘已成功但自动重启已停止：不再谎称已排队，标明原因
            queued_payload["queued"] = False
            queued_payload["halted"] = True
            queued_payload["halt_reason"] = res.get("detail")
        return queued_payload, 202
    if status == "stopped":
        _rollback(bot_id, previous_binding, settings_backup)
        return {"error": "bot 已停用", "code": "stopped"}, 409
    return _err_dict("unknown bot %s" % bot_id, 400)


def _rollback(bot_id: str, previous_binding, settings_backup: str | None) -> None:
    pc.rollback_binding(bot_id, previous_binding)
    path = pc.bot_settings_path(bot_id)
    if settings_backup and path and os.path.exists(settings_backup):
        try:
            os.replace(settings_backup, path)
        except OSError:
            pass


def _sync_proxy_after_binding():
    """绑定/重置成功后的配置重建副作用（§2.1）；返回 ``provider_proxy.sync`` 的结果（读不出清单 → None）。

    **no-op 由 ``provider_proxy.sync`` 自己保证**（无 proxy provider 时不写盘、不探测、
    不 kickstart、不创建运行目录），故无条件调用安全。E-17：把结果带回响应，
    由调用方（绑定走代理的 provider）据实报失败；读不出清单时无条件调用不安全，返回 None。
    """
    try:
        snap = pc.load_providers()
    except pc.ProvidersCorrupt:
        return None
    return pc._sync_after_write(snap)


# 下面三个函数是绑定 / 重置 / 重启的主体，**不依赖请求对象**，返回 (结果字典, HTTP 状态码)：
# 供应商页的三个路由取完请求体后调用它们再 jsonify；总览页的 claude 适配器（adapters/claude_local.py）
# 也调用它们——同一段校验、代理同步、当场重启、失败回滚、待重启记账，不另抄一份。

def _kept_while_stopped(bot_id: str, queued_payload: dict):
    """allow_stopped 时写完仍停着：只写不重启（INTERFACE-bot-lifecycle 3.0 第 15 条，管理台用）。worker 不在，
    下次启用时首次拉起就读到新设置，所以清掉待重启（收敛器对停用的 bot 也是这样）。不是停用的 → None，照常重启。"""
    if not _stopped(bot_id):
        return None
    pc.clear_pending(bot_id)
    return dict(queued_payload, stopped=True), 202


def bind_bot(bot_id: str, body, allow_stopped: bool = False) -> tuple:
    """换供应商主体。``body`` 是已解析的请求体（非 dict 视作"不是 JSON 对象"）。
    ``allow_stopped``：停用中的 bot 也写绑定、但不重启（回 202 带 ``stopped: true``）；供应商页不传，停用照旧 409。"""
    # 多错误命中顺序（Y2-6）：unknown bot → stopped → provider not found → 字段校验
    if bot_id not in _bot_ids():
        return _err_dict("unknown bot %s" % bot_id, 404)
    if _stopped(bot_id) and not allow_stopped:
        return {"error": "bot 已停用", "code": "stopped"}, 409

    if not isinstance(body, dict):
        return _err_dict("请求体必须是 JSON 对象")

    provider_id = body.get("provider_id")
    model = body.get("model")
    try:
        data = pc.load_providers()
    except pc.ProvidersCorrupt:
        return _err_dict("providers.json 解析失败", 500)
    entry = next((p for p in data.get("providers", []) if p.get("id") == provider_id), None)
    if entry is None:
        return _err_dict("provider not found", 404)
    if not isinstance(model, str) or not (1 <= len(model) <= 200):
        return _err_dict("model 必须是长度 1..200 的字符串")
    # §2.1 末：proxy=true 时客户端名为 <pid>/<model>，绑定前校验拼接后长度（不截断、不改名）。
    # proxy=false 时行为与今天完全一致（只校验 model 本身 1..200）。
    if entry.get("proxy") is True and len(provider_id) + 1 + len(model) > 200:
        return _err_dict("model 拼接 provider 前缀后超过 200 字符")

    resp = _local_conflict_response(bot_id)
    if resp:
        return resp

    previous_binding = pc.get_binding_raw(bot_id)
    result, err = pc.set_bot_provider(bot_id, entry, model)
    if err:
        # 纵深防御：set 内部的 local 复核在前置检查之后才命中（竞态）时也走 409 分支
        resp = _local_conflict_response(bot_id)
        if resp:
            return resp
        return _err_dict(err, 400)

    sync_result = _sync_proxy_after_binding()   # §2.1 绑定成功副作用：触发配置重建
    if (entry.get("proxy") is True and isinstance(sync_result, dict)
            and sync_result.get("ok") is False):
        # E-17：绑定走代理的 provider，代理同步失败 → 如实报失败（不重启、不报 applied）。
        # 写入本身已成功、pending_restart 已登记（收敛器后续重试）；但实例里还没有该 provider，
        # 报成功会让用户以为切换完成。
        return {"ok": False, "applied": False, "queued": False, "bot_id": bot_id,
                "error": sync_result.get("error") or "代理配置同步失败",
                "restart": {}}, 200
    warning = _bind_warning(entry)   # E-15：该 provider 的 openai 地址未验证 → 绑定响应带提示
    ok_payload = {"ok": True, "applied": True, "queued": False, "bot_id": bot_id,
                  "env_written": result["env_written"]}
    queued_payload = {"ok": True, "applied": False, "queued": True, "bot_id": bot_id}
    if warning:
        ok_payload["warning"] = warning
        queued_payload["warning"] = warning
    kept = _kept_while_stopped(bot_id, queued_payload) if allow_stopped else None
    if kept:
        return kept
    return _restart_and_finalize(bot_id, ok_payload, queued_payload,
                                 previous_binding, result.get("backup_path"))


def reset_bot(bot_id: str, allow_stopped: bool = False) -> tuple:
    """恢复默认主体（清掉单独绑定）。``allow_stopped`` 同 bind_bot。"""
    if bot_id not in _bot_ids():
        return _err_dict("unknown bot %s" % bot_id, 404)
    if _stopped(bot_id) and not allow_stopped:
        return {"error": "bot 已停用", "code": "stopped"}, 409
    resp = _local_conflict_response(bot_id)
    if resp:
        return resp

    previous_binding = pc.get_binding_raw(bot_id)
    ok, err = pc.clear_bot_provider(bot_id)
    if not ok:
        resp = _local_conflict_response(bot_id)
        if resp:
            return resp
        return _err_dict(err, 400)

    _sync_proxy_after_binding()   # §2.1 副作用：重置成功后同样触发配置重建
    queued_payload = {"ok": True, "applied": False, "queued": True, "bot_id": bot_id}
    kept = _kept_while_stopped(bot_id, queued_payload) if allow_stopped else None
    if kept:
        return kept
    return _restart_and_finalize(
        bot_id,
        {"ok": True, "applied": True, "queued": False, "bot_id": bot_id, "env_removed": list(pc.ALLOWED_ENV_KEYS)},
        queued_payload, previous_binding, None)


def restart_bot(bot_id: str, force: bool) -> tuple:
    """手动重启主体。"""
    if bot_id not in _bot_ids():
        return {"error": "unknown bot %s" % bot_id, "code": "unknown_bot"}, 404
    if _stopped(bot_id):
        return {"error": "bot 已停用", "code": "stopped"}, 409
    # 待重启且已停止的项：手动重启先解除停止（M4）；结果交 settle 记账（不在待重启的项全是无操作）
    binding = pc.get_binding_raw(bot_id) or {}
    if binding.get("pending_restart") and binding.get("halted") is True:
        pc.resume_restart(bot_id)
    res = _restart_worker(bot_id, force)
    _settle(bot_id, res)
    # E-24（裁定：以旧锁定契约为准）：状态码恒 200，成败由前端读 body.ok 判定并显示失败原因。
    return {"ok": res["status"] == "ok", "restart": res}, 200


@provider_bp.route("/api/bots/<bot_id>/provider", methods=["POST"])
def api_bind_bot(bot_id):
    payload, status = bind_bot(bot_id, _json_body())
    return jsonify(payload), status


@provider_bp.route("/api/bots/<bot_id>/provider/reset", methods=["POST"])
def api_reset_bot(bot_id):
    payload, status = reset_bot(bot_id)
    return jsonify(payload), status


# ── 思考强度（INTERFACE §2.7.4）────────────────────────────────────────────

def _current_effort(bot_id: str):
    """当前档位：binding 记录优先，退化读 settings 顶层 effortLevel；非法/缺失 → None。"""
    bd = pc.get_binding_raw(bot_id) or {}
    effort = bd.get("effort")
    if effort in pc.EFFORT_LEVELS:
        return effort
    doc = pc._read_settings_doc(pc.bot_settings_path(bot_id))
    value = doc.get(pc.EFFORT_KEY) if isinstance(doc, dict) else None
    return value if value in pc.EFFORT_LEVELS else None


@provider_bp.route("/api/bots/<bot_id>/effort", methods=["GET", "POST"])
def api_bot_effort(bot_id):
    if bot_id not in _bot_ids():
        return _err_coded("unknown bot %s" % bot_id, 404, "unknown_bot")
    if request.method == "GET":
        return jsonify({"ok": True, "bot_id": bot_id, "effort": _current_effort(bot_id),
                        "levels": list(pc.EFFORT_LEVELS)})

    body = _json_body()
    effort = body.get("effort") if isinstance(body, dict) else None
    if not isinstance(effort, str) or effort not in pc.EFFORT_LEVELS:
        return _err_coded("思考强度只支持 low、medium、high、xhigh、max", 400, "invalid")
    if _stopped(bot_id):
        return _err_coded("bot 已停用", 409, "stopped")
    conflict = _local_conflict_response(bot_id)
    if conflict:
        return jsonify(conflict[0]), conflict[1]

    ok, err = pc.set_bot_effort(bot_id, effort)
    if not ok:
        return _err_coded(err or "思考强度没改成", 400, "invalid")

    res = _restart_worker(bot_id)
    _settle(bot_id, res)
    applied = res.get("status") == "ok"
    payload = {"ok": True, "bot_id": bot_id, "effort": effort,
               "applied": applied, "queued": not applied,
               "restart": res, "effective": pc.effective_provider(bot_id)}
    return jsonify(payload), 200 if applied else 202


# ── 总览页的供应商动作：只转适配器，不直写 settings ───────────────────────

@provider_bp.route("/api/console/bots/<backend>/<bot_id>/provider", methods=["POST"])
def api_console_bot_provider(backend, bot_id):
    from adapters import registry
    from adapters.base import AdapterError, AdapterUnavailable

    body = _json_body()
    if not isinstance(body, dict) or not any(
            k in body for k in ("effort", "provider_id", "reset")):
        return _err_coded("请求体至少包含 effort / provider_id / reset 之一", 422, "invalid")
    # 枚举校验先于任何后端解析：后端不可用时也必须先回 422 invalid（INTERFACE §2.7.4）。
    if "effort" in body and (not isinstance(body.get("effort"), str)
                             or body.get("effort") not in pc.EFFORT_LEVELS):
        return _err_coded("思考强度只支持 low、medium、high、xhigh、max", 422, "invalid")
    try:
        out = registry.adapter(backend).set_provider(bot_id, body)
    except AdapterError as e:
        return jsonify({"error": e.message, "code": e.code}), e.http_status
    except AdapterUnavailable as e:
        http_status, code = registry.unavailable_http(e.status)
        return jsonify({"error": e.message, "code": code}), http_status
    return jsonify({"ok": True, "backend": backend, "bot_id": bot_id, **(out or {})})


# ── R9 手动重启 ────────────────────────────────────────────────────────────

@provider_bp.route("/api/bots/<bot_id>/restart", methods=["POST"])
def api_restart_bot(bot_id):
    body = request.get_json(silent=True)
    force = bool(body.get("force")) if isinstance(body, dict) else False
    payload, status = restart_bot(bot_id, force)
    return jsonify(payload), status


# ── R10 一键重下发 ─────────────────────────────────────────────────────────

@provider_bp.route("/api/providers/sync_all", methods=["POST"])
def api_sync_all():
    try:
        data = pc.load_providers()
    except pc.ProvidersCorrupt:
        return _err("providers.json 解析失败", 500)
    pc.ensure_instance_key()   # R6 汇口第 0 步：2xx 写请求后清单必有 instance_key

    results = []
    # 跳过判定与概览的 sync_plan 同出一处（_sync_plan）；没有绑定时不必去发现 bot
    known = _bot_ids() if data.get("bindings") else set()
    for bot_id, binding, entry, skip_reason in _sync_plan(data, known):
        if skip_reason is not None:
            results.append({"bot_id": bot_id, "ok": False, "key_masked": "",
                            "in_sync": False, "restart": {},
                            "error": ("provider not found" if skip_reason == _SKIP_PROVIDER_MISSING
                                      else "bot 不可用")})
            continue
        result, err = pc.set_bot_provider(bot_id, entry, binding.get("model"))
        if err:
            results.append({"bot_id": bot_id, "ok": False, "key_masked": "",
                            "in_sync": False, "restart": {}, "error": err})
            continue
        results.append({
            "bot_id": bot_id, "ok": True,
            "key_masked": pc._mask(entry.get("api_key") or ""),
            # 9.9：写入后用与 effective 同一判据实算（本地层覆盖等情形为 false），不写死
            "in_sync": bool(pc.effective_provider(bot_id).get("in_sync")), "restart": {},
        })
    return jsonify({"ok": True, "queued": len(results), "results": results}), 202


# ── GET /api/proxy、POST /api/proxy/sync ──

@provider_bp.record_once
def _disable_json_sort(state):
    """关掉本 app 的 JSON 键排序（INTERFACE §1.1 要求 providers[] 条目**恰好 14 键且顺序即此**）。

    Flask 3.x 默认 ``app.json.sort_keys = True``，会把键按字典序重排（``id`` 跑到 ``name`` 后面），
    与契约顺序不符。本蓝图注册时统一关掉——比逐个响应手工拼串干净，也覆盖了所有既有路由。
    **不改 moments/web.py**（PLAN §3.2 明确不改），用蓝图的 ``record_once`` 在注册期拿到 app。
    """
    state.app.json.sort_keys = False


def _proxy_snapshot():
    """取一份 providers.json 快照给 sync（读失败 → None）。"""
    try:
        return pc.load_providers()
    except pc.ProvidersCorrupt:
        return None


@provider_bp.route("/api/proxy/restart", methods=["POST"])
def api_proxy_restart():
    import provider_proxy
    status, body = provider_proxy.restart(ensure_key=pc.ensure_instance_key)   # 忽略请求体（9.15）
    return jsonify(body), status


@provider_bp.route("/api/proxy/generation", methods=["GET"])
def api_proxy_generation():
    import provider_proxy
    return jsonify(provider_proxy.generation_info())   # 恒 200（9.15）


@provider_bp.route("/api/proxy", methods=["GET"])
def api_proxy_status():
    import provider_proxy
    return jsonify(provider_proxy.proxy_status(_proxy_snapshot()))


@provider_bp.route("/api/proxy/sync", methods=["POST"])
def api_proxy_sync():
    import provider_proxy
    body = request.get_json(silent=True)
    # 请求体必须是对象或省略（INTERFACE §2.5）。
    if body is not None and not isinstance(body, dict):
        return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
    pc.ensure_instance_key()   # R6 汇口第 0 步（先于取快照：sync 要用 key 生成 config）
    snap = _proxy_snapshot()
    result = provider_proxy.sync(snap, explicit=True)   # 显式 sync 等核验结果（截止 3T+11.2 s）
    if not result.get("ok"):
        return jsonify({"ok": False, "error": result.get("error")}), 500
    if result.get("reloaded"):
        return jsonify({"ok": True, "reloaded": True,
                        "providers_count": result.get("providers_count", 0),
                        "models_count": result.get("models_count", 0)}), 200
    if result.get("last_error"):
        # 写入成功但核验失败（已 kickstart 仍未生效）→ 202；空清单重写的核验失败同样 202（9.4）。
        return jsonify({"ok": True, "reloaded": False,
                        "last_error": result.get("last_error"),
                        "providers_count": result.get("providers_count", 0),
                        "models_count": result.get("models_count", 0)}), 202
    # no-op（无任何 proxy=true provider）：200 + 全零（E10）。
    return jsonify({"ok": True, "reloaded": False, "providers_count": 0,
                    "models_count": 0, "last_error": None}), 200


