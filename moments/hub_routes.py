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


@hub_bp.get("/hub/api/provider")
def provider_list():
    return _todo()


@hub_bp.post("/hub/api/provider")
def provider_create():
    return _todo()


@hub_bp.patch("/hub/api/provider/<pid>")
def provider_update(pid):
    return _todo()


@hub_bp.delete("/hub/api/provider/<pid>")
def provider_delete(pid):
    return _todo()


@hub_bp.post("/hub/api/provider/<pid>/activate")
def provider_activate(pid):
    return _todo()


@hub_bp.post("/hub/api/provider/<pid>/test")
def provider_test(pid):
    return _todo()


@hub_bp.post("/hub/api/claude-native/activate")
def claude_native_activate():
    return _todo()


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
import time                                                   # noqa: E402
from urllib.parse import parse_qs, quote, urlsplit            # noqa: E402

from flask import request                                     # noqa: E402

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
    """§0.1 通则：请求体不是 JSON 对象一律 ``400 bad_body``（BUG-18）。

    ``get_json(silent=True)`` 对**合法但顶层是数组/字符串/数字/布尔**的体会原样返回，
    再 ``body.get()`` 就是 AttributeError → 500，把用户的输入错误说成门户坏了。
    空体 / Content-Type 不对回 ``None`` → 按"体缺失"当空对象，
    交给各端点自己的必填校验去报更准的码（如 ``bad_provider``）。
    """
    body = request.get_json(silent=True)
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise HubError(400, "bad_body", "请求体必须是一个 JSON 对象")
    return body


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
    return {"provider": f.get("provider"),
            "email_masked": _mask_email(email),
            "email_hash": _email_hash(email),
            "status": "disabled" if f.get("disabled") else "active",
            "alias": aliases[0] if aliases else None,
            # 用户自己用命令行登过的其它渠道原样列出，但不给切换入口（§10：隐藏会让人重复授权）
            "activatable": f.get("provider") in OAUTH_CALLBACK_PORTS}


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
    # state 只在**给了**的时候校验：各家回调不一定带 state，没带的那层由 cliproxy 自己兜；
    # 给了却对不上本进程发出去的会话（或超了 5 分钟）→ §4.3 的专码 409：
    # URL 通常完全正确，只是会话作废了，正确动作是重走 start 而不是改输入（400 会指错路）。
    if state and not _state_alive(state, provider):
        raise HubError(409, "oauth_state_expired", _STATE_EXPIRED_TEXT)
    client = cliproxy_client.from_env()
    payload = {"provider": provider, "code": code, "state": state}
    if redirect_url:            # 两种字段都送：真机认哪个由 cliproxy 定，桩两种都认
        payload["redirect_url"] = redirect_url
    client.mgmt_post("oauth-callback", payload)
    # §4.3 成功消费即作废：授权码本就是一次性凭据。不删就是同一串可无限重放、
    # 每次都真转发给 cliproxy —— 等于给公网可达的门户装了个免费放大器。
    # **只在 200 之后删**：上面任何一步抛错时 state 保留，用户可修正后重试。
    _OAUTH_SESSIONS.pop(state, None)
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


def _compensate(client, undo):
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


@hub_bp.post("/hub/api/oauth/accounts/<provider>/<email_hash>/activate")
@_json_errors
def oauth_account_activate(provider, email_hash):
    """§4.6 = §3.6 的 cliproxy 形态，阶段顺序照抄：A 全只读 / B 可补偿 / C 不可回退。"""
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
        _compensate(client, undo)       # 补偿再失败会抛 500 activate_partial 顶掉原错
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


@hub_bp.get("/hub/api/bots")
def bots_list():
    return _todo()


@hub_bp.post("/hub/api/bots/restart")
def bots_restart():
    return _todo()


@hub_bp.get("/hub/api/bots/restart/<job_id>")
def bots_restart_status(job_id):
    return _todo()
