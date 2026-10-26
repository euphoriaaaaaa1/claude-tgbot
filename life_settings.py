"""生活设定（BRIEF-life-settings §4）：字段表、校验、读配置、按顶层键文本块写回 ``configs/<bot>.yml``。

只被 ``adapters/claude_local.py`` 导入；只用 PyYAML 与标准库。配置目录与 bot_id 校验走 ``bots_registry`` / ``adapters.base`` 的公开接缝。

写回只替换被改的顶层键的文本块（``^键名:`` 那一行到下一个顶层键之前，块尾的空行与注释行留在原处），
文件其余部分逐字节不变；不整份重新序列化。写入顺序：文件锁 → 读原文 → 生成新文 → 解析校验（能解析、
被改的键等于期望值、没被改的键深度相等）→ 备份 → 临时文件 + 原子改名。任何一步不过就不写、不留临时文件。
本模块不生成、不改写任何文字内容；日志里不记字段的值。
"""
import contextlib
import datetime
import json
import logging
import math
import os
import re
import shutil
import stat
import tempfile
import time

import yaml

import bots_registry
import platform_lock
import config_loader

_log = logging.getLogger(__name__)

SAVED_MESSAGE = "已保存，下次判断要不要主动说话时生效"
LOCK_WAIT_SEC = 3.0
LOCK_RETRY_SEC = 0.05
BACKUP_KEEP = 2   # 用户 2026-10-15：备份一律只留 2 份
# 字段表 empty_hint 的原文（pub-draft 接口第二节）：页面上带它的项没填就显示；页面只看 empty_hint，不认键名
PROACTIVE_EMPTY_HINT = "人设摘要或「什么情况下主动说话」没填时，bot 会很少主动找你。"
# 睡觉时段为空时 life-context 的睡眠拦截不起作用
SLEEP_EMPTY_HINT = "睡觉时段没填时，bot 夜里也可能主动找你。"
# 对应嵌套子键的字段：字段键 → (顶层键, 子键)。每个顶层键在这里最多出现一次（写回时按改前的配置算新块）
NESTED_FIELDS = {"jiwen_delta_hints": ("jiwen", "delta_hints")}
# 注册用的键：永远不写、请求里出现即 invalid（BRIEF §4.3）
REGISTRATION_KEYS = frozenset({"id", "chat_id", "dispatcher_port", "bot_channel_path", "namespace_uuid",
                               "display_name", "enabled"})
# 事件类别全集来自突发事件池 events.yaml 里出现过的 category（generators/situation.py 按它启用事件）
EVENTS_PATH = os.path.join(config_loader.PROJECT_ROOT, "events.yaml")
# 爱好的活动类型全集：jiwen/engine.py 的 Rates.immersion_map 与 life-context.py 的 _ACTIVITY_KEYWORDS 都是这六个
ACTIVITY_TYPES = ["reading", "cooking", "browse", "search", "observe", "selfcare"]
RECURRING_GROUPS = [("weekday", "工作日"), ("weekend", "周末"), ("break", "假期"), ("early_return", "提前返校")]
# 表外子项 / 表外分组（如现有数据里的 prefer_channel、interruptible）原样往返，这是它们的大小上限
OPAQUE_MAX_DEPTH = 3
OPAQUE_MAX_ITEMS = 200
OPAQUE_MAX_LEN = 4000
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HHMM_RANGE_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_MMDD_RE = re.compile(r"^(\d{2})-(\d{2})$")
# 顶层键行：行首不缩进、键名后紧跟冒号与空白（或行尾）。注释、空行、以 - ? 引号等开头的行都不是键。
_KEY_LINE_RE = re.compile(r"^([^\s#\-?:,\[\]{}&*!|>'\"%@`][^:]*?)\s*:(?:\s|$)")
_REMOVE = object()   # 「把这个键从文件里去掉」


class LifeSettingsError(Exception):
    """code ∈ unknown_bot / invalid / busy / internal；invalid 时 fields = {键: 这一项哪里不对}。"""

    def __init__(self, code: str, message: str, fields=None):
        super().__init__(message)
        self.code, self.message = code, message
        self.fields = dict(fields) if fields else {}


class _Bad(Exception):
    """某一项校验不过；message 给人看。"""


# ── 字段表 ─────────────────────────────────────────────────────────────────

def _f(key, label, type_, help_, **extra):
    d = {"key": key, "label": label, "help": help_, "type": type_}
    d.update(extra)
    return d


def event_categories() -> list:
    """生活引擎认识的事件类别全集：events.yaml 里出现过的 category（去重、按名排序）。读不到 → []（不限取值）。"""
    try:
        with open(EVENTS_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError, UnicodeDecodeError):
        return []
    out = set()
    for e in data if isinstance(data, list) else []:
        c = e.get("category") if isinstance(e, dict) else None
        if isinstance(c, str) and c:
            out.add(c)
    return sorted(out)


def field_table() -> list:
    """BRIEF §4.1 的字段表；顺序就是页面显示顺序。每次调用重算（事件类别全集读自 events.yaml）。"""
    cats = event_categories()
    cat_extra = {"options": cats, "max_items": len(cats)} if cats else {"max_items": 60}
    hobby_items = [
        _f("name", "名字", "text", "必填", max_len=40),
        _f("probability_per_roll", "出现概率", "number", "每次判断时被选中的可能，0 到 1 之间", min=0, max=1),
        _f("effect", "做这件事时的状态", "textarea", "会原样放进提示词", max_len=500),
        _f("activity_type", "活动类型", "text", "生活引擎认识的六种之一，可以不填", options=list(ACTIVITY_TYPES),
           max_len=40),
    ]
    recurring_items = [
        _f("name", "名字", "text", "", max_len=500),
        _f("when", "时段", "text", "起止时间，可以跨过午夜", placeholder="08:00-12:00", max_len=500),
        _f("state", "状态", "text", "这段时间是空闲还是在忙", max_len=500),
        _f("description", "描述", "textarea", "会原样放进提示词", max_len=500),
    ]
    calendar_items = [
        _f("summer_start", "暑假开始", "text", "月和日，用短横线连接", placeholder="MM-DD", max_len=5),
        _f("summer_end", "暑假结束", "text", "月和日，用短横线连接", placeholder="MM-DD", max_len=5),
        _f("early_return", "提前返校", "text", "月和日，用短横线连接", placeholder="MM-DD", max_len=5),
        _f("winter_days", "寒假天数", "number", "0 到 60 的整数", min=0, max=60),
    ]
    return [
        _f("bio", "一句话简介", "text", "介绍这只 bot 的一句话", max_len=200),
        _f("birthday", "生日", "text", "按年、月、日的顺序写，中间用短横线连接", placeholder="YYYY-MM-DD", max_len=10),
        _f("city", "所在城市", "text", "这只 bot 住在哪座城市", max_len=40),
        _f("user_name", "怎么称呼用户（名字）", "text", "这只 bot 记得的用户名字", max_len=40),
        _f("user_address", "怎么称呼用户（称呼）", "text", "这只 bot 平时怎么叫用户", max_len=40),
        _f("persona_summary", "人设摘要", "textarea", "性格、背景与说话方式的概括，会原样放进提示词", max_len=4000,
           empty_hint=PROACTIVE_EMPTY_HINT),
        _f("speaking_threshold", "什么情况下主动说话", "textarea", "写清楚什么时候该主动开口、什么时候别打扰",
           max_len=4000, empty_hint=PROACTIVE_EMPTY_HINT),
        _f("jiwen_delta_hints", "思念值变化规则", "textarea",
           "这只 bot 的思念值在什么情形下升高、什么情形下回落，会原样放进判断思念值变化的提示词", max_len=4000),
        _f("sleep_hours", "睡觉时段", "string_list", "每项是一个起止时段，可以跨过午夜", placeholder="00:30-06:30",
           max_items=6, max_len=11, empty_hint=SLEEP_EMPTY_HINT),
        _f("interest_keywords", "关注的话题", "string_list", "平时关注的话题，每项一个", max_items=60, max_len=40),
        _f("rss_feeds", "订阅的新闻源", "string_list", "每项是一个新闻源的完整网址", placeholder="https://",
           max_items=30, max_len=500),
        _f("event_categories", "会遇到的事件类别", "string_list", "只有选中的类别里的突发事件才会发生", **cat_extra),
        _f("hobby_cooldown_hours", "同一个爱好隔多久再出现（小时）", "number", "同一个爱好触发后至少隔这么久才会再出现",
           min=0, max=168),
        _f("personal_hobbies", "爱好", "object_list", "空闲时可能去做的事", max_items=40, item_fields=hobby_items),
        _f("recurring_activities", "日常安排", "object_list_map", "按工作日、周末、假期和提前返校分别写一天的安排",
           max_items=40, groups=[{"key": k, "label": v} for k, v in RECURRING_GROUPS], item_fields=recurring_items),
        _f("school_calendar", "校历", "object", "暑假的起止、提前返校的日子和寒假的天数", item_fields=calendar_items),
    ]


def _empty(type_):
    if type_ in ("text", "textarea"):
        return ""
    if type_ == "number":
        return None
    if type_ in ("string_list", "object_list"):
        return []
    return {}


# ── 读 ─────────────────────────────────────────────────────────────────────

def _config_path(bot_id: str) -> str:
    # 公开版：bot 编号校验与配置目录都走 bots_registry 的同一接缝（CLAUDEBOT_CONFIG_DIR）。
    from adapters.base import valid_bot_id
    if not valid_bot_id(bot_id):
        raise LifeSettingsError("unknown_bot", "这个底层没有这只 bot")
    return os.path.join(str(bots_registry.configs_dir()), "%s.yml" % bot_id)


def _read(path: str):
    """返回 (原文, 解析结果)。不存在 → unknown_bot；解析不了 / 顶层不是映射 → internal。"""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
    except FileNotFoundError:
        raise LifeSettingsError("unknown_bot", "这个底层没有这只 bot")
    except (OSError, UnicodeDecodeError):
        raise LifeSettingsError("internal", "配置文件读不出来")
    try:
        cfg = yaml.safe_load(text)
    except yaml.YAMLError:
        raise LifeSettingsError("internal", "配置文件解析不了，请先手工修好")
    if cfg is None:
        cfg = {}
    if not isinstance(cfg, dict):
        raise LifeSettingsError("internal", "配置文件顶层不是键值表，请先手工修好")
    return text, cfg


def _scalar_text(raw):
    """文件里的标量 → 页面用的文字；日期按 ISO 写法。不是标量 → None（调用方给空值）。"""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, (int, float)):
        return str(raw)
    if isinstance(raw, (datetime.date, datetime.datetime)):
        return raw.isoformat()
    return None


def _scalar_number(raw):
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return raw
    if isinstance(raw, str):
        try:
            v = float(raw)
        except ValueError:
            return None
        return int(v) if v.is_integer() else v
    return None


def _display_item(sub: dict, raw):
    t = sub["type"]
    if t == "number":
        return _scalar_number(raw)
    v = _scalar_text(raw)
    return "" if v is None else v


def _display_row(item_fields: list, raw):
    """一行（object_list 的元素 / object 本身）：表内子项按类型取值，表外子项原样带上。"""
    if not isinstance(raw, dict):
        return None
    known = {s["key"]: s for s in item_fields}
    row = {}
    for k, v in raw.items():
        sub = known.get(k)
        row[k] = _display_item(sub, v) if sub else v
    return row


def _display_value(field: dict, raw):
    t = field["type"]
    if raw is None:
        return _empty(t)
    if t in ("text", "textarea"):
        v = _scalar_text(raw)
        return "" if v is None else v
    if t == "number":
        return _scalar_number(raw)
    if t == "string_list":
        items = raw if isinstance(raw, list) else [raw]
        return [s for s in (_scalar_text(x) for x in items) if s is not None]
    if t == "object_list":
        if not isinstance(raw, list):
            return []
        return [r for r in (_display_row(field["item_fields"], x) for x in raw) if r is not None]
    if t == "object_list_map":
        if not isinstance(raw, dict):
            return {}
        groups = {g["key"] for g in field["groups"]}
        out = {}
        for g, items in raw.items():
            if g in groups:
                out[g] = [r for r in (_display_row(field["item_fields"], x) for x in (items or []))
                          if r is not None] if isinstance(items, (list, type(None))) else []
            else:
                out[g] = items   # 表外分组原样带上
        return out
    if t == "object":
        return _display_row(field["item_fields"], raw) or {}
    return raw


def _raw_value(cfg: dict, key: str):
    """字段在配置里的原值；嵌套字段取子键（父键不是键值表就当没有）。"""
    if key not in NESTED_FIELDS:
        return cfg.get(key)
    top, sub = NESTED_FIELDS[key]
    parent = cfg.get(top)
    return parent.get(sub) if isinstance(parent, dict) else None


def get_life(bot_id: str) -> dict:
    """``{"fields": [...], "values": {...}, "display_name": "..."}``；没有的键给该类型的空值。"""
    path = _config_path(bot_id)
    _text, cfg = _read(path)
    fields = field_table()
    values = {f["key"]: _display_value(f, _raw_value(cfg, f["key"])) for f in fields}
    name = cfg.get("display_name")
    return {"fields": fields, "values": values,
            "display_name": name if isinstance(name, str) and name else bot_id}


# ── 校验（返回规整后的值；空值统一为 _empty(type)）────────────────────────────

def _check_str(value, sub: dict, single_line: bool) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise _Bad("要是文字")
    if _CONTROL_RE.search(value):
        raise _Bad("含有不可见的控制字符")
    if single_line and ("\n" in value or "\r" in value):
        raise _Bad("不能换行")
    max_len = sub.get("max_len")
    if max_len is not None and len(value) > max_len:
        raise _Bad("不能超过 %d 字" % max_len)
    return value


def _check_number(value, sub: dict, integer: bool):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _Bad("要是数字")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _Bad("要是数字")
        if integer:
            if not value.is_integer():
                raise _Bad("要是整数")
            value = int(value)
    lo, hi = sub.get("min"), sub.get("max")
    if (lo is not None and value < lo) or (hi is not None and value > hi):
        raise _Bad("要在 %s 到 %s 之间" % (lo, hi))
    return value


def _check_options(value: str, sub: dict) -> None:
    options = sub.get("options")
    if options and value not in options:
        raise _Bad("只能是 %s 之一" % "、".join(options))


def _check_date(value: str) -> None:
    m = _DATE_RE.match(value)
    try:
        if not m:
            raise ValueError
        datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        raise _Bad("要写成年-月-日，且是真实存在的日期")


def _check_mmdd(value: str) -> None:
    m = _MMDD_RE.match(value)
    try:
        if not m:
            raise ValueError
        datetime.date(2000, int(m.group(1)), int(m.group(2)))   # 2000 是闰年，02-29 也算真实存在
    except ValueError:
        raise _Bad("要写成月-日，且是真实存在的日子")


def _check_opaque(value, depth=0):
    """表外子项 / 表外分组：原样往返，只限大小与深度，防把配置文件撑爆。"""
    if value is None or isinstance(value, (bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise _Bad("不认识的子项里有不是数字的值")
        return value
    if isinstance(value, str):
        if len(value) > OPAQUE_MAX_LEN or _CONTROL_RE.search(value):
            raise _Bad("不认识的子项太长或含有控制字符")
        return value
    if depth >= OPAQUE_MAX_DEPTH:
        raise _Bad("不认识的子项嵌套太深")
    if isinstance(value, list):
        if len(value) > OPAQUE_MAX_ITEMS:
            raise _Bad("不认识的子项条数太多")
        return [_check_opaque(x, depth + 1) for x in value]
    if isinstance(value, dict):
        if len(value) > OPAQUE_MAX_ITEMS:
            raise _Bad("不认识的子项条数太多")
        return {str(k): _check_opaque(v, depth + 1) for k, v in value.items()}
    raise _Bad("不认识的子项是不支持的类型")


_INTEGER_NUMBERS = {"hobby_cooldown_hours", "winter_days"}


def _check_sub(sub: dict, value):
    """一个子项（item_fields 里的）按类型校验，返回规整值；空的返回 _empty(type)。"""
    t = sub["type"]
    if t == "number":
        return _check_number(value, sub, sub["key"] in _INTEGER_NUMBERS)
    v = _check_str(value, sub, single_line=(t == "text"))
    if v == "":
        return ""
    if sub["key"] in ("summer_start", "summer_end", "early_return"):
        _check_mmdd(v)
    _check_options(v, sub)
    return v


def _is_empty(v) -> bool:
    return v is None or v == "" or v == [] or v == {}


def _check_row(item_fields: list, raw, where: str) -> dict:
    """一行：表内子项按类型校验，空子项不写；表外子项原样带上。返回规整后的行（可能为空 {}）。"""
    if not isinstance(raw, dict):
        raise _Bad("%s不是一组子项" % where)
    known = {s["key"]: s for s in item_fields}
    row = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            raise _Bad("%s的子项名不是文字" % where)
        sub = known.get(k)
        if sub is None:
            if not _is_empty(v):
                row[k] = _check_opaque(v)
            continue
        try:
            nv = _check_sub(sub, v)
        except _Bad as e:
            raise _Bad("%s的「%s」%s" % (where, sub["label"], e))
        if not _is_empty(nv):
            row[k] = nv
    return row


def _check_rows(field: dict, raw, where: str) -> list:
    if not isinstance(raw, list):
        raise _Bad("%s要是列表" % where)
    if len(raw) > field["max_items"]:
        raise _Bad("%s最多 %d 行" % (where, field["max_items"]))
    out = []
    for i, item in enumerate(raw, 1):
        row = _check_row(field["item_fields"], item, "%s第 %d 行" % (where, i))
        if field["key"] == "personal_hobbies" and not row.get("name"):
            raise _Bad("%s第 %d 行的「名字」必填" % (where, i))
        if not row:
            raise _Bad("%s第 %d 行没有填任何内容" % (where, i))
        out.append(row)
    return out


def _check_string_list(field: dict, raw) -> list:
    if not isinstance(raw, list):
        raise _Bad("要是列表")
    if len(raw) > field["max_items"]:
        raise _Bad("最多 %d 项" % field["max_items"])
    key = field["key"]
    out = []
    for i, item in enumerate(raw, 1):
        try:
            v = _check_str(item, field, single_line=True)
            if v == "":
                raise _Bad("是空的")
            if key == "sleep_hours" and not _HHMM_RANGE_RE.match(v):
                raise _Bad("要写成 HH:MM-HH:MM")
            if key == "rss_feeds":
                if not v.startswith(("http://", "https://")):
                    raise _Bad("要以 http:// 或 https:// 开头")
                if re.search(r"\s", v):
                    raise _Bad("网址里不能有空白")
            _check_options(v, field)
        except _Bad as e:
            raise _Bad("第 %d 项%s" % (i, e))
        out.append(v)
    return out


def _check_value(field: dict, raw):
    """顶层字段：返回规整值；清空的返回 _empty(type)。"""
    t, key = field["type"], field["key"]
    if raw is None:
        return _empty(t)
    if t in ("text", "textarea"):
        v = _check_str(raw, field, single_line=(t == "text"))
        if v and key == "birthday":
            _check_date(v)
        return v
    if t == "number":
        return _check_number(raw, field, key in _INTEGER_NUMBERS)
    if t == "string_list":
        return _check_string_list(field, raw)
    if t == "object_list":
        return _check_rows(field, raw, "")
    if t == "object_list_map":
        if not isinstance(raw, dict):
            raise _Bad("要是按分组的键值表")
        labels = dict(RECURRING_GROUPS)
        out = {}
        for g, items in raw.items():
            if not isinstance(g, str):
                raise _Bad("分组名不是文字")
            if g in labels:
                rows = _check_rows(field, [] if items is None else items, "%s" % labels[g])
                if rows:
                    out[g] = rows
            elif not _is_empty(items):
                out[g] = _check_opaque(items)   # 表外分组原样往返
        return out
    if t == "object":
        return _check_row(field["item_fields"], raw, "")
    raise _Bad("不支持的字段类型")


def validate(values: dict) -> dict:
    """校验请求里的字段子集。全部通过 → {键: 规整值 | _REMOVE}；否则抛 invalid，fields 里逐键给原因。"""
    if not isinstance(values, dict):
        raise LifeSettingsError("invalid", "values 必须是键值表")
    table = {f["key"]: f for f in field_table()}
    errors, clean = {}, {}
    for key, raw in values.items():
        key_s = key if isinstance(key, str) else str(key)
        if key_s in REGISTRATION_KEYS:
            errors[key_s] = "注册用的键不能在这里改"
            continue
        field = table.get(key_s)
        if field is None:
            errors[key_s] = "不认识的字段"
            continue
        try:
            v = _check_value(field, raw)
        except _Bad as e:
            errors[key_s] = str(e)
            continue
        clean[key_s] = _REMOVE if _is_empty(v) else v
    if errors:
        raise LifeSettingsError("invalid", "有 %d 项没通过校验" % len(errors), errors)
    return clean


# ── 写 ─────────────────────────────────────────────────────────────────────

class _Dumper(yaml.SafeDumper):
    """块风格、列表项缩进两格（与手写配置一致）、多行文字用 ``|`` 字面块。"""

    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def _represent_str(dumper, value):
    # 多行且不以两个换行结尾才用 |：|+ 会把块后面保留的空行也吸进值里。其余情形交给 PyYAML 自己选，
    # 它会在明文会被解析成别的类型（如 2000-01-01、yes、1e3）或含特殊字符时自动加引号。
    style = "|" if ("\n" in value and value.strip() and not value.endswith("\n\n")) else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _represent_str)

# YAML 双引号字符串里不能原样出现、或会被读成换行的字符：DEL 与 C1 控制字符（含 NEL）、行分隔符、段分隔符、
# 落单的代理字符、两个非字符。U+0000-U+001F 已由 JSON 转义处理；这几类再写成 \uXXXX（也是 JSON 认的转义），读回逐字相同。
_DOUBLE_QUOTED_ESCAPE_RE = re.compile(r"[\x7f-\x9f\u2028\u2029\ud800-\udfff\ufffe\uffff]")


class DoubleQuoted(str):
    """rewrite 遇到这种值时固定写成一行 YAML 双引号字符串（按 JSON 字符串规则转义），不交给 PyYAML 选写法；
    值就是字符串本身，check_rewrite 照常按字符串比较。显示名用（INTERFACE-bot-lifecycle 3.2.2）。"""


def double_quoted(value: str) -> str:
    return _DOUBLE_QUOTED_ESCAPE_RE.sub(lambda m: "\\u%04x" % ord(m.group()), json.dumps(str(value), ensure_ascii=False))


def _render_block(key: str, value) -> str:
    if isinstance(value, DoubleQuoted):
        return "%s: %s\n" % (key, double_quoted(value))
    return yaml.dump({key: value}, Dumper=_Dumper, allow_unicode=True, default_flow_style=False,
                     sort_keys=False, width=10 ** 6)


def _is_blank_or_comment(line: str) -> bool:
    s = line.strip()
    return s == "" or s.startswith("#")


def _key_spans(lines: list) -> list:
    """[(键, 起始行, 结束行)]：块从键行到下一个顶层键之前，去掉块尾的空行与注释行（它们留在原处）。"""
    starts = []
    for i, line in enumerate(lines):
        m = _KEY_LINE_RE.match(line)
        if m:
            starts.append((i, m.group(1).strip()))
    spans = []
    for n, (i, key) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(lines)
        while end > i + 1 and _is_blank_or_comment(lines[end - 1]):
            end -= 1
        spans.append((key, i, end))
    return spans


def rewrite(text: str, changes: dict) -> str:
    """按顶层键文本块替换 / 删除 / 追加；只动 changes 里的键。只按 ``\\n`` 分行（不认 U+2028 之类）。"""
    lines = text.split("\n")
    out, pos, done = [], 0, set()
    for key, start, end in _key_spans(lines):
        if key not in changes:
            continue
        out.extend(lines[pos:start])
        if key not in done:   # 同一键重复出现：第一处放新块，后面的删掉
            done.add(key)
            if changes[key] is not _REMOVE:
                out.extend(_render_block(key, changes[key]).rstrip("\n").split("\n"))
        pos = end
    out.extend(lines[pos:])
    for key, value in changes.items():
        if key in done or value is _REMOVE:
            continue
        if out and out[-1] == "":   # 原文以换行结尾：新块插在结尾换行之前；没有结尾换行就先补一个
            out.pop()
        out.extend(_render_block(key, value).rstrip("\n").split("\n"))
        out.append("")
    return "\n".join(out)


def check_rewrite(new_text: str, old_cfg: dict, changes: dict) -> None:
    """新文必须：能解析成映射；被改的键等于期望值（删除的键不在）；没被改的键与原来深度相等。不过就抛 internal。"""
    try:
        new_cfg = yaml.safe_load(new_text)
    except yaml.YAMLError:
        raise LifeSettingsError("internal", "写回校验没通过（新文解析不了），文件没有改动")
    if new_cfg is None:
        new_cfg = {}
    if not isinstance(new_cfg, dict):
        raise LifeSettingsError("internal", "写回校验没通过（新文顶层不是键值表），文件没有改动")
    for key, value in changes.items():
        if value is _REMOVE:
            if key in new_cfg:
                raise LifeSettingsError("internal", "写回校验没通过（该清空的键还在），文件没有改动")
        elif key not in new_cfg or new_cfg[key] != value:
            raise LifeSettingsError("internal", "写回校验没通过（被改的键与期望不符），文件没有改动")
    for key in set(old_cfg) | set(new_cfg):
        if key in changes:
            continue
        if key not in old_cfg or key not in new_cfg or old_cfg[key] != new_cfg[key]:
            raise LifeSettingsError("internal", "写回校验没通过（没被改的键变了），文件没有改动")


@contextlib.contextmanager
def _locked(path: str):
    """``<bot>.yml.life.lock`` 上的跨平台锁（T03 platform_lock）；等不到 LOCK_WAIT_SEC → busy。
    锁文件不删（删了会与别人的锁错开）。只把加锁阶段的失败翻成 LifeSettingsError，
    yield 里的读写异常照原样抛。"""
    lock_path = path + ".life.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        raise LifeSettingsError("internal", "拿不到配置文件的锁")
    try:
        deadline = time.monotonic() + LOCK_WAIT_SEC
        try:
            got = platform_lock.acquire(fd, deadline=deadline, poll=LOCK_RETRY_SEC)
        except platform_lock.LockUnsupported:
            raise LifeSettingsError("internal", "拿不到配置文件的锁")
        if not got:
            raise LifeSettingsError("busy", "别的操作正在改这只 bot 的设定，请稍后再试")
        try:
            yield
        finally:
            platform_lock.release(fd)
    finally:
        os.close(fd)

def _backup(path: str, mode: int) -> None:
    """备份为 ``<bot>.yml.bak-life-<时间戳>``（权限同原文件），只留最近 BACKUP_KEEP 份。"""
    now = time.time()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + "-%06d" % int((now % 1) * 1_000_000)
    base = "%s.bak-life-%s" % (path, stamp)
    bak, n = base, 0
    while os.path.lexists(bak):
        n += 1
        bak = "%s.%d" % (base, n)
    shutil.copyfile(path, bak)
    os.chmod(bak, mode)
    d, prefix = os.path.dirname(path), os.path.basename(path) + ".bak-life-"
    names = sorted(n for n in os.listdir(d) if n.startswith(prefix))
    for old in names[:-BACKUP_KEEP]:
        with contextlib.suppress(OSError):
            os.unlink(os.path.join(d, old))


def _write_atomic(path: str, new_text: str, mode: int) -> None:
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".%s.life-tmp-" % os.path.basename(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(new_text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        dfd = os.open(d, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def _nested_parent(cfg: dict, top: str, sub: str, value):
    """子键 sub 改成 value（_REMOVE 即删掉）之后顶层键 top 应有的值：键值表；删完空了是 _REMOVE；本来就没有、
    不用改是 None。top 原来是别的类型（列表、文字等）时不替人覆盖，抛 internal。"""
    parent = cfg.get(top)
    if value is _REMOVE:
        if not isinstance(parent, dict) or sub not in parent:
            return None
        rest = {k: v for k, v in parent.items() if k != sub}
        return rest if rest else _REMOVE
    if parent is not None and not isinstance(parent, dict):
        raise LifeSettingsError("internal", "配置里的 %s 不是键值表，请先手工修好" % top)
    new = dict(parent or {})
    new[sub] = value
    return new


def _splice_sub(text: str, top: str, sub: str, value):
    """只替换顶层键 top 的块里子键 sub 那几行（value 为 _REMOVE 即删掉），块里其他行（含注释）原样；原来没有 sub
    就接在块的最后一行内容之后。子键块的边界同 _key_spans：块尾的空行与注释行留在原处。块不是「键行 + 统一缩进的
    子键」这种写法（值写在键行上、顶层键或子键重复、缩进不齐、用制表符缩进等）→ None。结果由调用方校验。"""
    lines = text.split("\n")
    spans = [(s, e) for k, s, e in _key_spans(lines) if k == top]
    if len(spans) != 1:
        return None
    start, end = spans[0]
    rest = lines[start][_KEY_LINE_RE.match(lines[start]).end():].strip()
    if rest and not rest.startswith("#"):   # 值写在键行上：{...}、&锚点、null 等
        return None
    indent, subs = None, []   # subs：[(子键, 行号)]
    for i in range(start + 1, end):
        line = lines[i]
        if _is_blank_or_comment(line):
            continue
        n = len(line) - len(line.lstrip(" "))
        if indent is None:
            indent = n
        if n == 0 or n < indent or line[n] == "\t":
            return None
        if n == indent:
            m = _KEY_LINE_RE.match(line[n:])
            if not m:
                return None   # 与子键同一缩进却不是键（如列表项）
            subs.append((m.group(1).strip(), i))
    hits = [j for j, (k, _i) in enumerate(subs) if k == sub]
    if indent is None or len(hits) > 1:
        return None
    new = [] if value is _REMOVE else [
        (" " * indent + ln) if ln else ln for ln in _render_block(sub, value).rstrip("\n").split("\n")]
    if not hits:
        return "\n".join(lines[:end] + new + lines[end:])
    s_start = subs[hits[0]][1]
    s_end = subs[hits[0] + 1][1] if hits[0] + 1 < len(subs) else end
    while s_end > s_start + 1 and _is_blank_or_comment(lines[s_end - 1]):
        s_end -= 1
    return "\n".join(lines[:s_start] + new + lines[s_end:])


def _block_comments(text: str, top: str) -> bool:
    """顶层键 top 周围有没有人写的注释：键行尾注、块内注释行、紧贴在键行上方的空行/注释行里的注释。
    写法认不出、要退回整块重写时，用它判断重写会不会把注释弄丢。"""
    lines = text.split("\n")
    spans = [(s, e) for k, s, e in _key_spans(lines) if k == top]
    if not spans:
        return False
    start, end = spans[0]
    m = _KEY_LINE_RE.match(lines[start])
    if m and lines[start][m.end():].strip().startswith("#"):
        return True
    for line in lines[start + 1:end]:
        if line.strip().startswith("#"):
            return True
    i = start - 1
    while i >= 0 and _is_blank_or_comment(lines[i]):
        if lines[i].strip().startswith("#"):
            return True
        i -= 1
    return False


def _rewrite_nested(text: str, cfg: dict, top: str, sub: str, value, parent) -> str:
    """把顶层键 top 的块改成 parent（_REMOVE 即整块删掉）；text 要与 cfg 对应。top 原来就是键值表时先只改 sub 那几行，
    保住块里的注释与其他子键的写法；认不出写法或改完校验不过时，块周围没有注释才退回整块重写（值不变，写法与
    引号会重排），有注释就拒绝保存，宁可不改也不把用户的注释弄丢。"""
    if parent is not _REMOVE and isinstance(cfg.get(top), dict):
        spliced = _splice_sub(text, top, sub, value)
        if spliced is not None:
            try:
                check_rewrite(spliced, cfg, {top: parent})
            except LifeSettingsError:
                pass
            else:
                return spliced
        if _block_comments(text, top):
            raise LifeSettingsError("internal", "%s 段的写法认不出且含注释，为避免丢注释没有改动" % top)
        _log.info("%s 段的写法认不出，改为整块重写", top)
    return rewrite(text, {top: parent})


def set_life(bot_id: str, values: dict) -> dict:
    """校验 → 锁内按块写回。校验不过一个字都不写。返回 BRIEF §3.2 的成功形状。"""
    path = _config_path(bot_id)
    _read(path)   # 先确认 bot 存在、文件能解析：unknown_bot 要排在 invalid 之前
    changes = validate(values)
    if not changes:
        return {"applied": True, "queued": False, "message": SAVED_MESSAGE}
    with _locked(path):
        text, cfg = _read(path)   # 锁内重读：锁外那次之后文件可能已被别人改过
        top_changes = {k: v for k, v in changes.items()
                       if k not in NESTED_FIELDS and not (v is _REMOVE and k not in cfg)}
        expected, new_text = dict(top_changes), text
        # 嵌套子键先改（这时文字与 cfg 还对得上，逐行改完能单独校验），顶层键再按块替换；两边的顶层键不重叠
        for key, (top, sub) in NESTED_FIELDS.items():
            parent = _nested_parent(cfg, top, sub, changes[key]) if key in changes else None
            if parent is not None:
                new_text = _rewrite_nested(new_text, cfg, top, sub, changes[key], parent)
                expected[top] = parent
        if expected:
            new_text = rewrite(new_text, top_changes)
            check_rewrite(new_text, cfg, expected)
            try:
                mode = stat.S_IMODE(os.stat(path).st_mode)
                _backup(path, mode)
                _write_atomic(path, new_text, mode)
            except OSError:
                _log.exception("生活设定写回失败 bot=%s", bot_id)
                raise LifeSettingsError("internal", "配置文件写不进去，文件没有改动")
    _log.info("生活设定已保存 bot=%s keys=%s", bot_id, ",".join(sorted(changes)))
    return {"applied": True, "queued": False, "message": SAVED_MESSAGE}
