# -*- coding: utf-8 -*-
"""全站唯一的脱敏实现（INTERFACE §0.2）。

两层：
1. 主：字段白名单投影 —— cliproxy 管理 API 的响应先投影成只含安全字段的新对象再返回，
   未列入白名单的字段一律丢弃；字段名像凭据的就地掩成 ``****`` + 末 4 位。
2. 兜底：出站正则 —— 只对 ``/hub/api/*`` 的 ``detail`` / ``tail`` / ``text_head``
   之类自由文本过一遍。**不作为正确性依据**，正确性由第一层保证。

作用域裁决（§0.2b）：老路由（``/``、``/styles``、``/api/*``、``/image/*``）
的响应体逐字节不变，绝不挂全局 after_request。

本模块被 styles_routes / cliproxy_client / hub_routes 共用（[RA] M1：
脱敏是核心能力，不该藏在「生图设置」这个平级功能模块里）。
"""
import re

# 兜底正则：老的三条（pst- / Bearer / Authorization，原 styles_routes._SECRET_RE）
# 加上 §0.2 第二层点名的其余几条。只会比原来抹得多，不会抹得少。
SECRET_RE = re.compile(
    r"sk-ant-[\w-]+"
    r"|sk-[\w-]{6,}"
    r"|pst-[\w-]+"
    r"|AIza[\w-]{20,}"
    r"|ghp_\w+"
    r"|Bearer\s+\S+"
    r"|Authorization[^\n]*"
    r"|x-api-key[^\n]*"
    r"|\d{8,10}:[A-Za-z0-9_-]{30,}",   # Telegram bot token 形状
    re.I,
)

# 字段名像凭据就掩掉（大小写不敏感，含连字符/下划线变体）
_SECRET_FIELD_RE = re.compile(r"key|token|secret|password|credential|auth", re.I)

# URL 里的 userinfo（https://user:pass@host/…）也是凭据。M4 的入参校验已经拒了带 @ 的
# base-url，这里再打一层：投影是出站前的最后一道，纵深防御不差这一行。
_URL_USERINFO_RE = re.compile(r"(//)[^/@\s]+@")

# §0.2 安全字段白名单。顶层为 provider 配置块的字段，MODEL_SAFE_FIELDS 为 models[] 元素的字段。
SAFE_FIELDS = ("name", "base-url", "prefix", "weight", "disabled", "excluded-models", "models")
MODEL_SAFE_FIELDS = ("name", "alias", "display-name", "max-context-length", "force-mapping")


def mask(secret) -> str:
    """凭据的唯一对外形态：``****`` + 末 4 位。空值回空串。"""
    s = "" if secret is None else str(secret)
    return ("****" + s[-4:]) if len(s) >= 4 else ("****" if s else "")


def is_secret_field(name) -> bool:
    return bool(_SECRET_FIELD_RE.search(str(name)))


def scrub_text(text) -> str:
    """出站兜底：把自由文本里的疑似凭据抹成 ``***``。"""
    return SECRET_RE.sub("***", text or "")


def project(block, fields=SAFE_FIELDS, model_fields=MODEL_SAFE_FIELDS) -> dict:
    """把 cliproxy 的一个配置块投影成只含白名单字段的新 dict。

    白名单外的键直接丢弃；命中凭据字段名的（白名单里目前没有，防的是将来加错）掩成末 4 位；
    ``models`` 递归投影。入参不是 dict 时回空 dict —— 上游形状变了也不能把原对象带出去。
    """
    if not isinstance(block, dict):
        return {}
    out = {}
    for k in fields:
        if k not in block:
            continue
        v = block[k]
        if isinstance(v, str) and "@" in v and k.endswith("url"):
            out[k] = _URL_USERINFO_RE.sub(r"\1****@", v)
        elif k == "models":
            out[k] = [project(m, model_fields, model_fields) for m in v] if isinstance(v, list) else []
        elif is_secret_field(k):
            out[k] = mask(v)
        else:
            out[k] = v
    return out


# 第二层兜底只作用于这几个"自由文本"字段（§0.2）：它们由上游报错拼出来，形状不可控。
FREE_TEXT_FIELDS = ("detail", "tail", "text_head", "stderr")


def scrub_fields(obj, fields=FREE_TEXT_FIELDS):
    """递归把 JSON 结构里自由文本字段的疑似凭据抹掉。返回 (新对象, 是否改动过)。"""
    changed = False
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in fields and isinstance(v, str):
                nv = scrub_text(v)
                changed = changed or nv != v
                out[k] = nv
            else:
                out[k], c = scrub_fields(v, fields)
                changed = changed or c
        return out, changed
    if isinstance(obj, list):
        out = []
        for v in obj:
            nv, c = scrub_fields(v, fields)
            changed = changed or c
            out.append(nv)
        return out, changed
    return obj, False
