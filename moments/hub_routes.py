# -*- coding: utf-8 -*-
"""管理台蓝图（PLAN M1 空壳骨架 / INTERFACE §2 §3 §4 §5 §6）。

四段固定顺序，段界就是并行分工线，改自己那段别越界：

  第 1 段  页面路由        M1 已实现，其余模块只读不改
  第 2 段  provider CRUD   M3 填（§3 + §5 claude-native）
  第 3 段  OAuth 账户      M5 填（§4）
  第 4 段  bots 状态与重启 M7 填（§6）

未填的端点一律回 ``501 {"error":"not_implemented"}``：形状合规（§0.1），
但 **501 不在 §7 错误码总表里** —— 它是「这段还没人填」的施工态，
某段填完时该段的 ``_todo()`` 必须一个不剩。

鉴权由 ``moments/hub_auth.py`` 的 before_request 统管（``/hub`` 及其子路径全锁），
本模块不写任何鉴权判断。``/hub/healthz`` 已在 ``hub_auth.install()`` 注册，**不要在这里重复注册**。
"""
from flask import Blueprint, jsonify, render_template

from moments import provider_model

hub_bp = Blueprint("hub", __name__)

# §4.2：OAuth 回调端口的唯一真相表。路由、页面文案、README 三处都从它取，
# 不在各处各硬编码一遍（[RA] 建议2）。M5 接第 3 段时复用它，别另起一张。
OAUTH_CALLBACK_PORTS = {"codex": 1455, "antigravity": 51121}
OAUTH_PROVIDER_LABELS = {"codex": "ChatGPT", "antigravity": "Google（Antigravity）"}
OAUTH_WAIT_SECONDS = 300        # §4.4 服务端授权等待窗口，页面按它显示倒计时


def _todo():
    """空壳占位：合规 JSON 形状 + 501。填完该端点即删本次调用。"""
    return jsonify({"error": "not_implemented"}), 501


# ======================================================================
# 第 1 段 · 页面路由（M1，已完成）
#
# 铁律（§2 末 / [R4D] I1-3）：三个页面在 cliproxy 没跑、settings.json 被手写坏、
# bot 全离线等任何依赖故障下**一律 200 HTML**，数据全部由前端调 /hub/api/* 取。
# 所以这三个函数**禁止**碰 cliproxy、禁止读 settings.json —— 不碰依赖，
# 就不可能被依赖打成 5xx。要加数据请加到 API 端点里，不要加到这里。
# ======================================================================


@hub_bp.get("/hub")
def hub_home():
    return render_template("hub.html", nav_active="hub")


@hub_bp.get("/hub/provider")
def hub_provider_page():
    # 下拉框的 kind 选项从 KIND_SPEC 派生（[RA] 建议2：校验/互转/页面三处同源）。
    return render_template(
        "hub_provider.html",
        nav_active="provider",
        kinds=provider_model.SUPPORTED_KINDS,
        default_haiku_alias=provider_model.DEFAULT_HAIKU_ALIAS,
        oauth_providers=[{"id": pid,
                          "label": OAUTH_PROVIDER_LABELS[pid],
                          "callback_port": port}
                         for pid, port in OAUTH_CALLBACK_PORTS.items()],
        oauth_wait_minutes=OAUTH_WAIT_SECONDS // 60,
    )


@hub_bp.get("/hub/bots")
def hub_bots_page():
    return render_template("hub_bots.html", nav_active="bots")


# ======================================================================
# 第 2 段 · 第三方 key provider + Claude 原生订阅（M3 填）
#
# 契约：INTERFACE §3.2-§3.7、§5；错误码只许用 §7 总表里的。
# 依赖：M4 的 moments/provider_model.py（校验/互转/落键计算）、
#       M2 的 moments/cliproxy_client.py（管理 API + set_alias_exclusive）、
#       moments/settings_io.py（原子写）。
# 注意 §0.3：cliproxy 三键任一缺失时，GET /hub/api/provider 仍 200
#       （warnings=["cliproxy_unconfigured"]），其余写端点 503；
#       claude-native/activate 不碰 cliproxy，不受影响。
# ======================================================================


import contextlib                             # noqa: E402  第 2 段自己的依赖，段界即分工线
import functools                              # noqa: E402
import os                                     # noqa: E402
import sys                                    # noqa: E402
import threading                              # noqa: E402

from flask import current_app, request        # noqa: E402

from moments import cliproxy_client, redact, settings_io   # noqa: E402
from moments.provider_model import HubError   # noqa: E402

# §3.7：未知模型一律按 message 文本判，**不按状态码**（真机回 400、[CPX] 写的是 502，
# 按码归类会在上游版本一变就把"配置缺失"误报成"上游报错"）。
ROUTING_HINT = "unknown provider for model"


def _api(fn):
    """把 HubError 翻成 §0.1 形状的错误响应；其余异常照旧冒泡给 500 internal 处理器。

    `detail` 是人读文案，出站还会再过一遍 §0.2 第二层兜底（web.py 的 after_request）。
    """
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        try:
            return fn(*a, **kw)
        except HubError as e:
            return jsonify({"error": e.error, "detail": e.detail}), e.status
    return wrapper


@contextlib.contextmanager
def hold_activate_lock(app=None):
    """§3.6 A0：**三个写 settings.json 的入口共用的一把非阻塞锁**（§3.6 / §5 / §4.6）。

    不加锁时两个并发切换会各自基于**旧快照**读-改-写条目表，终态可能两条都带 prefix
    （settings 指的 alias 一个不带 prefix 的条目都没有、haiku 也是 0），而双方都回 200 ——
    用户看到"切换成功"，bot 却全线 502（BUG-19）。锁覆盖整段 A→B→C（含写 settings），
    终态必是最后完成者的完整结果，不会是两次切换各写一半拼出来的四不像。

    抢不到不排队，直接 `409 activate_busy`：切换是秒级人工操作，排队只会把
    "两个标签页各点一次"变成"看起来都成了、实际后一次覆盖前一次"，还要引进
    超时/队列长度/取消这些本不需要的东西。
    锁挂在 ``app.extensions["hub_activate"]``（与 §6.2 重启锁同款作用域，测试不串扰）。
    **M5 的 §4.6 OAuth activate 直接 `with hold_activate_lock():` 即可，别另起一把。**
    """
    ext = (app or current_app).extensions
    lock = ext.setdefault("hub_activate", threading.Lock())     # dict.setdefault 在 GIL 下原子
    if not lock.acquire(blocking=False):
        raise HubError(409, "activate_busy", "另一次切换正在进行，请稍候重试")
    try:
        yield
    finally:
        lock.release()


def _validated(body, base=None):
    """§3.3 校验（PATCH 传 base = 原条目字段，请求体是子集）。逃生门默认关。"""
    return provider_model.validate_provider_payload(
        body, allow_private=os.environ.get("HUB_ALLOW_PRIVATE_BASE_URL") == "1", base=base)


def _settings_now():
    """读并解析 settings.json（§3.6 A2）。坏文件抛 409 settings_unparsable。"""
    path = provider_model.settings_path()
    return path, provider_model.parse_settings(settings_io.read_text(path))


def _active_id(port, views):
    """当前生效的是哪条（§3.2 判定）。settings 读不出/坏了 → None：
    删除与改动照常放行，不因为一份手写坏的配置把管理台锁死。"""
    try:
        _path, settings = _settings_now()
    except HubError:
        return None
    return provider_model.resolve_active(settings, port, views)[0]["id"]


@hub_bp.get("/hub/api/provider")
def provider_list():
    """§3.2：**恒返回 200**（§2 铁律：页面要能打开），三种依赖故障用 warnings 区分。"""
    cfg = cliproxy_client.load_config()
    info = {"running": False, "port": cfg["port"], "version": None,
            # supported_kinds 是本地静态知识：没配/没跑照样得给，否则页面下拉是空的
            "supported_kinds": list(provider_model.SUPPORTED_KINDS),
            "anthropic_compat": cfg["anthropic_compat"]}
    warnings, providers = [], []
    if not cfg["configured"]:
        warnings.append("cliproxy_unconfigured")
    else:
        try:
            client = cliproxy_client.from_env()
            health = client.healthz()
            info["running"] = True              # 拿到 HTTP 响应就算跑着（§0.4 传输层/应用层分界）
            if isinstance(health, dict):
                info["version"] = health.get("version")
            providers = client.providers()
        except HubError as e:
            providers = []
            if getattr(e, "upstream_status", None) is not None:
                # 跑着但管理 API 非 2xx：§3.2 第四态 —— running=true + [] + cliproxy_error
                info["error_status"] = e.upstream_status
                warnings.append("cliproxy_error")
            else:
                info["running"] = False
                warnings.append(e.error)        # cliproxy_down / cliproxy_unconfigured
    try:
        _path, settings = _settings_now()
    except HubError:
        settings = None                         # 读不出/坏了：恒 200，故障走 warnings
    active, warn = provider_model.resolve_active(settings, cfg["port"], providers)
    warnings.extend(warn)
    hit = None
    for p in providers:
        p["active"] = active["id"] is not None and p["id"] == active["id"]
        hit = p if p["active"] else hit
    # 第六个 warning（§3.2）：与 active.kind **正交** —— active 条目的 haiku alias 下
    # 不带 prefix 的条目数 ≠ 1（0 个 = 后台摘要必 502；≥2 个 = 在多家之间轮询，
    # 用户只看到"摘要偶尔串味"）。可与 alias_disabled 同时出现，warnings 是数组。
    if hit and hit["haiku_alias"] and len(_unprefixed(providers, hit["haiku_alias"])) != 1:
        warnings.append("haiku_conflict")
    return jsonify({"cliproxy": info, "active": active,
                    "providers": providers, "warnings": warnings})


@hub_bp.post("/hub/api/provider")
@_api
def provider_create():
    """§3.3：校验 → 查重 → 写 cliproxy 一处（台账已删，没有第二个存储要对账）。"""
    p = _validated(request.get_json(silent=True))
    client = cliproxy_client.from_env()
    entries = client.entries()
    pid = provider_model.provider_id(p)
    if any(e.view["id"] == pid for e in entries):
        raise HubError(409, "duplicate_provider", "同一个来源（含 base_url/上游模型/alias）已存在")
    # §3.3 + §3.0c：主 alias ∪ haiku alias 任一被别的 enabled 条目占用 → 新建即停用。
    # haiku 默认值人人相同，所以第二个起的 provider 必然创建即 disabled，这是预期行为。
    disabled = provider_model.alias_occupied([e.view for e in entries],
                                             [p["model_alias"], p["haiku_alias"]])
    entry = provider_model.to_block_entry(p, disabled=disabled)
    client.add_entry(p["kind"], entry)
    # 响应由刚写下去的条目现算：from_block_entry 只带出安全字段 + key_masked，
    # 明文 key 不进响应体（§3.1「没有 api_key 字段」）。省一次回读往返。
    return jsonify(provider_model.from_block_entry(p["kind"], entry)), 201


@hub_bp.patch("/hub/api/provider/<pid>")
@_api
def provider_update(pid):
    """§3.4：请求体是 §3.3 的子集，缺的字段取原值；改四元组 → **换 id**，响应里是新 id。"""
    client = cliproxy_client.from_env()
    entries = client.entries()
    e = client.find(pid, entries)
    if e is None:
        raise HubError(404, "not_found", "provider 不存在")
    views = [x.view for x in entries]
    # 省略 api_key = 保持原 key：从条目里取出明文只为原样写回，不进响应也不进日志
    base = dict(e.view, api_key=provider_model.entry_api_key(e.kind, e.raw))
    p = _validated(request.get_json(silent=True), base=base)
    new_id = provider_model.provider_id(p)
    if new_id != pid and any(x.view["id"] == new_id for x in entries):
        raise HubError(409, "duplicate_provider", "改成的来源与已有条目重复")
    was_active = _active_id(client.port, views) == pid       # 改之前判，改完再切（§3.4 末条）
    entry = provider_model.to_block_entry(p, disabled=e.view["disabled"])
    if p["kind"] == e.kind:
        # 按下标写是整条替换：把原条目里我们不管的字段（weight / 用户手改的 disabled /
        # excluded-models）合进来，否则这次 PATCH 等于顺手把它们删了。
        entry = dict(e.raw, **entry)
        client.replace_entry(e.kind, e.index, entry)
    else:
        client.delete_entry(e.kind, e.index)                 # 换 kind = 换承载块，只能删了重建
        client.add_entry(p["kind"], entry)
    view = provider_model.from_block_entry(p["kind"], entry)
    if was_active:
        _activate(client, new_id)            # 不自动切的话 bot 会打到一个已经不存在的 alias
        view["active"] = True
    return jsonify(view)


@hub_bp.delete("/hub/api/provider/<pid>")
@_api
def provider_delete(pid):
    """§3.5：只动 cliproxy 一处，不碰 settings.json（当前生效的先切走再删）。"""
    client = cliproxy_client.from_env()
    entries = client.entries()
    e = client.find(pid, entries)
    if e is None:
        raise HubError(404, "not_found", "provider 不存在")
    if _active_id(client.port, [x.view for x in entries]) == pid:
        raise HubError(409, "delete_active", "它正在生效，先切换到别的来源再删")
    client.delete_entry(e.kind, e.index)
    return jsonify({"ok": True, "id": pid})


def _both_aliases(view):
    """§3.0c：主 alias + haiku alias。claude CLI 一次会话只请求这两个 model id，
    少注册/少互斥一个，后台摘要请求就打到别的后端或直接 502。"""
    out = []
    for a in (view.get("model_alias"), view.get("haiku_alias")):
        if a and a not in out:
            out.append(a)
    return out


def _unprefixed(views, alias):
    """注册了该 alias 且**当前不带 prefix** 的条目 —— 就是运行时会接裸模型名的那些。

    读侧 `disabled` = `prefix 非空 or 用户手写 disabled: true` 的或（§3.0d）：
    手改停用的条目同样不接请求，一起排除掉才不说谎。
    """
    return [p for p in views if not p["disabled"] and alias in _both_aliases(p)]


def _sole(views, alias, target_id):
    """该 alias 下不带 prefix 的恰好是 target 一条（§3.6 reconcile 的 I1/I2）。"""
    live = _unprefixed(views, alias)
    return len(live) == 1 and live[0]["id"] == target_id


def _compensate(client, records):
    """§3.6：把 B 阶段改过的 prefix 还原。**补偿本身失败 = 不一致态**（cliproxy 已切、
    settings 未切）→ 500 activate_partial，如实说，不假装成功也不假装回退。重试可收敛。"""
    if not records:
        return
    try:
        client.restore_prefixes(records)
    except HubError:
        raise HubError(500, "activate_partial",
                       "cliproxy 已切换但 settings.json 未更新，请重试；仍失败请手动检查 cliproxy")


def _activate(client, pid):
    """§3.6 三段式：A 只读（无副作用）→ B 改 cliproxy（可补偿）→ C 改 settings（不可回退）。

    顺序不能反：先动 cliproxy 的话，一旦 settings 手写坏了就会留下"旧 alias 已禁用、
    settings 仍指向它" —— bot 每个请求 502，而页面显示"当前生效：旧 provider"，
    用户完全看不出问题在哪。重排后最常见的两类失败都在动 cliproxy 之前返回。
    幂等：同一目标重放收敛到同一结果，不因"已经是这个状态"报错。
    **全段持锁**（含 C 段写 settings）：终态必是最后完成者的完整结果，
    不会是两次切换各写一半拼出来的四不像。
    """
    with hold_activate_lock():                                # A0
        return _activate_locked(client, pid)


def _activate_locked(client, pid):
    entry = client.find(pid)                                  # A1
    if entry is None:
        raise HubError(404, "not_found", "provider 不存在")
    view = entry.view
    path, data = _settings_now()                              # A2
    if not settings_io.dir_writable(path):                    # A3
        raise HubError(500, "settings_write_failed", "settings.json 所在目录不可写")
    client.healthz()                                          # A4
    undo = []
    try:                                                      # B：两个 alias 各跑一次互斥
        for alias in _both_aliases(view):
            undo.extend(client.set_alias_exclusive(alias, pid))
    except HubError as e:
        _compensate(client, undo + list(getattr(e, "undo", None) or []))
        raise
    try:                                                      # C：不可回退段
        settings_io.write_json_atomic(
            path, provider_model.apply_cliproxy_env(data, client.port, client.api_key,
                                                    view["model_alias"]))
    except HubError:
        _compensate(client, undo)
        raise
    return jsonify({"ok": True, "id": pid, "alias": view["model_alias"]})


@hub_bp.post("/hub/api/provider/<pid>/activate")
@_api
def provider_activate(pid):
    return _activate(cliproxy_client.from_env(), pid)


@hub_bp.post("/hub/api/provider/<pid>/test")
@_api
def provider_test(pid):
    """§3.7：真发一次最小请求。上游报错是**业务结果**（HTTP 200 + ok:false + stage），
    只有"我们这边坏了"（cliproxy 不可达 / id 不存在 / 没配）才用非 200。"""
    client = cliproxy_client.from_env()
    e = client.find(pid)
    if e is None:
        raise HubError(404, "not_found", "provider 不存在")
    if e.view["disabled"]:
        return jsonify({"ok": False, "stage": "disabled",
                        "detail": "先切换到它再自测，或它的 alias 正被其他来源占用"})
    r = client.probe_messages(e.view["model_alias"])
    if r["ok"]:
        return jsonify({"ok": True, "latency_ms": r["latency_ms"],
                        "model_echo": r.get("model_echo"), "text_head": r.get("text_head")})
    if r.get("timeout"):
        return jsonify({"ok": False, "stage": "timeout"})
    if ROUTING_HINT in "%s %s" % (r.get("error_type") or "", r.get("error_message") or ""):
        return jsonify({"ok": False, "stage": "routing", "detail": "cliproxy 未注册该模型名"})
    # 只回结构化的两个字段：上游 4xx 常把请求头（含 Authorization）原样回显，
    # 原始响应体绝不端上桌（[R4D] I3-3，脱敏已在 cliproxy_client 里做过）。
    return jsonify({"ok": False, "stage": "upstream", "upstream_status": r.get("status"),
                    "error_type": r.get("error_type"), "error_message": r.get("error_message")})


@hub_bp.post("/hub/api/claude-native/activate")
@_api
def claude_native_activate():
    """§5：只做 §3.6 的 C 阶段，形态是四键全删。**不经过 cliproxy** ——
    没有 A4 探活、没有 B 阶段、也就没有补偿与 activate_partial。
    cliproxy 挂了/压根没配时，这是唯一还能用的逃生出口。
    与 §3.6 共用同一把 activate 锁：它同样写 settings.json，跟切 provider 互斥（BUG-19）。"""
    with hold_activate_lock():
        path, data = _settings_now()
        if not settings_io.dir_writable(path):
            raise HubError(500, "settings_write_failed", "settings.json 所在目录不可写")
        settings_io.write_json_atomic(path, provider_model.apply_native_env(data))
    return jsonify({"ok": True, "active_kind": "claude_native"})


def _reconcile():
    """启动自愈（[RA] F4，全方案唯一收住漂移的东西）。维护的是**两条**不变量：

      I1 主 alias（settings 的 ANTHROPIC_MODEL）下恰有一个条目不带 prefix，且是 active 条目
      I2 该条目的 haiku alias 同样恰有一个不带 prefix，**且必须是同一条**

    只修 I1 会留一个永远自愈不了的态：两个 provider 主 alias 各自唯一、共用默认 haiku 名，
    补偿失败后两条都不带 prefix —— 主模型走对后端，**后台摘要在两家之间轮询**，
    用户只看到"摘要偶尔风格不对"（BUG-20）。

    自愈只写 prefix（§3.0d 禁写 cliproxy 的 disabled 键），只动 cliproxy、**不写 settings**
    —— 用哪个 alias 是用户的意图，以 settings 为准，我们只把运行时对齐到它。
    cliproxy 不可达/没配/settings 坏了一律跳过：启动路径不许把门户拦死（§2 铁律）。
    """
    def log(*fields):
        # alias 来自 settings.json（用户数据），过一遍兜底脱敏再落日志
        print("hub_reconcile " + " ".join(redact.scrub_text(f) for f in fields), file=sys.stderr)

    try:
        if not cliproxy_client.load_config()["configured"]:
            return log("skipped")
        client = cliproxy_client.from_env()
        client.healthz()
        _path, settings = _settings_now()
        alias = (settings.get("env") or {}).get("ANTHROPIC_MODEL")
        if not isinstance(alias, str) or not alias:
            return log("skipped")
        views = client.providers()
        # 收敛目标 = §3.2 的 active 判定：只看主 alias，优先取还不带 prefix 的那条
        cands = [p for p in views if p["model_alias"] == alias]
        target = next((p for p in cands if not p["disabled"]), None) or (cands or [None])[0]
        if target is None:                 # alias 在 cliproxy 里根本不存在 = active_unknown，
            return log("alias=%s" % alias, "fixed=false")   # 列表端点提示"点一次切换即可修复"
        haiku = target["haiku_alias"]
        if _sole(views, alias, target["id"]) and (not haiku or _sole(views, haiku, target["id"])):
            return                                          # I1 与 I2 都成立，健康
        records = client.set_alias_exclusive(alias, target["id"])
        if haiku:
            records += client.set_alias_exclusive(haiku, target["id"])
        # 真写了才算 fixed=true：目标条目是被用户手改的 disabled: true 停用的话这里写不动它
        # （§3.0d 禁止写 cliproxy 的 disabled 键），如实报 false，别把没修好说成修好了。
        log("alias=%s" % alias, "haiku=%s" % (haiku or "-"),
            "fixed=%s" % ("true" if records else "false"))
    except HubError as e:
        log("skipped reason=%s" % e.error)
    except Exception as e:                # 启动路径绝不因为自愈失败而起不来
        log("skipped reason=%s" % type(e).__name__)


# 每次 app 注册蓝图（= 每次门户启动）跑一次，比 before_request 早、也不拖慢首个请求。
hub_bp.record_once(lambda _state: _reconcile())


# ======================================================================
# 第 3 段 · OAuth 订阅账户（M5 填）
#
# 契约：INTERFACE §4.1-§4.7。provider 枚举只有 codex / antigravity
# （anthropic 被显式移除，Claude 订阅走第 2 段的 claude-native）。
# 回调端口取本模块顶部的 OAUTH_CALLBACK_PORTS，别再写一张表。
# §4.1 恒 200：读不出来时 accounts=[] + warnings 非空，页面才打得开。
# ======================================================================


@hub_bp.get("/hub/api/oauth/accounts")
def oauth_accounts():
    return _todo()


@hub_bp.post("/hub/api/oauth/start")
def oauth_start():
    return _todo()


@hub_bp.post("/hub/api/oauth/submit")
def oauth_submit():
    return _todo()


@hub_bp.get("/hub/api/oauth/status")
def oauth_status():
    return _todo()


@hub_bp.delete("/hub/api/oauth/accounts/<provider>/<email_hash>")
def oauth_account_delete(provider, email_hash):
    return _todo()


@hub_bp.post("/hub/api/oauth/accounts/<provider>/<email_hash>/activate")
def oauth_account_activate(provider, email_hash):
    return _todo()


# ======================================================================
# 第 4 段 · bot 状态与重启（M7 填）
#
# 契约：INTERFACE §6.0-§6.2。要点：探活用 POST（dispatcher 对非 POST 一律 405）、
# 并发探活整体封顶 2 秒、重启改异步 202 + 轮询、job 只留最近 5 条、
# tail 出站前过 §0.2 第二层脱敏（restart-bots.sh 的 stderr 可能带出 Telegram token）。
# 两个测试接缝 HUB_RESTART_CMD / HUB_BOTS_FILE 在 bots_client.py 里落，不写进 README。
# ======================================================================


@hub_bp.get("/hub/api/bots")
def bots_list():
    return _todo()


@hub_bp.post("/hub/api/bots/restart")
def bots_restart():
    return _todo()


@hub_bp.get("/hub/api/bots/restart/<job_id>")
def bots_restart_status(job_id):
    return _todo()
