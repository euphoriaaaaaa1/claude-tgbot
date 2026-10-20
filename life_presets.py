"""生活设定预置清单（PLAN-bot-lifecycle D10；INTERFACE-bot-lifecycle 3.2.4、3.6）：读取、逐项校验、按字段组织输出。

只认一份：``<CLAUDEBOT_CONFIG_DIR，缺省 仓库/configs>/life_presets.json``，路径写死，没有切换位置的配置项；每次调用现读，
改清单不用重启。打开时不跟随软链：软链、不是普通文件、超过 256 KiB、不是 UTF-8 JSON 对象、``version`` 不是整数 1、
``fields`` 不是对象 → ``invalid``；文件不存在 → ``missing``。

每个条目用与生活设定 PUT 时相同的校验（复用 life_settings 的字段表与校验函数），另加预置自己的规则（对象条目的键只能取
字段表 item_fields 里的、``name`` 必填；整套的 ``id`` 合 ``[a-z0-9_]{1,32}`` 且不重复；同一字段里识别键重复的，后出现的
跳过）。不过的条目跳过并记进 ``skipped``：只有字段、序号、原因三个键，原因里不带条目原文。序号的含义：
- ``-1``：整个字段跳过（不支持预置，或预置的形状与字段类型对不上）；
- ``items`` 形状：条目在 items 里的位置（从 0 起）；
- ``groups`` / ``sets`` 形状：条目所在的组或套在列表里的位置（从 0 起），组里第几条写在原因里。
只读不写；日志只记不合规的种类，不记条目内容。命令行 ``python3 -m life_presets --check`` 只打印状态与跳过数。
"""
import argparse
import errno
import json
import logging
import os
import re
import stat
import sys

import bots_registry
import life_settings

_log = logging.getLogger(__name__)

FILE_NAME = "life_presets.json"
LOCATION = "本仓库 configs/life_presets.json"
MAX_BYTES = 256 * 1024
MISSING_MESSAGE = "预置清单没找到：本仓库 configs/life_presets.json"
INVALID_MESSAGE = "预置清单格式不对，暂不显示预置项"
UNSUPPORTED_REASON = "该字段不支持预置"
SHAPE_REASON = "预置的形状和这个字段的类型对不上"
# 支持预置的字段类型 → 预置里装条目的容器键（INTERFACE 3.6.1 的表）
_CONTAINERS = {"string_list": ("items", "groups"), "object_list": ("items",), "object_list_map": ("sets",)}
# 事件类别直接按字段表给的 options 勾选，不进清单（D10）
_NOT_PRESETABLE = frozenset({"event_categories"})
_SET_ID_RE = re.compile(r"[a-z0-9_]{1,32}")
_Bad = life_settings._Bad


class _Invalid(Exception):
    """整份清单不合规（→ invalid）。message 只是种类，供日志用。"""


def preset_path() -> str:
    return os.path.join(str(bots_registry.configs_dir()), FILE_NAME)


def _read_bytes(path: str):
    """不跟随软链打开、只认普通文件、最多读 MAX_BYTES 字节。不存在 → None；其余不合规 → _Invalid。
    O_NONBLOCK：路径若是命名管道，打开时不会卡住（随后按"不是普通文件"拒掉）。"""
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as e:
        if e.errno in (errno.ENOENT, errno.ENOTDIR):
            return None
        raise _Invalid(errno.errorcode.get(e.errno, type(e).__name__))   # 软链是 ELOOP
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _Invalid("not_regular_file")
        if st.st_size > MAX_BYTES:
            raise _Invalid("too_large")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES:   # 读的时候还在变大
                raise _Invalid("too_large")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _parse(raw: bytes) -> dict:
    """→ fields 对象。"""
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _Invalid("not_utf8_json")
    if not isinstance(doc, dict):
        raise _Invalid("not_object")
    version = doc.get("version")
    if type(version) is not int or version != 1:   # True、1.0、"1" 都不算
        raise _Invalid("version")
    fields = doc.get("fields")
    if not isinstance(fields, dict):
        raise _Invalid("fields")
    return fields


def _skip(field: str, index: int, reason: str) -> dict:
    return {"field": field, "index": index, "reason": reason}


def _check_label(value) -> None:
    """组名、套名、条目的显示名：一行非空文字，不含控制字符。"""
    if not isinstance(value, str) or not value.strip():
        raise _Bad("名称要是一行非空文字")
    life_settings._check_str(value, {}, single_line=True)


def _string_entry(field: dict, entry) -> str:
    """字符串，或 {"label", "value"}；→ 存下来的字符串（就是它的识别键）。"""
    value = entry
    if isinstance(entry, dict):
        if set(entry) != {"label", "value"}:
            raise _Bad("带名称的一项只能有 label 与 value 两个键")
        _check_label(entry["label"])
        value = entry["value"]
    try:   # 与 PUT 时同一个校验：只放这一项进去，所以它报的位置总是"第 1 项"，去掉
        return life_settings._check_string_list(dict(field, max_items=1), [value])[0]
    except _Bad as e:
        raise _Bad(re.sub(r"^第 1 项", "", str(e)))


def _object_entry(field: dict, entry) -> dict:
    """爱好、日常安排的一条：键只能取字段表 item_fields 里的，name 必填；→ 规整后的一行（空的子项不带）。"""
    if not isinstance(entry, dict):
        raise _Bad("要是一组子项")
    known = {s["key"] for s in field["item_fields"]}
    if any(k not in known for k in entry):
        raise _Bad("有字段表里没有的子项")
    row = life_settings._check_row(field["item_fields"], entry, "这一项")
    if not row.get("name"):
        raise _Bad("缺少必填的「名字」")
    return row


class _Seen:
    """同一字段里识别键重复的，后出现的跳过。"""

    def __init__(self):
        self._keys = set()

    def take(self, identity: str) -> None:
        if identity in self._keys:
            raise _Bad("和前面的条目重复")
        self._keys.add(identity)


def _items(key: str, field: dict, items: list, seen: _Seen, skipped: list) -> list:
    out = []
    for i, entry in enumerate(items):
        try:
            if field["type"] == "string_list":
                seen.take(_string_entry(field, entry))
            else:
                entry = _object_entry(field, entry)
                seen.take(entry["name"])
        except _Bad as e:
            skipped.append(_skip(key, i, str(e)))
            continue
        out.append(entry)
    return out


def _string_groups(key: str, field: dict, groups: list, seen: _Seen, skipped: list) -> list:
    out = []
    for gi, group in enumerate(groups):
        if not isinstance(group, dict) or not isinstance(group.get("items"), list):
            skipped.append(_skip(key, gi, "这一组要有 label 与 items 列表"))
            continue
        try:
            _check_label(group.get("label"))
        except _Bad as e:
            skipped.append(_skip(key, gi, "这一组的%s" % e))
            continue
        kept = []
        for j, entry in enumerate(group["items"]):
            try:
                seen.take(_string_entry(field, entry))
            except _Bad as e:
                skipped.append(_skip(key, gi, "第 %d 项%s" % (j + 1, e)))
                continue
            kept.append(entry)
        if kept:
            out.append({"label": group["label"], "items": kept})
    return out


def _sets(key: str, field: dict, sets: list, seen: _Seen, skipped: list) -> list:
    """日常安排按人物类型成套：识别键 = 分组键 + name + when（三者之间用 NUL 隔开；校验已保证文字里没有 NUL）。"""
    labels = {g["key"]: g["label"] for g in field.get("groups") or []}
    out, ids = [], set()
    for si, one in enumerate(sets):
        if not isinstance(one, dict) or not isinstance(one.get("groups"), dict):
            skipped.append(_skip(key, si, "这一套要有 id、label 与按分组的 groups"))
            continue
        set_id = one.get("id")
        if not isinstance(set_id, str) or not _SET_ID_RE.fullmatch(set_id):
            skipped.append(_skip(key, si, "这一套的 id 只能是 1 到 32 个小写字母、数字或下划线"))
            continue
        if set_id in ids:
            skipped.append(_skip(key, si, "这一套的 id 和前面的重复"))
            continue
        ids.add(set_id)
        try:
            _check_label(one.get("label"))
        except _Bad as e:
            skipped.append(_skip(key, si, "这一套的%s" % e))
            continue
        groups, count = {}, 0
        for group_key, rows in one["groups"].items():
            if group_key not in labels:
                skipped.append(_skip(key, si, "这一套里有不认识的分组"))
                continue
            if not isinstance(rows, list):
                skipped.append(_skip(key, si, "「%s」要是列表" % labels[group_key]))
                continue
            kept = []
            for j, entry in enumerate(rows):
                try:
                    row = _object_entry(field, entry)
                    seen.take("\0".join((group_key, row["name"], row.get("when", ""))))
                except _Bad as e:
                    skipped.append(_skip(key, si, "「%s」第 %d 条%s" % (labels[group_key], j + 1, e)))
                    continue
                kept.append(row)
            groups[group_key] = kept
            count += len(kept)
        if count:   # 一条可用的都没有的套不显示
            out.append({"id": set_id, "label": one["label"], "groups": groups})
    return out


def _field_presets(key: str, field: dict, spec, skipped: list):
    """一个字段的预置：只含通过校验的条目；一条都没有 → None。"""
    containers = _CONTAINERS[field["type"]]
    present = [c for c in containers if isinstance(spec, dict) and c in spec]
    if not present or any(not isinstance(spec[c], list) for c in present):
        skipped.append(_skip(key, -1, SHAPE_REASON))
        return None
    seen, out = _Seen(), {}
    if "items" in present:
        items = _items(key, field, spec["items"], seen, skipped)
        if items:
            out["items"] = items
    if "groups" in present:
        groups = _string_groups(key, field, spec["groups"], seen, skipped)
        if groups:
            out["groups"] = groups
    if "sets" in present:
        sets = _sets(key, field, spec["sets"], seen, skipped)
        if sets:
            out["sets"] = sets
    return out or None


def _validate(fields: dict):
    table = {f["key"]: f for f in life_settings.field_table()}
    presets, skipped = {}, []
    for key, spec in fields.items():
        field = table.get(key)
        if field is None or key in _NOT_PRESETABLE or field["type"] not in _CONTAINERS:
            skipped.append(_skip(key, -1, UNSUPPORTED_REASON))
            continue
        got = _field_presets(key, field, spec, skipped)
        if got:
            presets[key] = got
    return presets, skipped


def load() -> dict:
    """GET /api/console/life_presets 的响应体（不含 ok）：location、status、message、presets、skipped。不抛异常。"""
    out = {"location": LOCATION, "status": "ok", "message": "", "presets": {}, "skipped": []}
    try:
        raw = _read_bytes(preset_path())
        if raw is None:
            out.update(status="missing", message=MISSING_MESSAGE)
            return out
        out["presets"], out["skipped"] = _validate(_parse(raw))
        return out
    except _Invalid as e:
        _log.warning("预置清单不合规：%s", e)
    except Exception as e:   # 校验自身出错也不让接口回 500（3.2.4 除登录门外恒回 200）；异常文字可能带条目内容，只记类型
        _log.error("预置清单读取出错：%s", type(e).__name__)
    out.update(status="invalid", message=INVALID_MESSAGE, presets={}, skipped=[])
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m life_presets",
                                 description="生活设定预置清单：只打印状态与跳过数，不输出条目原文。")
    ap.add_argument("--check", action="store_true", help="读一遍清单并逐项校验")
    args = ap.parse_args(argv)
    if not args.check:
        ap.print_usage(sys.stderr)
        return 2
    got = load()
    print("预置清单（%s）：状态 %s，跳过 %d 条" % (got["location"], got["status"], len(got["skipped"])))
    if got["message"]:
        print(got["message"])
    # 全部条目都通过才算过：跳过的条目在页面上不会出现，交给人看之前应先改好
    return 0 if got["status"] == "ok" and not got["skipped"] else 1


if __name__ == "__main__":
    sys.exit(main())
