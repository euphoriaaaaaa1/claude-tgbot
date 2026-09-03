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


def _require_json_object(body):
    """§0.1 通则（BUG-18 → §12.7 ㊳）：请求体不是 dict 一律 ``400 bad_body``。

    **三段共用** —— provider 段 / oauth 段 / bots 段的每个收请求体的端点都调这一个。
    放在模块公共区、名字不带段前缀，是因为它**本来就该只有一份**；
    段内辅助才必须带段前缀（第 2/3 段各写一个同名 ``_compensate`` 互顶过，
    把 provider activate 顶成了 404）。要加新的段内辅助请回各自段里加。

    入参是 ``request.get_json(silent=True)`` 的结果：``None``（空体 / Content-Type 不是
    json / 非法 JSON）与**合法 JSON 但顶层是数组 / 字符串 / 数字 / null / 布尔**在这里一视同仁
    —— 不拦的话下一步 ``body.get()`` 就是 ``AttributeError`` → 500，
    把用户的输入错误报成服务器故障。
    空体想报更准的码（如 §4.2 的 ``bad_provider``）由调用方在传进来之前自己兜。
    """
    if not isinstance(body, dict):
        raise provider_model.HubError(400, "bad_body", "请求体必须是一个 JSON 对象")
    return body


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
    """§3.3 校验（PATCH 传 base = 原条目字段，请求体是子集）。逃生门默认关。

    §0.1 通则在这里过一道：本段两个收请求体的端点（POST / PATCH provider）都经由它，
    非 dict 的体到不了下面的逐字段校验。纯函数层自己也有同一道守卫（它是公开函数，
    谁都能直接调），这里这道是**路由层的契约点**，不是重复。
    """
    return provider_model.validate_provider_payload(
        _require_json_object(body),
        allow_private=os.environ.get("HUB_ALLOW_PRIVATE_BASE_URL") == "1", base=base)


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
# 段内 import：段界就是并行分工线（见文件顶注），各段各带各的依赖，
# 谁也不用改文件顶部那几行 —— 并行 worktree 合并时零冲突。
import functools                                              # noqa: E402
import hashlib                                                # noqa: E402
import threading                                              # noqa: E402
import time                                                   # noqa: E402
from urllib.parse import parse_qs, quote, urlsplit            # noqa: E402

from flask import current_app, request                        # noqa: E402

from moments import cliproxy_client, redact, settings_io      # noqa: E402
from moments.provider_model import HubError                   # noqa: E402

#: 本进程发出去的授权会话：state -> (provider, 发出时刻)。5 分钟窗口一过就当没发过 ——
#: §4.4 的「未知 / 已过期」本来就是同一个态（都得让用户重开一次授权）。
#: ponytail: 进程内内存，门户是单进程 Flask 够用；真上多 worker 得挪到共享存储。
_OAUTH_SESSIONS = {}

_STATUS_TEXT = {"ok": "授权已完成", "wait": "还在等授权完成…", "error": "授权失败，请重新开始"}

#: §4.3：超窗、失配、已消费三种情况用户看到同一句话 —— 动作都是"重走一次 start"。
_STATE_EXPIRED_TEXT = "授权会话已过期或已被新的登录取代，请重新点击开始登录"

#: §4.6：detail 必须指出修复路径，否则用户拿到 409 完全无从下手。
_NO_ALIAS_TEXT = ("该账户尚未配置模型映射，请在 cliproxy 的 oauth-model-alias（按渠道）"
                  "或该账户 auth JSON 的 model_aliases 里加一条")


def _json_errors(fn):
    """HubError -> §0.1 的 ``{"error","detail"}`` + 状态码。

    M3/M7 落地时如果也要，提到文件顶部共用即可（现在提上去只会跟并行分支抢同一行）。
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except HubError as e:
            return jsonify({"error": e.error, "detail": e.detail}), e.status
    return wrapper


def _json_body():
    """本段取请求体：§0.1 通则走模块公共区的 ``_require_json_object``（三段共用）。

    只在"空体"这一处与它不同：Content-Type 不对 / 空体回 ``None`` → 按"体缺失"当空对象，
    交给各端点自己的必填校验去报更准的码（如 ``bad_provider``）。
    合法但顶层是数组 / 字符串 / 数字 / 布尔的体照样 ``400 bad_body``。
    """
    body = request.get_json(silent=True)
    return {} if body is None else _require_json_object(body)


def _provider_or_400(raw):
    """§4：枚举只有 codex / antigravity。anthropic 被显式移除（Claude 走 §5 原生订阅）。"""
    if isinstance(raw, str) and raw in OAUTH_CALLBACK_PORTS:
        return raw
    raise HubError(400, "bad_provider", "只支持 ChatGPT（codex）与 Google（antigravity）")


def _remember_state(state, provider):
    now = time.monotonic()
    for s, (_p, t) in list(_OAUTH_SESSIONS.items()):    # 快照迭代：并发轮询时不会炸
        if now - t > OAUTH_WAIT_SECONDS:
            _OAUTH_SESSIONS.pop(s, None)
    _OAUTH_SESSIONS[state] = (provider, now)


def _state_alive(state, provider=None):
    """state 是不是本进程 5 分钟内发出去的（给了 provider 就连渠道一起对）。"""
    got = _OAUTH_SESSIONS.get(state)
    if not got or time.monotonic() - got[1] > OAUTH_WAIT_SECONDS:
        return False
    return provider is None or got[0] == provider


def _consume_state(state, provider):
    """§4.3 的"校验 + 用过即焚"，**必须是一步**：分两步做的话，一串 state 的并发重放
    能在删除落地之前同时穿过检查，全都被转发给 cliproxy —— ㊱ 要防的就是这个放大器。

    ``dict.pop`` 在 GIL 下是原子的，所以"摘牌"这一下天然只有一个请求拿得到。
    返回摘下来的记录，供转发失败时挂回去（§4.3：只有 200 才作废）。
    失配 / 超窗 / 已被消费三种情况同码同文案 —— 用户的动作都是重走一次 start。
    """
    got = _OAUTH_SESSIONS.pop(state, None)
    if (got is None or got[0] != provider
            or time.monotonic() - got[1] > OAUTH_WAIT_SECONDS):
        raise HubError(409, "oauth_state_expired", _STATE_EXPIRED_TEXT)
    return got


def _mask_email(email):
    """§4.1：本地部分只留首字符（与 key_masked 同思路）。"""
    local, at, domain = (email if isinstance(email, str) else "").partition("@")
    return (local[:1] or "") + "****" + at + domain


def _email_hash(email):
    """§4.5/§4.6 的 URL 段 = ``sha1(email)[:8]``：完整邮箱不进 URL（[SEC] 2.1 坑 1）。"""
    raw = email if isinstance(email, str) else ""
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]


def _account_aliases(f):
    """该账户登记的官方 alias（键序 = §10 里 model_aliases 的插入序，第一个是主 alias）。"""
    m = f.get("model_aliases")
    return [k for k in m if isinstance(k, str) and k] if isinstance(m, dict) else []


def _account_view(f):
    """§10 的 ``files[]`` 条目 → §4.1 的 ``accounts[]`` 对象。

    **只投影这六个字段**（§0.2 第一层）：auth-file 里还躺着 OAuth 令牌之类，
    整条带出去就是把订阅凭证送到浏览器。邮箱只以打码形态出现，URL 段用 hash。
    """
    email, aliases = f.get("email"), _account_aliases(f)
    # §4.1：activatable 含 activate 的**全部前置检查** —— 页面给得出的可点入口必须点了会成，
    # 否则只是把失败从"按钮置灰"推迟到"点了报错"。两个理由分开报，页面给不同提示。
    if f.get("provider") not in OAUTH_CALLBACK_PORTS:
        # 用户自己用命令行登过的其它渠道：原样列出但不给切换入口（§10：隐藏会让人重复授权）
        reason = "unsupported_provider"
    elif not aliases:
        reason = "no_alias"                     # 切过去 ANTHROPIC_MODEL 无值可写（§4.6）
    else:
        reason = None
    return {"provider": f.get("provider"),
            "email_masked": _mask_email(email),
            "email_hash": _email_hash(email),
            "status": "disabled" if f.get("disabled") else "active",
            "alias": aliases[0] if aliases else None,
            "activatable": reason is None,
            "not_activatable_reason": reason}


def _auth_files(client):
    """读 OAuth 账户列表。真机回 ``{"files":[...]}``（键名≠路径段，M2 实测），桩同形。"""
    got = client.mgmt_get("auth-files")
    return [f for f in cliproxy_client.unwrap_list(got, "files", "auth-files")
            if isinstance(f, dict)]


def _find_account(files, provider, email_hash):
    for f in files:
        if f.get("provider") == provider and _email_hash(f.get("email")) == email_hash:
            return f
    raise HubError(404, "not_found", "没有这个 OAuth 账户")


@hub_bp.get("/hub/api/oauth/accounts")
def oauth_accounts():
    """§4.1：**恒 200**（页面要能开）。三种故障态一律 accounts=[] + warnings 非空，
    与「一个都没登过」（warnings 是空数组）严格可区分。"""
    try:
        files = _auth_files(cliproxy_client.from_env())
    except HubError as e:
        warn = (e.error if e.error in ("cliproxy_unconfigured", "cliproxy_down")
                else "cliproxy_error")      # 管理 API 非 2xx：§7 规定只作为 warning 出现
        return jsonify({"accounts": [], "warnings": [warn]})
    return jsonify({"accounts": [_account_view(f) for f in files], "warnings": []})


@hub_bp.post("/hub/api/oauth/start")
@_json_errors
def oauth_start():
    body = _json_body()
    provider = _provider_or_400(body.get("provider"))
    client = cliproxy_client.from_env()          # 没配 → 503 cliproxy_unconfigured（§0.3）
    data = client.mgmt_get("%s-auth-url" % provider)
    data = data if isinstance(data, dict) else {}
    url, state = data.get("url"), data.get("state")
    # 这个 url 会被页面渲染成可点链接，非 http(s) 一律不接（本机 cliproxy 被替换掉时的兜底）。
    if not (isinstance(url, str) and url.startswith(("http://", "https://"))
            and isinstance(state, str) and state):
        raise HubError(502, "cliproxy_reject", "cliproxy 没给出可用的授权链接")
    _remember_state(state, provider)
    return jsonify({"url": url, "state": state,
                    "callback_port": OAUTH_CALLBACK_PORTS[provider],
                    "expires_in": OAUTH_WAIT_SECONDS})


def _split_callback(raw):
    """§4.3 形式一：把粘回来的整串回调 URL 拆成 ``(code, state)``。

    只认**绝对 http(s) URL**：``localhost:1455/...``、``/callback?...``、``javascript:``
    这些都不是用户从地址栏整串复制出来的东西（§4.7 引导的就是"整串"）。
    """
    parts = urlsplit(raw) if isinstance(raw, str) else None
    if parts is None or parts.scheme not in ("http", "https") or not parts.netloc:
        raise HubError(400, "bad_redirect_url",
                       "请把浏览器地址栏里以 http:// 开头的**整串**地址粘进来")
    q = parse_qs(parts.query)
    code = (q.get("code") or [""])[0]
    if not code:
        # URL 形状对但没有 code：多半是粘了授权页而不是回调页。早说清楚，
        # 比原样送给 cliproxy 换回一个 502 好懂得多。
        raise HubError(400, "bad_redirect_url", "这串地址里没有 code 参数，粘错页面了？")
    return code, (q.get("state") or [""])[0]


@hub_bp.post("/hub/api/oauth/submit")
@_json_errors
def oauth_submit():
    body = _json_body()
    provider = _provider_or_400(body.get("provider"))
    if "redirect_url" in body:                            # 形式一：整串回调 URL
        redirect_url = body["redirect_url"]
        code, state = _split_callback(redirect_url)
    elif body.get("code"):                                # 形式二：code + state
        redirect_url, code, state = None, str(body["code"]), body.get("state")
        if not isinstance(state, str) or not state:
            raise HubError(400, "bad_body", "code 形式必须同时带 state")
    else:
        raise HubError(400, "bad_body", "请给 redirect_url，或 code + state")
    # state 只在**给了**的时候校验：各家回调不一定带 state，没带的那层由 cliproxy 自己兜。
    ticket = _consume_state(state, provider) if state else None
    try:
        client = cliproxy_client.from_env()
        payload = {"provider": provider, "code": code, "state": state}
        if redirect_url:        # 两种字段都送：真机认哪个由 cliproxy 定，桩两种都认
            payload["redirect_url"] = redirect_url
        client.mgmt_post("oauth-callback", payload)
    except HubError:
        if ticket is not None:  # §4.3：只有 200 才作废，失败时挂回去让用户能重试
            _OAUTH_SESSIONS[state] = ticket
        raise
    return jsonify({"ok": True})               # §4.3：只表示已提交，不代表已落盘


@hub_bp.get("/hub/api/oauth/status")
@_json_errors
def oauth_status():
    state = (request.args.get("state") or "").strip()
    if not state:
        raise HubError(400, "bad_body", "缺 state")
    client = cliproxy_client.from_env()
    # 先问 cliproxy、再判本地会话：cliproxy 没跑时哪怕 state 是瞎编的也得回 503，
    # 否则页面会把「依赖挂了」显示成「会话过期」，用户只会一遍遍重开授权（§4.4 用例）。
    data = client.mgmt_get("get-auth-status?state=%s" % quote(state, safe=""))
    if not _state_alive(state):
        # §4.4 [R4D] I1-9：它是流程状态不是资源，**200 不是 404** —— 页面轮询不该被打断。
        return jsonify({"status": "error", "detail": "授权会话不存在或已过期，请重新开始"})
    data = data if isinstance(data, dict) else {}
    raw = data.get("status")
    # 认不出的上游态一律当"还在等"：页面有 5 分钟倒计时兜底，不会无限转圈；
    # 反过来把未知当 error 会在上游改字面量时误报"授权失败"。
    status = raw if raw in ("ok", "wait", "error") else "wait"
    detail = next((data[k] for k in ("detail", "message")
                   if isinstance(data.get(k), str) and data[k]), "")
    return jsonify({"status": status,
                    "detail": redact.scrub_text(detail)[:200] if detail else _STATUS_TEXT[status]})


def _is_active(port, aliases):
    """这些 alias 里有没有正被 settings.json 钉住的（§4.5 ``delete_active`` 的判据）。

    只判到「settings 指着本机 cliproxy 且 ANTHROPIC_MODEL 是它登记的 alias」这一层：
    同一个 alias 也可能同时挂着第三方 provider 条目，最终谁接管由 §3.0d 的 prefix 决定，
    读侧看不出来。宁可多拦一次删除，也别把用户正在用的账户删掉。
    settings 读不出/写坏了 → 证明不了它 active，按 §4.5 照常放行（该端点没有 unparsable 态）。
    """
    if not aliases:
        return False
    try:
        settings = provider_model.parse_settings(
            settings_io.read_text(provider_model.settings_path()))
    except HubError:
        return False
    act, _warnings = provider_model.resolve_active(settings, port, [])
    return act["kind"] == "cliproxy" and act["alias"] in aliases


@hub_bp.delete("/hub/api/oauth/accounts/<provider>/<email_hash>")
@_json_errors
def oauth_account_delete(provider, email_hash):
    client = cliproxy_client.from_env()
    acc = _find_account(_auth_files(client), provider, email_hash)
    if _is_active(client.port, _account_aliases(acc)):
        raise HubError(409, "delete_active", "这是当前生效的账户，先切到别的来源再删")
    # 用 name（auth-dir 里的文件名）定位，走请求体不走 URL —— 邮箱不进任何 URL。
    client.mgmt_delete("auth-files", {"name": acc.get("name")})
    return jsonify({"ok": True})


def _claim_aliases(client, aliases, undo):
    """§3.6 B1 的 OAuth 版：让这些 alias 下的第三方 provider 条目全部让位。

    手段与 §3.0d 一致 —— 落选条目 ``prefix`` = 它自己的 id，裸官方名才只命中我们要的那边。
    改过谁**原地追加**进 ``undo``（中途抛错时调用方照样拿得到已改的那部分，能补偿）。
    ponytail: 同渠道的其它 OAuth 账户一律不动 —— 多账户共用一个 alias 是配额池不是冲突，
    禁掉它们等于把用户刚登的号白登了。
    """
    for e in client.entries():
        own = {a for a in (e.view.get("model_alias"), e.view.get("haiku_alias")) if a}
        if not own & aliases:
            continue
        old = e.raw.get("prefix")
        old = old if isinstance(old, str) else ""
        if old == e.view["id"]:
            continue                    # 已经让过位：不重复写（幂等重跑零写入）
        client.replace_entry(e.kind, e.index, dict(e.raw, prefix=e.view["id"]))
        undo.append({"kind": e.kind, "index": e.index, "prefix": old})


# 段内辅助必须带段前缀（此前与第 2 段同名互顶导致 provider activate 404）
def _oauth_compensate(client, undo):
    """§3.6 的补偿：prefix 原样写回、被我们启用的账户再禁回去。补偿本身失败 = 不一致态。"""
    try:
        client.restore_prefixes([r for r in undo if "kind" in r])
        for r in undo:
            if "account" in r:
                client.mgmt_patch("auth-files/status",
                                  {"name": r["account"], "disabled": True})
    except HubError:
        raise HubError(500, "activate_partial",
                       "cliproxy 已改、settings.json 未改，请重试或手动检查")


def _activate_lock():
    """§12.8 ㊴：三个写 settings.json 的入口（§3.6 / §4.6 / §5）共用的一把非阻塞锁。

    键名与对象都定死在 ``app.extensions["hub_activate"]``（与 §6.2 重启锁同款作用域）：
    第 2 段的模块级辅助落地后，同键 ``setdefault`` 拿到的就是同一把锁，天然合一。
    ``dict.setdefault`` 在 GIL 下原子，两个并发请求不会各造一把。
    """
    return current_app.extensions.setdefault("hub_activate", threading.Lock())


@hub_bp.post("/hub/api/oauth/accounts/<provider>/<email_hash>/activate")
@_json_errors
def oauth_account_activate(provider, email_hash):
    """§4.6 = §3.6 的 cliproxy 形态，阶段顺序照抄：A0 抢锁 / A 只读 / B 可补偿 / C 不可回退。"""
    # A0：拿不到就 409，**不排队**（§12.8 ㊴）—— 切换是秒级人工操作，排队只会把
    # "两个标签页各点一次"变成"看起来都成了、实际后一次覆盖前一次"。
    lock = _activate_lock()
    if not lock.acquire(blocking=False):
        raise HubError(409, "activate_busy", "另一个切换正在进行，请稍后再试")
    try:
        return _oauth_activate_locked(provider, email_hash)
    finally:
        lock.release()


def _oauth_activate_locked(provider, email_hash):
    client = cliproxy_client.from_env()
    acc = _find_account(_auth_files(client), provider, email_hash)           # A1
    # 列表给的是 activatable:false，接口就不能背着页面把它切了 —— 用户自己用命令行登过的
    # 其它渠道（典型是 anthropic）只许看与删，切 Claude 订阅走 §5 的原生端点。
    _provider_or_400(provider)
    aliases = _account_aliases(acc)
    if not aliases:
        # §4.6 专码：请求体是空对象，用户没传错任何东西 —— 是账户还没被映射到官方模型名
        # 这个**状态**问题（切过去 ANTHROPIC_MODEL 无值可写），归 409 不归 400。
        raise HubError(409, "oauth_no_alias", _NO_ALIAS_TEXT)
    path = provider_model.settings_path()
    settings = provider_model.parse_settings(settings_io.read_text(path))    # A2
    if not settings_io.dir_writable(path):                                   # A3
        raise HubError(500, "settings_write_failed", "settings.json 所在目录不可写")
    client.healthz()                                                         # A4
    undo = []
    try:
        _claim_aliases(client, set(aliases), undo)                           # B1
        if acc.get("disabled"):                                              # B2
            client.mgmt_patch("auth-files/status",
                              {"name": acc.get("name"), "disabled": False})
            undo.append({"account": acc.get("name")})
        settings_io.write_json_atomic(                                       # C
            path, provider_model.apply_cliproxy_env(settings, client.port,
                                                    client.api_key, aliases[0]))
    except HubError:
        _oauth_compensate(client, undo)       # 补偿再失败会抛 500 activate_partial 顶掉原错
        raise
    return jsonify({"ok": True, "active_kind": "cliproxy"})


# ======================================================================
# 第 4 段 · bot 状态与重启（M7 填）
#
# 契约：INTERFACE §6.0-§6.2。要点：探活用 POST（dispatcher 对非 POST 一律 405）、
# 并发探活整体封顶 2 秒、重启改异步 202 + 轮询、job 只留最近 5 条、
# tail 出站前过 §0.2 第二层脱敏（restart-bots.sh 的 stderr 可能带出 Telegram token）。
# 两个测试接缝 HUB_RESTART_CMD / HUB_BOTS_FILE 在 bots_client.py 里落，不写进 README。
# ======================================================================

# import 放在本段内而不是文件顶部：段界就是分工线，顶部 import 区是各段的公共冲突点。
from flask import current_app                          # noqa: E402

from moments import bots_client                        # noqa: E402


@hub_bp.record_once
def _init_restart_jobs(state):
    """每个 app 一份 job 表（生产里一个进程一个 app = §6.2 的进程内互斥）。"""
    state.app.extensions["hub_restart_jobs"] = bots_client.RestartJobs()


def _jobs():
    return current_app.extensions["hub_restart_jobs"]


@hub_bp.get("/hub/api/bots")
def bots_list():
    try:
        entries = bots_client.list_bots()
    except Exception as e:      # 名单读不出来就是 config_unreadable，detail 只给类名（§6.0/§7）
        return jsonify({"error": "config_unreadable", "detail": type(e).__name__}), 500
    # 探活自己吞异常，不会把故障冒成 config_unreadable；空名单是正常态，不是错误。
    return jsonify({"bots": bots_client.probe_bots(entries),
                    "restart_available": bots_client.restart_plan()[1]})


@hub_bp.post("/hub/api/bots/restart")
def bots_restart():
    argv, available = bots_client.restart_plan()
    if not available:
        return jsonify({"error": "restart_script_missing", "detail": "先跑 install.sh"}), 409
    acc = _jobs().start(argv)
    if acc.busy:
        return jsonify({"error": "restart_in_progress", "job_id": acc.busy}), 409
    return jsonify({"job_id": acc.job_id, "started_at": acc.started_at}), 202


@hub_bp.get("/hub/api/bots/restart/<job_id>")
def bots_restart_status(job_id):
    st = _jobs().get(job_id)
    if st is None:
        return jsonify({"error": "not_found"}), 404
    return jsonify(st)
