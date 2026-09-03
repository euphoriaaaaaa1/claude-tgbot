# -*- coding: utf-8 -*-
"""provider 管理台的纯函数层（INTERFACE §3 / PLAN M4）。

职责：KIND_SPEC 唯一真相表、全部入参校验（含 SSRF 判定）、id 派生、
provider 对象 ↔ cliproxy 承载块互转、settings.json 三键/四键增删的**计算**。

**本模块不做任何文件 IO**（落盘一律走 moments/settings_io.py），
也不发任何 HTTP（管理 API 一律走 moments/cliproxy_client.py）。
唯一的外部依赖是 §3.3 要求的"域名解析一次"，由 `resolve` 参数注入，
默认实现在文件末尾；单测一律注入假解析器，绝不联网。
"""
import hashlib
import ipaddress
import json
import os
import socket
from urllib.parse import urlsplit

# §3.0c：claude CLI 一次会话只请求两个 model id，haiku 名没注册 → 后台摘要请求 502。
DEFAULT_HAIKU_ALIAS = "claude-3-5-haiku-20241022"

# §3.0b + 修订 4 ⑩：S0-B 已实证 claude-api-key 能承载第三方端点，
# `anthropic-direct` 永不启用，route 恒 cliproxy，本模块不写 direct 分支。
ROUTE = "cliproxy"

# settings.json 里由本台账管的四个键（§3.6 形态表 / §5 四键全删）。
ANTHROPIC_ENV_KEYS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                      "ANTHROPIC_MODEL", "ANTHROPIC_API_KEY")

# §3.0 KIND_SPEC —— 唯一真相表：kind → 承载块（= 管理 API 路径段）/ key 落在哪个字段 /
# 该块有没有 `name` 字段。校验、互转、页面下拉三处全部从这张表派生（[RA] 建议2）。
# 块名由 S0-B 真二进制实测确定：anthropic-compatibility / gemini-compatibility 在 v7 不存在。
KIND_SPEC = {
    "openai": {"block": "openai-compatibility",
               "key_field": "api-key-entries", "has_name": True},
    "anthropic": {"block": "claude-api-key",
                  "key_field": "api-key", "has_name": False},
    "gemini": {"block": "gemini-api-key",
               "key_field": "api-key", "has_name": False},
}
SUPPORTED_KINDS = tuple(KIND_SPEC)          # §3.2 由接口自报，实现不得散落硬编码
LABEL_MAX = 40


class HubError(Exception):
    """携带机读错误码与状态码的校验失败（§7 错误契约总表）。

    路由层直接 `return jsonify({"error": e.error, "detail": e.detail}), e.status`。
    `detail` 是人读文案，永不含 key/token 明文。
    """

    def __init__(self, status, error, detail=""):
        super().__init__("%s %s" % (status, error))
        self.status = status
        self.error = error
        self.detail = detail


def _text(v):
    """非字符串一律当"没填"——避免 None / 数字混进下游拼串。"""
    return v.strip() if isinstance(v, str) else ""


def _s(v):
    """把手写 config.yaml 里的非字符串标量降级成字符串（YAML 不加引号的 key/端口就是 int）。

    §3.2 铁律是"列表端点恒 200"，所以读侧一个 TypeError 都不许冒到路由层。
    dict/list 当成没填 —— str() 出来是垃圾，不如空。
    """
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    return ""


def _bad_alias(v):
    """alias 规则（§3.3）：非空、不含 `/`、不含空白。haiku_alias 用完全相同的规则。"""
    return (not v) or ("/" in v) or any(c.isspace() for c in v)


def _pick(body, base, key):
    """PATCH 语义：请求体给了就用请求体的（哪怕是空值，空值该报错就报错），否则取原值。"""
    return body[key] if key in body else base.get(key)


def validate_provider_payload(body, supported_kinds=SUPPORTED_KINDS,
                              allow_private=False, resolve=None, base=None):
    """§3.3 校验表逐行、**先命中先返回**；返回归一化字段（含明文 api_key）。

    `base` 供 §3.4 PATCH：请求体是子集，缺的字段取原条目值；
    `api_key` 省略 = 保持原 key，显式传空串仍是 400 key_required。
    `allow_private` 对应逃生门 HUB_ALLOW_PRIVATE_BASE_URL=1。
    """
    if not isinstance(body, dict):
        raise HubError(400, "bad_body", "请求体必须是 JSON 对象")
    base = base or {}

    kind = _pick(body, base, "kind")
    if kind not in supported_kinds:
        raise HubError(400, "bad_kind", "kind 必须是 %s 之一" % (list(supported_kinds),))

    label = _text(_pick(body, base, "label"))
    if not label or len(label) > LABEL_MAX:
        raise HubError(400, "bad_label", "label 需 1-%d 个字符" % LABEL_MAX)

    api_key = _text(_pick(body, base, "api_key"))
    if not api_key:
        raise HubError(400, "key_required", "api_key 不能为空")

    base_url = _check_base_url(_pick(body, base, "base_url"), allow_private, resolve)

    model_alias = _text(_pick(body, base, "model_alias"))
    if _bad_alias(model_alias):
        raise HubError(400, "bad_alias", "model_alias 不能为空、不能含 / 或空白")

    upstream_model = _text(_pick(body, base, "upstream_model"))
    if not upstream_model:
        raise HubError(400, "bad_upstream_model", "upstream_model 不能为空")

    # §3.0c：省略合法，服务端填默认值；传了就按主 alias 同一套规则校验，且不得与主 alias 同名
    # （同名 = 同一条目里两条一模一样的 model 映射）。
    raw_haiku = _pick(body, base, "haiku_alias")
    if raw_haiku is None and "haiku_alias" not in body:
        haiku_alias = DEFAULT_HAIKU_ALIAS
    else:
        haiku_alias = _text(raw_haiku)
        if _bad_alias(haiku_alias) or haiku_alias == model_alias:
            raise HubError(400, "bad_haiku_alias",
                           "haiku_alias 不能为空、不能含 / 或空白、不能与 model_alias 相同")

    return {"label": label, "kind": kind, "base_url": base_url, "api_key": api_key,
            "upstream_model": upstream_model, "model_alias": model_alias,
            "haiku_alias": haiku_alias}


def _private_ip(ip):
    """内网/回环/链路本地/保留/多播/未指定，任一为真即拒（§3.3 SSRF 收口）。

    额外挡 IPv4-mapped（`::ffff:127.0.0.1` 的 is_loopback 是 False，直接放行等于留后门）。
    """
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return (ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _check_base_url(raw, allow_private=False, resolve=None):
    """§3.3：必须是 http(s) 绝对 URL；host 不得指向内网。返回归一化后的 URL。

    门户是公网 tunnel 可达的，base_url 由用户填、由 localhost 内侧发起请求，
    不拦就等于送一个内网探测器（可打 127.0.0.1:8317/v0/management/*、169.254.169.254）。
    域名只解析一次，承认挡不住 DNS rebinding，挡掉直接利用。
    """
    url = _text(raw)
    if not url:
        raise HubError(400, "bad_base_url", "base_url 必须是 http(s):// 绝对地址")
    try:
        parts = urlsplit(url)               # 方括号不配对（http://[::1）时它自己抛 ValueError
        host = parts.hostname
        parts.port                          # 端口越界（:99999）时在这里抛
    except ValueError:
        raise HubError(400, "bad_base_url", "base_url 不是合法 URL（端口或 IPv6 括号有误）")
    if parts.scheme not in ("http", "https") or not host:
        raise HubError(400, "bad_base_url", "base_url 必须是 http(s):// 绝对地址")
    if allow_private:                       # 逃生门：真在内网自建网关的用户
        return url
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:                      # 域名：解析一次做同样判断，解析不出就放行
        ips = []
        for addr in (resolve or resolve_host)(host):
            try:
                ips.append(ipaddress.ip_address(addr))
            except ValueError:
                continue
    if any(_private_ip(ip) for ip in ips):
        raise HubError(400, "bad_base_url_private",
                       "base_url 指向内网/回环地址；确需如此请设 HUB_ALLOW_PRIVATE_BASE_URL=1")
    return url


def resolve_host(host):
    """默认解析器：只在校验第三方 base_url 时被调一次，失败一律返回空（不因 DNS 挂了拒绝用户）。"""
    try:
        return [info[4][0] for info in socket.getaddrinfo(host, None)]
    except (socket.gaierror, UnicodeError, OSError):
        return []


def derive_id(block, base_url, upstream_model, model_alias):
    r"""§3.1：id 是派生的不是存储的 —— sha1(block|base_url|upstream_model|alias)[:8]。

    不含 haiku_alias（改 haiku 名不换 id）、不含 label（改显示名不换 id）。

    字段自身可能含 `|`（base_url 的 path 与 upstream_model 都没有字符集限制），
    直接 join 会错位：不同四元组拼出同一串 → 同一个 id → PATCH/DELETE 打到别人身上。
    所以先转义 `\` 与 `|` 再拼。**不含这两个字符的字段（现实里的全部条目）id 不变。**
    """
    parts = [f.replace("\\", "\\\\").replace("|", "\\|")
             for f in (block, base_url, upstream_model, model_alias)]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:8]


def provider_id(p):
    """按归一化字段算 id。四元组相同 = 同一条 provider（§3.3 duplicate_provider 的判据）。"""
    return derive_id(KIND_SPEC[p["kind"]]["block"], p["base_url"],
                     p["upstream_model"], p["model_alias"])


def mask_key(key):
    """§0.2 的合规形态：`****`+末 4 位。与 styles_routes._mask 同口径（那个是私有的，
    且所在模块导入 flask + 在导入期算路径，纯函数层不该反向依赖它）。"""
    key = _s(key)                           # 手写 config.yaml 里的纯数字 key 是 int
    return ("****" + key[-4:]) if len(key) >= 4 else ("****" if key else "")


def entry_api_key(kind, entry):
    """从承载块 entry 取明文 key（PATCH 保原 key 用）。取不到返回空串，绝不进日志/响应。"""
    if KIND_SPEC[kind]["key_field"] == "api-key-entries":
        raw = entry.get("api-key-entries")
        entries = [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []
        return _s(entries[0].get("api-key")) if entries else ""
    return _s(entry.get("api-key"))


def to_block_entry(p, disabled=False):
    """provider（已校验字段）→ cliproxy 承载块里的一条 entry。

    §3.0c：models 必须**两条**，name 都是 upstream_model，alias 分别是主名与 haiku 名，
    两条都 force-mapping —— 少一条，claude CLI 的后台摘要请求就 502。
    `disabled=True` 表示"落选"，按 §3.0d 落成 prefix（不是 cliproxy 的 disabled 键）。
    """
    spec = KIND_SPEC[p["kind"]]
    entry = {}
    if spec["has_name"]:
        # openai-compatibility 的 name 会被 cliproxy 小写后当内部 provider key。
        # 用派生 id 而不是 label：label 可含空格/emoji，且改显示名不该动路由键。
        entry["name"] = "hub-" + provider_id(p)
    entry["base-url"] = p["base_url"]
    if spec["key_field"] == "api-key-entries":
        entry["api-key-entries"] = [{"api-key": p["api_key"]}]
    else:
        entry["api-key"] = p["api_key"]
    entry["models"] = [{"name": p["upstream_model"], "alias": alias,
                        "display-name": p["label"], "force-mapping": True}
                       for alias in (p["model_alias"], p["haiku_alias"])]
    # §3.0d：互斥靠 per-entry prefix，三块统一。落选 = prefix 设成自己的 id
    # （配全局 force-model-prefix:true，裸官方名就不会命中它）；当选 = 空串。
    # 禁止再写 cliproxy 的 disabled 键：它只在 openai-compatibility 有，另两块会静默忽略。
    entry["prefix"] = provider_id(p) if disabled else ""
    return entry


def from_block_entry(kind, entry, active=False):
    """cliproxy 承载块 entry → §3.1 的 provider 对象（**没有 api_key 字段**，只有 key_masked）。

    models[0] 当主 alias、models[1] 当 haiku alias（就是 to_block_entry 写下去的顺序）。
    用户手写的单 model 条目 haiku_alias 为 None，页面据此提示"补一条 haiku 映射"。
    """
    raw = entry.get("models")
    models = [m for m in raw if isinstance(m, dict)] if isinstance(raw, (list, tuple)) else []
    main = models[0] if models else {}
    # 全部字段过 _s()：条目可能是用户手写的 config.yaml，YAML 不加引号的值就是 int/float。
    upstream = _s(main.get("name"))
    # cliproxy 口径：alias 为空则回落用 name（service.go:958-963），id 派生跟着用同一口径
    alias = _s(main.get("alias")) or upstream
    base_url = _s(entry.get("base-url"))
    return {"id": derive_id(KIND_SPEC[kind]["block"], base_url, upstream, alias),
            "label": _s(main.get("display-name")) or _s(entry.get("name")) or alias,
            "kind": kind, "base_url": base_url, "upstream_model": upstream,
            "model_alias": alias,
            "haiku_alias": (_s(models[1].get("alias")) or None) if len(models) > 1 else None,
            "key_masked": mask_key(entry_api_key(kind, entry)),
            # §3.0d：disabled 的语义 = "该条目当前带 prefix"。仍兼读用户手写的原生
            # disabled（openai-compatibility 独有）—— 那种条目 cliproxy 真的不路由，
            # 报成 enabled 会让 active 判定说谎。写只写 prefix，读两个都认。
            "disabled": bool(_s(entry.get("prefix"))) or bool(entry.get("disabled")),
            "route": ROUTE, "active": bool(active)}


def alias_occupied(providers, aliases, exclude_id=None):
    """这些 alias 是否已被**别的 enabled 条目**占用（§3.3「新建即 disabled」的判据）。

    唯一性只在运行时层保证（[RA] F2 裁决）：台账层允许重名，S1/S2 的场景本来就是
    多个来源争同一个官方 alias，切到谁才 enable 谁。
    §3.0c 定案：检查范围 = **主 alias ∪ haiku alias 并集**，任一被占即新建为 disabled。
    因 haiku 默认值人人相同，**第二个起的 provider 必然创建即停用**，这是预期行为。
    """
    if aliases is None:
        aliases = []
    elif isinstance(aliases, str):
        aliases = [aliases]
    wanted = {a for a in aliases if a}      # None（手写单 model 条目的 haiku）自动被滤掉
    for p in providers:
        if p.get("disabled") or p.get("id") == exclude_id:
            continue
        if {p.get("model_alias"), p.get("haiku_alias")} & wanted:
            return True
    return False


def settings_path():
    """写哪份 settings.json —— 口径与 dispatcher/provider_watch.ts:23-25 settingsPath() 逐字一致。
    不一致就是"写 A 盯 B"，切换永远不生效。

    ⚠️ CLAUDE_SETTINGS_PATH 只是本项目 writer/watcher 的接缝，**真 claude CLI 不认它**
    （§9.4：起真 CLI 要用 CLAUDE_CONFIG_DIR / 隔离 HOME / --settings）。
    """
    return os.environ.get("CLAUDE_SETTINGS_PATH") or os.path.join(
        os.path.expanduser("~"), ".claude", "settings.json")


def parse_settings(text):
    """§3.6 A2：顶层必须是对象、env 缺失或为对象，否则 409 settings_unparsable。

    文件不存在（text=None）或空文件 → 视为 {}，新建而不是报错（§3.6 末段）。
    """
    if text is None or not text.strip():
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        raise HubError(409, "settings_unparsable", "settings.json 不是合法 JSON")
    if not isinstance(data, dict):
        raise HubError(409, "settings_unparsable", "settings.json 顶层不是 JSON 对象")
    if "env" in data and not isinstance(data["env"], dict):
        # {"env": "x"} 时 data["env"][k] = v 会抛 TypeError → 500（[R4D] I1-5）
        raise HubError(409, "settings_unparsable", "settings.json 的 env 不是对象")
    return data


def apply_cliproxy_env(settings, port, auth_token, model_alias):
    """§3.6 形态一（cliproxy 路由）：三键写入 + 删 ANTHROPIC_API_KEY。其余键与顶层 model 原样保留。

    返回**新对象**，入参不被改（纯函数；调用方失败时手上还是原文）。
    BASE_URL 不带 `/v1`（[CPX] Q2：带了会变成 /v1/v1/messages）。
    """
    data = dict(settings or {})
    env = dict(data.get("env") or {})
    env["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:%d" % int(port)
    env["ANTHROPIC_AUTH_TOKEN"] = auth_token
    env["ANTHROPIC_MODEL"] = model_alias
    env.pop("ANTHROPIC_API_KEY", None)
    data["env"] = env
    return data


def apply_native_env(settings):
    """§5 形态三（Claude 原生订阅）：四键全删，走 claude CLI 自己的 keychain/DPAPI 凭证。"""
    data = dict(settings or {})
    env = dict(data.get("env") or {})
    for k in ANTHROPIC_ENV_KEYS:
        env.pop(k, None)
    data["env"] = env
    return data


def _points_at_cliproxy(base_url, port):
    """settings 里的 BASE_URL 是不是指向本机 cliproxy（判 active 用，容忍 localhost/[::1]/尾斜杠）。"""
    try:
        parts = urlsplit(_text(base_url))
        host, url_port = parts.hostname, parts.port
        want = int(port)                    # port 为 None（cliproxy 没配）时这里就退出
    except (ValueError, TypeError):
        return False
    return host in ("127.0.0.1", "localhost", "::1") and url_port == want


def resolve_active(settings, port, providers):
    """§3.2 `active` 五态判定，返回 (active, warnings)。

    `settings=None` 表示读不出/解析不了（调用方接住 parse_settings 的 HubError 后传 None）——
    列表端点恒 200，故障用 warnings 表达，不抛错。
    `active.kind` 只看主 alias：ANTHROPIC_MODEL 写的就是它（§3.0c，haiku_alias 不参与判定）。
    """
    if settings is None:
        return {"kind": "unknown", "id": None, "alias": None}, ["settings_unparsable"]
    env = settings.get("env") or {}
    # alias 恒取 ANTHROPIC_MODEL 原值（键缺失才 None），none/direct 态也回原值——
    # 页面要能显示"现在到底钉在哪个模型名上"，判不出来源不等于读不出名字。
    alias = env.get("ANTHROPIC_MODEL")
    if not isinstance(alias, str):          # 手写的 settings 里可能是数字/对象/true
        alias = None
    if not any(k in env for k in ANTHROPIC_ENV_KEYS):
        return {"kind": "claude_native", "id": None, "alias": alias}, []
    if not _points_at_cliproxy(env.get("ANTHROPIC_BASE_URL"), port):
        # 用户手写的第三方直连，匹配不上任何 provider（route:"direct" 当前不可达，§3.0b）
        return {"kind": "none", "id": None, "alias": alias}, []
    hit = [p for p in providers if p.get("model_alias") == alias]
    if not hit:
        return {"kind": "cliproxy", "id": None, "alias": alias}, ["active_unknown"]
    live = [p for p in hit if not p.get("disabled")]
    if live:
        return {"kind": "cliproxy", "id": live[0]["id"], "alias": alias}, []
    # 存在却被 disabled：§3.6 补偿失败或用户手改后的不一致态，页面提示"点一次切换即可修复"
    return {"kind": "cliproxy", "id": hit[0]["id"], "alias": alias}, ["alias_disabled"]
