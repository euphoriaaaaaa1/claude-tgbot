# -*- coding: utf-8 -*-
"""纯工具函数（普通模块，非测试文件）。测试从这里取断言辅助，保持用例本身只讲场景。"""
import os
import stat as _stat
from pathlib import Path

import yaml


def version_of(path):
    """§2.1：`f"{st_mtime_ns}-{st_size}"`，文件不存在时 `"absent"`。"""
    p = Path(path)
    if not p.exists():
        return "absent"
    st = p.stat()
    return f"{st.st_mtime_ns}-{st.st_size}"


def mode_of(path):
    """返回 0o600 这种权限位（只取低 9 位）。"""
    return _stat.S_IMODE(os.stat(path).st_mode)


def changed_lines(before, after):
    """逐行 diff，返回 [(行号, 旧行, 新行)]；行数不同时把多出来的行也报出来。"""
    b, a = before.splitlines(True), after.splitlines(True)
    out = []
    for i in range(max(len(b), len(a))):
        ob = b[i] if i < len(b) else None
        oa = a[i] if i < len(a) else None
        if ob != oa:
            out.append((i + 1, ob, oa))
    return out


def dig(obj, dotted):
    """按点分路径取值；取不到抛 KeyError（用例要的是明确失败）。"""
    cur = obj
    for part in dotted.split("."):
        cur = cur[part]
    return cur


def load(path):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def text(path):
    return Path(path).read_text(encoding="utf-8")


def addbot_payload(**over):
    """§4.2 的一份合法请求体（token 由用例注入，端口由用例挑空闲的）。"""
    body = {"bot_id": "xiaoyu", "telegram_token": OMIT, "dispatcher_port": OMIT,
            "persona_template": OMIT, "persona_text": "我是测试人设。",
            "display_name": "小雨", "chat_id": 123456789}
    body.update(over)
    # OMIT = 这个键根本不发；显式 None 会原样发出去（用来测 null 的行为）
    return {k: v for k, v in body.items() if not isinstance(v, _Omit)}


class _Omit:
    def __repr__(self):
        return "<OMIT>"


OMIT = _Omit()


def steps_by_name(body):
    """§4.3 的 steps 列表 → {name: step}，缺 steps 时给空 dict（断言会自己炸）。"""
    return {s.get("name"): s for s in (body.get("steps") or []) if isinstance(s, dict)}


def backup_ids(body, kind=None):
    items = body.get("backups") or []
    if kind is not None:
        items = [b for b in items if b.get("kind") == kind]
    return [b.get("id") for b in items]


def flat_text(obj):
    """把响应体拍平成一个字符串，用于"这串东西绝不能出现在响应里"的断言。"""
    import json
    return json.dumps(obj, ensure_ascii=False)
