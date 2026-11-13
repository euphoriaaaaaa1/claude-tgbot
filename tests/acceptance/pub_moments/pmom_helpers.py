# -*- coding: utf-8 -*-
"""朋友圈页（公开版）验收的公共小工具（不是测试文件）。

- 底层适配器替身：按接口文件 §0 的形状提供 meta / bots / get_life / notify，notify 只记参数。
- 造数据：经 db 模块已有函数写临时库；接口造不出来的坏数据（坏 JSON、坏头像地址）直接用 sqlite3 写。
- 测试图片：真 1 像素 PNG 加填充字节，不是真实照片。

本模块顶层不导入任何被测模块（moments.moments_routes 等）：被测模块还没实现时，测试照样能被收集。
"""
import base64
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from html import unescape
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

USER = "__user__"
ME = "我"                       # user_display_name 的缺省值
ADDR = "哥哥"                   # 回落称呼
DECODE_FAILED = "图片解析失败，请换一张图片重试"
MiB = 1024 * 1024
MAX_IMG = 8 * MiB               # 上传图解码后上限
MAX_BODY = 12 * MiB             # 带图接口请求体上限
DM_RULE = "- 想就这条朋友圈和 %s 多聊几句，可以用 reply 工具私聊；动态里写了想让人私聊的，就私聊；评论和私聊可以都做"
OLD_DM_BAN = "不要在 telegram 私聊里说话"
URL_RULE_MSG = "%s 只收 http/https 链接或本站 /image/ 地址（2048 字符以内，不含引号、括号、反斜杠与换行）"
COMMENT_KEYS = {"id", "ts", "from", "text", "pending", "parent_id", "image_path", "image_url"}
MOMENT_KEYS = {"id", "bot_id", "ts", "time_str", "time_short", "ago", "text", "image_path", "image_paths", "kind",
               "visibility", "metadata", "chapter", "chapter_sub", "image_urls"}
PROFILE_KEYS = {"avatar_url", "banner_url", "signature", "sig_updated_at", "sig_asked_at", "display_name",
                "updated_at"}
TABLES = ("moments", "moment_comments", "moment_likes", "bot_profiles")

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


def png(tag=""):
    """真 PNG 头 + 填充；tag 不同内容就不同。"""
    return PNG_1PX + tag.encode("utf-8")


def data_url(data, mime="image/png"):
    return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))


def digest(p):
    with open(str(p), "rb") as fh:
        return hashlib.sha1(fh.read()).hexdigest()


def write_file(p, data=None):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(PNG_1PX if data is None else data)
    return p


def files_in(d):
    """目录下（递归）全部普通文件的路径列表；目录不存在时为空。"""
    out = []
    for root, _dirs, names in os.walk(str(d)):
        out += [os.path.join(root, n) for n in names]
    return sorted(out)


def media_state(root):
    """图片根下逐文件（相对路径 → 内容摘要）。"""
    return {os.path.relpath(p, str(root)): digest(p) for p in files_in(root)}


def sized_json(prefix, n):
    """恰好 n 字节的 JSON 文本：prefix 是开头的几个键（不带花括号收尾），后面用 pad 填满。"""
    head, tail = prefix + ', "pad": "', '"}'
    text = head + "x" * (n - len(head.encode()) - len(tail.encode())) + tail
    assert len(text.encode()) == n
    return text


# ── 库 ───────────────────────────────────────────────────────────────────────

def q(db_path, sql, args=()):
    con = sqlite3.connect(str(db_path), timeout=10)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql, args)]
    finally:
        con.close()


def x(db_path, sql, args=()):
    con = sqlite3.connect(str(db_path), timeout=10)
    try:
        cur = con.execute(sql, args)
        con.commit()
        return cur.lastrowid
    finally:
        con.close()


def tables(db_path):
    return {t: q(db_path, "SELECT * FROM %s ORDER BY rowid" % t) for t in TABLES}


def user_moment(db, text="我的动态", ts=None, image_path=None, metadata=None, visibility="public"):
    return db.insert_moment(bot_id=USER, ts=int(time.time()) if ts is None else ts, text=text,
                            image_path=None if image_path is None else str(image_path),
                            metadata={"author_display": ME} if metadata is None else metadata,
                            kind="user_post", visibility=visibility)


def bot_moment(db, bot, text="原帖", ts=1760000000, image_path=None, metadata=None, visibility="public"):
    return db.insert_moment(bot_id=bot, ts=ts, text=text, image_path=None if image_path is None else str(image_path),
                            metadata={} if metadata is None else metadata, kind="daily", visibility=visibility)


def add_comment(db_path, mid, who, text, parent=None, image=None, ts=None, pending=0):
    """直接写一条评论（bot 的回复、带图评论等接口造不出来的）。ts 可指定，便于排序。"""
    return x(db_path, "INSERT INTO moment_comments (moment_id, from_user, text, ts, parent_id, pending, image_path) "
                      "VALUES (?,?,?,?,?,?,?)",
             (mid, who, text, int(time.time()) if ts is None else ts, parent, pending,
              None if image is None else str(image)))


def add_like(db_path, mid, who, ts=1760000200):
    x(db_path, "INSERT INTO moment_likes (moment_id, liker, ts) VALUES (?,?,?)", (mid, who, ts))


def comment_rows(db_path, mid):
    return q(db_path, "SELECT * FROM moment_comments WHERE moment_id=? ORDER BY id", (mid,))


def like_rows(db_path, mid):
    return q(db_path, "SELECT * FROM moment_likes WHERE moment_id=? ORDER BY rowid", (mid,))


def pending_of(db_path, cid):
    (row,) = q(db_path, "SELECT pending FROM moment_comments WHERE id=?", (cid,))
    return bool(row["pending"])


# ── 页面解析 ─────────────────────────────────────────────────────────────────

_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>[\s\S]*?</\1>", re.I)


def markup(html):
    """去掉 <script> 与 <style> 块后的标记文本。"""
    return _SCRIPT_STYLE.sub("", html)


def chips(html):
    """每人一个 chip：href 恰好是 ?bot=<人> 的 <a>。返回 {人: 整个 a 元素文本}。"""
    out = {}
    for m in re.finditer(r'<a\b([^>]*)>([\s\S]*?)</a>', markup(html)):
        hm = re.search(r'\bhref\s*=\s*"\?bot=([^"&]+)"', m.group(1))
        if hm:
            out[unescape(hm.group(1))] = m.group(0)
    return out


def class_count(html, cls):
    """标记里 class 含 cls 的开标签个数（不算脚本与样式里的）。"""
    return len(re.findall(r'<[a-zA-Z][^>]*\bclass\s*=\s*"[^"]*(?<![\w-])%s(?![\w-])[^"]*"' % re.escape(cls),
                          markup(html)))


def times_marks(html):
    """标记里"×"的个数（含 &times; 与 &#215; 写法）。"""
    m = markup(html)
    return m.count("×") + m.count("&times;") + m.count("&#215;") + m.count("&#xd7;") + m.count("&#xD7;")


def style_attrs(html):
    return [unescape(v) for v in re.findall(r'\bstyle\s*=\s*"([^"]*)"', markup(html))]


def img_srcs(html):
    return [unescape(v) for v in re.findall(r'<img\b[^>]*\bsrc\s*=\s*"([^"]*)"', markup(html))]


# ── 底层适配器替身 ───────────────────────────────────────────────────────────

def bot(bot_id, state="running", person=None, display_name=None):
    b = {"bot_id": bot_id, "display_name": display_name or ("名-" + bot_id), "state": state}
    if person is not None:
        b["person"] = person
    return b


class FakeAdapter:
    """一个底层的替身。notify 按 bot_id 可指定：异常实例（抛它）、字典（原样回）；缺省回 delivered。"""

    def __init__(self, name, log, bots=None, caps=None, life=None):
        self.name = name
        self.log = log
        self.bot_list = list(bots or [])
        self.caps = {"bot.notify": True, "bot.life_settings": False}
        self.caps.update(caps or {})
        self.life = dict(life or {})          # bot_id -> values 字典
        self.notify_plan = {}
        self.fail = {}                        # 方法名 -> 异常实例
        self.block = None                     # threading.Event：bots() 等它（模拟超时）
        self.life_calls = []

    def meta(self):
        if "meta" in self.fail:
            raise self.fail["meta"]
        return {"capabilities": dict(self.caps)}

    def bots(self):
        if self.block is not None:
            self.block.wait(20)
        if "bots" in self.fail:
            raise self.fail["bots"]
        return [dict(b) for b in self.bot_list]

    def get_life(self, bot_id):
        self.life_calls.append(bot_id)
        if "get_life" in self.fail:
            raise self.fail["get_life"]
        return {"values": dict(self.life.get(bot_id, {}))}

    def notify(self, *args, **kwargs):
        assert not kwargs, "notify 的四个参数应按位置传，实际有关键字参数 %r" % sorted(kwargs)
        bot_id, source, text, key = args
        with self.log.lock:
            self.log.calls.append({"backend": self.name, "bot_id": bot_id, "source": source, "text": text, "key": key})
        how = self.notify_plan.get(bot_id)
        if isinstance(how, BaseException):
            raise how
        if isinstance(how, dict):
            return dict(how)
        return {"status": "delivered"}


class Backends:
    """替换 adapters.registry.enabled_backends / adapter 的登记表。"""

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.fakes = {}
        self.unavailable = {}                 # 底层名 -> 异常实例（adapter(name) 时抛）

    def add(self, name, bots=None, caps=None, life=None):
        f = FakeAdapter(name, self, bots, caps, life)
        self.fakes[name] = f
        return f

    def enabled(self):
        return list(self.fakes)

    def adapter(self, name):
        if name in self.unavailable:
            raise self.unavailable[name]
        return self.fakes[name]

    def who(self, source=None):
        return sorted((c["backend"], c["bot_id"]) for c in self.calls if source is None or c["source"] == source)

    def of(self, bot_id):
        return [c for c in self.calls if c["bot_id"] == bot_id]

    def clear(self):
        with self.lock:
            del self.calls[:]


def dm_lines(text, addr=ADDR):
    """通知文字里"多聊几句"那类行（去首尾空白）。"""
    return [ln.strip() for ln in text.split("\n") if "多聊几句" in ln]


def jdump(obj):
    return json.dumps(obj, ensure_ascii=False)
