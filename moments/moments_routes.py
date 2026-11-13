"""朋友圈页（/moments）、它的接口与图片路由。

分层：bot 名单与投递一律经 adapters 包，不读任何 bot 配置，也不碰任何一种底层的目录。直接读写的只有三样共用数据：
朋友圈库里的四张表（db 模块，表结构不动）、全局参数文件（moments.global_cfg，只读）、图片根（MOMENTS_MEDIA_ROOT）。
登录门、跨站核对、可达面闸门在 moments/web.py 里对本蓝图生效。本模块不起任何后台循环；投递在请求里做完。
"""
import base64
import contextlib
import json
import logging
import os
import re
import secrets
import stat
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime
from urllib.parse import quote, urlsplit

from flask import Blueprint, abort, current_app, jsonify, render_template, request, send_file

import db
from adapters import AdapterError, AdapterUnavailable, registry, valid_bot_id
from adapters.base import NOTIFY_STATUSES
from moments import global_cfg, shell_path, sticker_store
from moments.request_body import BodyError, json_object

moments_bp = Blueprint("moments_feed", __name__)
_log = logging.getLogger(__name__)

# 没填称呼时的中性写法。各句另有不带「」称呼的中性版本，见 _address_words。
USER_ADDRESS_NEUTRAL = "用户"
USER_DISPLAY_FALLBACK = "我"
USER_PROFILE_KEY = "__user__"
PAGE_SIZE = 30
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOMENT_REPLY_SCRIPT = os.path.join(_REPO_ROOT, "scripts", "moment_reply.py")
MOMENT_LIKE_SCRIPT = os.path.join(_REPO_ROOT, "scripts", "moment_like.py")
MOMENT_DELETE_COMMENT_SCRIPT = os.path.join(_REPO_ROOT, "scripts", "moment_delete_comment.py")
PYTHON_BIN = os.environ.get("CLAUDEBOTLIFE_PYTHON", sys.executable)
# "在跑"：进程在，或空闲态（来消息会自己起来）。停用、坏了都不算。
# dispatcher_down 也算：只是主程序暂时没响应、会话还在，这只 bot 仍可收通知。
RUNNING_STATES = ("running", "idle", "dispatcher_down")
# 取两种底层 bot 名单的总时限（秒）：一边慢不拖另一边，超时的那一边当没有 bot。
BOTS_TIMEOUT = 12
# 发动态提醒与"用户在自己动态下评论"的通知共用的私聊规则：想聊就私聊，bot 自己判断。
DM_RULE = "- 想就这条朋友圈和 {addr} 多聊几句，可以用 reply 工具私聊；动态里写了想让人私聊的，就私聊；评论和私聊可以都做"
# 两种评论通知（单人回复、用户在自己动态下评论）的长度：评论不限条数、不限长度时，整条攒到约 16 KiB 会被适配器
# 拒收（adapters.base.NOTIFY_MAX_TEXT_BYTES），bot 收不到。字数按字符算，整条按 UTF-8 字节算。
SEE_COMMENT_MAX_OTHERS = 20      # 其他评论只列最近这么多条
SEE_COMMENT_OTHER_CHARS = 100    # 其他评论每条正文超过这么多字就截断，以"…"结尾
SEE_COMMENT_MOMENT_CHARS = 200   # 动态原文超过这么多字就截断，以"…"结尾
SEE_COMMENT_MAX_BYTES = 15000    # 整条上限：先从最早的其他评论删起，仍超再截这条评论；命令与规则几行不动
# 评论正文里的换行一律换成一个空格（\r\n 算一个换行，口径同 str.splitlines）：一条评论只占一行，也冒充不了指令行
_LINE_BREAK = re.compile(r"\r\n|[\n\r\v\f\x1c\x1d\x1e\x85  ]")
# 上传图片解不开、存不下时回给浏览器的固定文字：异常原文可能带路径，只把类名记进日志。
IMAGE_DECODE_FAILED = "图片解析失败，请换一张图片重试"
MAX_IMAGE_BYTES = 8 * 1024 * 1024      # 上传图解码后上限
MAX_BODY_BYTES = 12 * 1024 * 1024      # 带图接口请求体上限：8 MiB 原图 base64 后约 10.7 MiB，再留些余量
# 资料接口只收页面会提交的这三个键（页面的换头像、换封面、改名）
PROFILE_KEYS = ("avatar_url", "banner_url", "display_name")
# 头像、横幅地址会被放进 style 属性的 url('…')：只收 http/https 链接与本站 /image/…，限长，且不许出现能跳出
# CSS 字符串或 url() 的字符。HTML 实体在属性里先被解码再交给 CSS，所以光靠模板转义拦不住引号。
IMAGE_URL_MAX = 2048
_IMAGE_URL_BAD = frozenset("'\"()\\") | frozenset(chr(c) for c in range(0x20)) | {"\x7f"}
DB_UNAVAILABLE = "朋友圈库打不开，请检查数据目录的权限与磁盘空间后刷新重试"

_db_ready = False
_db_lock = threading.Lock()


def _media_root() -> str:
    """图片根（不写死家目录）。每次现读，测试可换临时目录。"""
    return os.path.realpath(os.path.expanduser(os.environ.get("MOMENTS_MEDIA_ROOT") or "~/resource/media"))


def _under(real: str, root: str) -> bool:
    """real 是否在 root 之下（不含 root 本身）。两边 normcase：大小写不敏感的文件系统上也比得准。"""
    prefix = root if root.endswith(os.sep) else root + os.sep
    return os.path.normcase(real).startswith(os.path.normcase(prefix))


def _image_url(path):
    """库里的绝对路径 → 本站 /image/<相对图片根的路径>（/ 分隔）；不在图片根下、不是绝对路径的 → None。
    页面与接口只给这种地址，不把图片根以外的绝对路径带出去，Windows 盘符路径也能用。"""
    if not isinstance(path, str) or not path or not os.path.isabs(path):
        return None
    root = _media_root()
    try:
        real = os.path.realpath(path)
    except (OSError, ValueError):
        return None
    if not _under(real, root):
        return None
    rel = os.path.relpath(real, root).replace(os.sep, "/")
    return "/image/" + quote(rel, safe="/")


@moments_bp.before_request
def _ensure_db():
    """本进程第一次处理朋友圈请求前建一次表（幂等；旧库缺表缺列时 db.init 先备份再迁移）。
    新装机库文件还不存在时，打开朋友圈页就能用。失败回 503，下次请求再试。图片路由不依赖库，不受影响。"""
    global _db_ready
    if _db_ready or request.endpoint == "moments_feed.serve_image":
        return None
    with _db_lock:
        if not _db_ready:
            try:
                db.init()
            except Exception as e:
                _log.error("朋友圈页：建库失败（%s）", type(e).__name__)
                if request.path.startswith("/api/"):
                    return jsonify({"error": DB_UNAVAILABLE}), 503
                return DB_UNAVAILABLE, 503
            _db_ready = True
    return None


# ── bot 名单：各底层适配器的 bots()，按"人"合并 ─────────────────────────────

def _entry(backend: str, raw):
    """适配器给的一条 → 本页用的条目；bot_id 不合规的不要。没有 person 的用 bot_id 代替。"""
    if not isinstance(raw, dict) or not valid_bot_id(raw.get("bot_id")):
        return None
    bot_id = raw["bot_id"]
    person = raw.get("person")
    if not (isinstance(person, str) and person.strip() and len(person) <= 64):
        person = bot_id
    if person == USER_PROFILE_KEY:          # 用户自己主页的键不许被哪只 bot 占走
        return None
    name = raw.get("display_name")
    state = raw.get("state")
    return {"backend": backend, "bot_id": bot_id, "person": person,
            "display_name": name if isinstance(name, str) and name else bot_id,
            "state": state if isinstance(state, str) else "broken"}


def _load_backend(name: str, deadline: float):
    """一种底层的 (能力表, 条目列表)。任何失败只落在这一边：这一边的 bot 不出现，另一边照常。"""
    try:
        ad = registry.adapter(name)
        meta = ad.meta()
        caps = meta.get("capabilities")
        caps = {str(k): v is True for k, v in caps.items()} if isinstance(caps, dict) else {}
        if time.monotonic() >= deadline:
            return {}, []
        raw = ad.bots()
    except (AdapterUnavailable, AdapterError):
        return {}, []
    except Exception:
        _log.exception("朋友圈页：底层 %s 读取失败", name)
        return {}, []
    entries = [e for e in (_entry(name, b) for b in (raw if isinstance(raw, list) else [])) if e]
    return caps, entries


def _probe():
    """各底层并行读，共用一个总时限。返回 ``({底层: 能力表}, 全部条目)``。"""
    names = registry.enabled_backends()
    deadline = time.monotonic() + BOTS_TIMEOUT
    ex = ThreadPoolExecutor(max_workers=max(1, len(names)))
    futures = [ex.submit(_load_backend, n, deadline) for n in names]
    ex.shutdown(wait=False)   # 不等被放弃的调用；工作线程跑完自己退出
    caps, entries = {}, []
    for name, fut in zip(names, futures):
        try:
            c, e = fut.result(timeout=max(0.0, deadline - time.monotonic()))
        except FutureTimeout:
            fut.add_done_callback(lambda f: f.exception())
            continue
        caps[name] = c
        entries.extend(e)
    return caps, entries


def _rank(e):
    """同一个人的几份条目谁赢：在跑的优先，其次 dsh。"""
    return (1 if e["state"] in RUNNING_STATES else 0, 1 if e["backend"] == "dsh" else 0)


def _tag(e) -> str:
    return "%s(%s)" % (e["bot_id"], e["backend"])


def _warn_collision(person, dropped, kept):
    """撞名留痕：同一行在一个进程里只写一次；没有应用上下文就每次都写。"""
    line = "[moments] 朋友圈名 %s 撞车，%s 与 %s，留 %s\n" % (person, _tag(dropped), _tag(kept), _tag(kept))
    try:
        store = current_app.extensions.setdefault("moments_collision_warned", set())
    except RuntimeError:
        store = None
    if store is not None:
        if line in store:
            return
        store.add(line)
    sys.stderr.write(line)


def _pick_people(entries) -> dict:
    """``{朋友圈名: 条目}``，每人一条（按 _rank 挑，同分留先出现的）。

    挑中的条目多带 ``conflict``：这个人在两种底层上都在跑，即同一个 Telegram 账号被两边同时拉消息。
    这种情况不悄悄选一边，页面标错误；投递仍只投一份，按 _rank 落在 dsh 那一份。"""
    groups = {}
    for e in entries:
        groups.setdefault(e["person"], []).append(e)
    people = {}
    for person, copies in groups.items():
        best = max(copies, key=_rank)
        running_backends = {c["backend"] for c in copies if c["state"] in RUNNING_STATES}
        people[person] = dict(best, conflict=len(running_backends) > 1)
        for c in copies:
            if c is not best:
                _warn_collision(person, c, best)
    return people


def _life_values(e, caps) -> dict:
    """这只 bot 的一句话简介与对用户的称呼。只在底层报了 bot.life_settings 时经适配器问；问不到就空着。"""
    if not caps.get(e["backend"], {}).get("bot.life_settings"):
        return {}
    try:
        values = registry.adapter(e["backend"]).get_life(e["bot_id"]).get("values")
    except (AdapterError, AdapterUnavailable):
        return {}
    except Exception:
        _log.exception("朋友圈页：取 %s 的生活设定失败", _tag(e))
        return {}
    out = {}
    # bio 为空时读 persona_summary（另一种底层的字段名）
    for k, names in (("bio", ("bio", "persona_summary")), ("user_address", ("user_address",))):
        for name in names:
            v = values.get(name) if isinstance(values, dict) else None
            if isinstance(v, str) and v.strip():
                out[k] = v
                break
    return out


def _address_words(entry, caps) -> dict:
    """通知里与称呼有关的几处说法。填了称呼的照原句嵌称呼；没填的不套任何固定称呼，改用中性的"用户"并换句，
    免得与 bot 人设里的称呼打架。"""
    addr = _life_values(entry, caps).get("user_address")
    if addr:
        return {
            "addr": addr,
            "public_warn": f"不要在回复里用「{addr}」这种亲密称呼，也不要透露你和 {addr} 的私密关系",
            "private_note": f"只有 {addr} 能看到，可以放开亲密称呼",
            "no_you_rule": f"评论里不要用「你」指代 {addr}，直接称「{addr}」或省略主语",
        }
    return {
        "addr": USER_ADDRESS_NEUTRAL,
        "public_warn": "不要在回复里用你平时对用户的亲密称呼，也不要透露你和用户的私密关系",
        "private_note": "只有用户能看到，可以放开亲密称呼",
        "no_you_rule": "评论里不要用「你」指代用户，按你人设里对用户的称呼直接称呼，或省略主语",
    }


def _notify(e, caps, source: str, text: str, key: str) -> bool:
    """经适配器投给这一份。True = 已交给 bot（duplicate 也算）。底层没报 bot.notify 就不调。"""
    if not caps.get(e["backend"], {}).get("bot.notify"):
        sys.stderr.write("[moments] %s 的底层不支持投递通知，跳过\n" % _tag(e))
        return False
    try:
        res = registry.adapter(e["backend"]).notify(e["bot_id"], source, text, key)
    except AdapterError as err:
        sys.stderr.write("[moments] 通知 %s 没投成：%s\n" % (_tag(e), err.code))
        return False
    except AdapterUnavailable as err:
        sys.stderr.write("[moments] 通知 %s 没投成：%s\n" % (_tag(e), err.status))
        return False
    except Exception:
        _log.exception("朋友圈页：通知 %s 失败", _tag(e))
        return False
    return isinstance(res, dict) and res.get("status") in NOTIFY_STATUSES


def _notify_all_running(source: str, trigger, *args) -> None:
    """给每个在跑的人各投一份。名单 = 每人在跑的那一份，每人只投一份（两边都在跑时投 dsh 那份），停用的、
    不在跑的不投。各人之间并行投，免得一边连不上、重试把整个请求拖成几十秒；全部投完才返回，不留后台任务。
    某一份出错只记一行，不影响别人。trigger 的参数是 (条目, 能力表, {朋友圈名: 显示名}, *args)。"""
    try:
        caps, entries = _probe()
        targets = [e for _p, e in sorted(_pick_people(entries).items()) if e["state"] in RUNNING_STATES]
    except Exception:
        _log.exception("朋友圈页：取投递名单失败")
        return
    if not targets:
        return
    running_names = {e["person"]: e["display_name"] for e in targets}
    with ThreadPoolExecutor(max_workers=len(targets)) as ex:
        futures = [ex.submit(trigger, e, caps, running_names, *args) for e in targets]
    for e, fut in zip(targets, futures):
        if fut.exception() is not None:
            sys.stderr.write("[%s] trigger %s failed: %s\n" % (source, _tag(e), type(fut.exception()).__name__))


# ── 展示 ─────────────────────────────────────────────────────────────────────

def _user_display_name() -> str:
    return global_cfg.load().get("user_display_name") or USER_DISPLAY_FALLBACK


def _style_url(v):
    """头像、封面地址进 style 前再判一次地址规则：旧库迁移来的数据没经过写入口的校验，不合规就当没有。"""
    return v if isinstance(v, str) and v and _image_url_ok(v) else None


def _user_meta() -> dict:
    """主页 = 用户自己的朋友圈主页资料"""
    g = global_cfg.load()
    profile = db.get_profile(USER_PROFILE_KEY)
    return {
        "id": USER_PROFILE_KEY,
        "name": g.get("user_display_name") or USER_DISPLAY_FALLBACK,
        "bio": g.get("user_bio", ""),
        "avatar_url": _style_url(profile.get("avatar_url")),
        "banner_url": _style_url(profile.get("banner_url")),
    }


def _bot_meta(e, life=None) -> dict:
    person = e["person"]
    profile = db.get_profile(person)
    life = life or {}
    # 名字优先库里的 display_name（用户改的），其次底层报的显示名
    return {
        "id": person,
        "name": profile.get("display_name") or e["display_name"] or person,
        "bio": profile.get("signature") or life.get("bio", ""),
        "avatar_url": _style_url(profile.get("avatar_url")),
        "banner_url": _style_url(profile.get("banner_url")),
        "conflict": bool(e.get("conflict")),
    }


_WEEKDAY_ZH = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def _chapter_for(dt: datetime) -> dict:
    """根据日期分组：今天 / 昨天 / 本周 / YYYY · MM"""
    today = datetime.now().date()
    delta = (today - dt.date()).days
    sub = dt.strftime("%Y · %m · %d") + " · " + _WEEKDAY_ZH[dt.weekday()]
    if delta == 0:
        return {"chapter": "今天", "chapter_sub": sub}
    if delta == 1:
        return {"chapter": "昨天", "chapter_sub": sub}
    if delta < 7:
        return {"chapter": "本周", "chapter_sub": sub}
    return {"chapter": dt.strftime("%Y · %m"), "chapter_sub": sub}


def _ago(ts: int) -> str:
    diff = max(0, int(time.time()) - ts)   # 本机时钟回拨时不出负数
    if diff < 60:
        return f"{diff}秒前"
    if diff < 3600:
        return f"{diff // 60}分钟前"
    if diff < 86400:
        return f"{diff // 3600}小时前"
    return f"{diff // 86400}天前"


def _format_moment(m: dict) -> dict:
    try:
        metadata = json.loads(m.get("metadata_json") or "{}")
    except (TypeError, ValueError):
        metadata = {}
    if not isinstance(metadata, dict):       # 合法 JSON 但不是对象（如数组）同样当空
        metadata = {}
    paths = metadata.get("image_paths")
    if not (isinstance(paths, list) and paths):
        paths = [m["image_path"]] if m.get("image_path") else []
    ts = m["ts"]
    dt = datetime.fromtimestamp(ts)
    return {
        "id": m["id"],
        "bot_id": m["bot_id"],
        "ts": ts,
        "time_str": dt.strftime("%Y-%m-%d %H:%M"),
        "time_short": dt.strftime("%H:%M"),
        "ago": _ago(ts),
        "text": m["text"],
        "image_path": m.get("image_path"),
        "image_paths": paths,
        "image_urls": [_image_url(p) for p in paths],
        "kind": m.get("moment_kind"),
        "visibility": m.get("visibility") or "public",
        "metadata": metadata,
        **_chapter_for(dt),
    }


@moments_bp.route("/moments")
def moments_page():
    bot_filter = request.args.get("bot")
    # 默认进"主页"（用户自己的朋友圈）；切到某人才看 ta 的
    if not bot_filter:
        bot_filter = USER_PROFILE_KEY
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    moments_raw = db.list_moments(bot_id=bot_filter, limit=PAGE_SIZE * page + 1)
    has_more = len(moments_raw) > PAGE_SIZE * page
    moments = [_format_moment(m) for m in moments_raw[:PAGE_SIZE * page]]

    ids = [m["id"] for m in moments]
    likes_map = db.likers_bulk(ids)
    comments_map = db.comments_bulk(ids)
    # 各底层的 bot 都列，含停用的；每人一个 chip
    caps, entries = _probe()
    people = _pick_people(entries)
    bot_meta_by_id = {p: _bot_meta(e) for p, e in sorted(people.items())}
    bot_meta_by_id[USER_PROFILE_KEY] = _user_meta()

    def _name_for(uid: str) -> str:
        if uid in bot_meta_by_id:
            return bot_meta_by_id[uid].get("name") or uid
        return uid

    for m in moments:
        m["likers"] = [_name_for(u) for u in likes_map.get(m["id"], [])]
        m["bot_meta"] = bot_meta_by_id.get(m["bot_id"], {})
        raw_comments = comments_map.get(m["id"], [])
        by_id = {c["id"]: c for c in raw_comments}
        m["comments"] = [{
            **c,
            "from_user_label": _name_for(c["from_user"]),
            "parent_label": _name_for(by_id[c["parent_id"]]["from_user"])
                            if c.get("parent_id") and c["parent_id"] in by_id else None,
            "pending": bool(c.get("pending")),
            "image_url": _image_url(c.get("image_path")),
        } for c in raw_comments]

    if bot_filter in people:   # 顶部横幅用：简介要问底层，只问当前这一只
        active_meta = _bot_meta(people[bot_filter], _life_values(people[bot_filter], caps))
    else:
        active_meta = bot_meta_by_id[USER_PROFILE_KEY]
    bots = [v for k, v in bot_meta_by_id.items() if k != USER_PROFILE_KEY]
    return render_template(
        "moments.html",
        moments=moments,
        bots=bots,
        conflicts=[b for b in bots if b["conflict"]],
        active_bot=bot_filter,
        active_meta=active_meta,
        user_display=_user_display_name(),
        page=page,
        has_more=has_more,
    )


@moments_bp.route("/api/moments")
def api_moments():
    bot_filter = request.args.get("bot")
    nums = {}
    for name, default in (("since", "0"), ("limit", "50")):
        try:
            nums[name] = int(request.args.get(name, default))
        except ValueError:
            return jsonify({"error": "参数 %s 必须是整数" % name, "param": name}), 400
    since, limit = nums["since"], nums["limit"]
    # SQLite 的 LIMIT -1 等于不限，0 等于空：都不是调用方想要的，夹在 1..200
    if not 1 <= limit <= 200:
        return jsonify({"error": "参数 limit 必须是 1 到 200 的整数", "param": "limit"}), 400
    moments = db.list_moments(bot_id=bot_filter, limit=limit, since_ts=since)
    return jsonify([_format_moment(m) for m in moments])


# ── 互动接口 ─────────────────────────────────────────────────────────────────

@moments_bp.route("/api/like", methods=["POST"])
def api_like():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    try:
        moment_id = int(data.get("moment_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "moment_id required"}), 400
    moment = db.get_moment(moment_id)
    if not moment:
        return jsonify({"error": "moment not found"}), 404
    liker = _user_display_name()
    liked = db.toggle_like(moment_id, liker)
    likers = [r["liker"] for r in db.list_likers(moment_id)]
    return jsonify({"liked": liked, "likers": likers})


# dataURL 的头：只认 data:image/<子类型>;base64。真正的类型与扩展名另按文件头定，头里写的不作数。
_DATA_URL_HEAD = re.compile(r"data:image/[A-Za-z0-9.+-]+;base64", re.I)
# 文件开头的格式标识：PNG、JPEG、GIF。WEBP 另判（RIFF....WEBP）。
_IMAGE_MAGIC = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"), (b"GIF87a", "gif"), (b"GIF89a", "gif"))


def _decode_image(data_url) -> bytes:
    """data:image/…;base64,… → 原始字节。不是这种形状的抛 ValueError（含非字符串）。"""
    if not isinstance(data_url, str):
        raise ValueError("not a string")
    head, sep, b64 = data_url.partition(",")
    if not sep or not _DATA_URL_HEAD.fullmatch(head):
        raise ValueError("not an image data url")
    return base64.b64decode(b64)


def _image_ext(raw: bytes):
    """按文件头认图片类型，返回扩展名；认不出的回 None（HTML、SVG 之类冒充的字节不落盘）。"""
    for magic, ext in _IMAGE_MAGIC:
        if raw.startswith(magic):
            return ext
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return None


def _save_upload(subdir: str, prefix: str, raw: bytes) -> str:
    """存进 <图片根>/__user__/<subdir>/，返回绝对路径。文件名带随机后缀、用 "xb" 打开：同一秒两次上传、
    目录里已有同名文件都不会被覆盖。写到一半失败把半截文件删掉再抛。"""
    ext = _image_ext(raw)
    if ext is None:
        raise ValueError("not an image")
    save_dir = os.path.join(_media_root(), USER_PROFILE_KEY, subdir)
    os.makedirs(save_dir, exist_ok=True)
    for _ in range(5):
        full = os.path.join(save_dir, "%s_%d_%s.%s" % (prefix, int(time.time()), secrets.token_hex(4), ext))
        try:
            fh = open(full, "xb")
        except FileExistsError:
            continue
        try:
            with fh:
                fh.write(raw)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(full)
            raise
        return full
    raise FileExistsError("no free file name")


def _image_failed(e: Exception):
    _log.warning("朋友圈页：上传图片处理失败（%s）", type(e).__name__)
    return jsonify({"error": IMAGE_DECODE_FAILED}), 400


def _body():
    """带图接口的请求体：超 12 MiB 回 413、不是 JSON 对象回 422（不整段读进内存）。返回 (体, 错误响应)。"""
    try:
        return json_object(MAX_BODY_BYTES), None
    except BodyError as e:
        return None, (jsonify({"error": e.message}), e.http_status)


@moments_bp.route("/api/comment", methods=["POST"])
def api_comment():
    data, err = _body()
    if err:
        return err
    try:
        moment_id = int(data.get("moment_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "moment_id required"}), 400
    text = data.get("text")
    text = text.strip() if isinstance(text, str) else ""
    image_data = data.get("image")  # 可选 base64 dataURL
    if not text and not image_data:
        return jsonify({"error": "empty"}), 400
    if len(text) > 500:
        return jsonify({"error": "text too long (>500)"}), 400
    parent_id = data.get("parent_id")
    if parent_id is not None:
        try:
            parent_id = int(parent_id)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid parent_id"}), 400
    moment = db.get_moment(moment_id)
    if not moment:
        return jsonify({"error": "moment not found"}), 404

    image_path = None
    if image_data:
        try:
            raw = _decode_image(image_data)
            if len(raw) > MAX_IMAGE_BYTES:
                return jsonify({"error": "image too large"}), 400
            image_path = _save_upload("comments", "cmt", raw)
        except Exception as e:
            return _image_failed(e)

    bot_id = moment["bot_id"]
    user_display = _user_display_name()

    # 决定通知谁：
    # 1. 在 bot 朋友圈下评论 → 只触发该 bot 回复
    # 2. 在用户主页朋友圈下回复某条 bot 评论 → 只触发该 bot 回复
    # 3. 在用户主页朋友圈下普通评论（parent=None 或 parent 是用户自己）→ 通知每个在跑的人，没有单个等待回复的人
    # 父评论找不到（不在这条动态下）时哪一种都不算，不通知。
    target_bot = None
    to_everyone = False
    if bot_id != USER_PROFILE_KEY:
        target_bot = bot_id
    elif parent_id:
        parent_row = next((c for c in db.list_comments(moment_id) if c["id"] == parent_id), None)
        pu = parent_row and parent_row["from_user"]
        if pu in (user_display, USER_PROFILE_KEY):
            to_everyone = True  # 回复自己的评论，同普通评论
        elif pu:
            target_bot = pu  # 回复某个 bot 的评论 → 触发该 bot
    else:
        to_everyone = True

    pending = bool(target_bot)
    cid = db.add_comment(moment_id, user_display, text, parent_id=parent_id,
                         pending=pending, image_path=image_path)

    if to_everyone:
        # 评论已存下；投递怎么出错都不影响它，也不影响返回
        try:
            _notify_all_running("moment_reply", _trigger_bot_see_user_comment, moment, cid, text, image_path)
        except Exception:
            _log.exception("朋友圈页：通知在跑的人看评论失败")

    if target_bot:
        delivered = False
        try:
            caps, entries = _probe()
            people = _pick_people(entries)
            entry = people.get(target_bot)
            # 这个人没在跑（停用、没有、坏了）就没人会回
            if entry is not None and entry["state"] in RUNNING_STATES:
                names = {p: e["display_name"] for p, e in people.items()}
                delivered = _trigger_bot_moment_reply(entry, caps, names, moment, text, cid)
        except Exception:
            _log.exception("朋友圈页：触发 %s 回复评论失败", target_bot)
        if not delivered:
            db.mark_pending(cid, False)  # 别让评论一直挂"待回复"

    return jsonify({
        "id": cid, "ts": int(time.time()),
        "from": user_display, "text": text, "pending": pending,
        "parent_id": parent_id, "image_path": image_path, "image_url": _image_url(image_path),
    })


def _clip(s: str, limit: int) -> str:
    """超过 limit 个字符就只留前 limit 个，以"…"结尾。"""
    return s if len(s) <= limit else s[:limit] + "…"


def _comment_lines(moment_id: int, exclude_id: int, label_of) -> list:
    """这条动态下除 exclude_id 外的评论，按 (ts, id) 从早到晚（同一秒按编号先后，"最近"才有准），只留最近
    SEE_COMMENT_MAX_OTHERS 条；正文换行换空格、超长截断。label_of(from_user) 给每条的说话人。"""
    lines = []
    for c in sorted(db.list_comments(moment_id), key=lambda c: (c["ts"] or 0, c["id"])):
        if c["id"] == exclude_id:
            continue
        body = _clip(_LINE_BREAK.sub(" ", c["text"] or ""), SEE_COMMENT_OTHER_CHARS)
        lines.append(f"  {label_of(c['from_user'])}: {body}")
    return lines[-SEE_COMMENT_MAX_OTHERS:]


def _fit(render, others: list, said: str) -> str:
    """整条超 SEE_COMMENT_MAX_BYTES：先从最早的其他评论删起；删光仍超，再把这条评论截成放得下的最长前缀、
    以"…"结尾。命令与规则几行不动。"""
    text = render(others, said)
    while others and len(text.encode("utf-8")) > SEE_COMMENT_MAX_BYTES:
        others = others[1:]
        text = render(others, said)
    if len(text.encode("utf-8")) > SEE_COMMENT_MAX_BYTES:
        room = SEE_COMMENT_MAX_BYTES - len(render([], "…").encode("utf-8"))
        text = render([], said.encode("utf-8")[:max(room, 0)].decode("utf-8", "ignore") + "…")
    return text


def _trigger_bot_moment_reply(entry, caps, names: dict, moment: dict, user_text: str, comment_id: int) -> bool:
    """评论要这只 bot 回复：拼通知文字，经适配器投递。names 是 {朋友圈名: 显示名}，历史评论里别的 bot 按它标。"""
    w = _address_words(entry, caps)
    addr = w["addr"]
    visibility_label = "私密" if (moment.get("visibility") or "public") == "private" else "公开"
    moment_text = (moment.get("text") or "")[:200]
    bot_id = entry["person"]
    user_display = _user_display_name()

    def label_of(who):
        if who == bot_id:
            return "你"
        if who in (USER_PROFILE_KEY, user_display):
            return addr
        return names.get(who, who)

    history = _comment_lines(moment["id"], comment_id, label_of)

    is_public = (moment.get("visibility") or "public") != "private"
    public_warn = (
        f"这条朋友圈是公开的，所有朋友都能看到评论。{w['public_warn']}，就当回复一个普通朋友的评论。\n"
        "如果之前的历史评论里已经泄密了亲密称呼，用 Bash 工具删掉：\n"
        f"  {shell_path(PYTHON_BIN)} {shell_path(MOMENT_DELETE_COMMENT_SCRIPT)} <comment_id>\n"
    ) if is_public else (
        f"（这是私密朋友圈，{w['private_note']}。）\n"
    )

    def render(shown: list, said: str) -> str:
        history_block = (
            "\n这条朋友圈下的历史评论（按时间从早到晚）：\n" + "\n".join(shown) + "\n"
            if shown else ""
        )
        return (
            f"[moment-interaction] {addr}在你的朋友圈下评论了：\n"
            f"\"{said}\"\n\n"
            f"原朋友圈（{visibility_label}）：\"{moment_text}\"\n"
            f"{history_block}\n"
            f"{public_warn}\n"
            f"用 Bash 工具回写朋友圈评论：\n"
            f"{shell_path(PYTHON_BIN)} {shell_path(MOMENT_REPLY_SCRIPT)} {moment['id']} {comment_id} \"<你的回复内容>\" [--image <图绝对路径>]\n\n"
            f"可以配图：评论场景适合配图时（{addr} 让你发图、自拍、晒东西、画面感强），"
            f"先用 novelai-skill 生图，再加 --image <路径> 参数。\n"
            f"\n"
            f"如果 {addr} 在评论里说了「私发/私聊/发我/单独发我」这类话：\n"
            f"- 同时用 reply() 工具把内容（含图）发到 telegram 私聊\n"
            f"- 朋友圈评论里只回一句简短的「私发了/已发」表态\n"
            f"- 其他情况下不要在 telegram 私聊里说话\n"
            f"\n"
            f"回复要求：\n"
            f"- 符合人设和当下心情；公开圈不暴露亲密关系\n"
            f"- 顺着历史评论来，不要重复\n"
            f"- 不用「你」指代任何人，直接称呼或省略主语\n"
            f"- 朋友圈评论一句话即可"
        )

    text = _fit(render, history, user_text)
    return _notify(entry, caps, "moment_reply", text, f"moment-reply:{moment['id']}:{comment_id}")


def _trigger_bot_see_user_comment(entry, caps, running_names: dict, moment: dict, comment_id: int,
                                  user_text: str, image_path) -> bool:
    """通知一个 bot：用户在自己的动态下发了条普通评论（没回复哪只 bot），要不要接话 ta 自己决定。

    类型、去重键与评论回复相同。前缀沿用 [moment-interaction]：chat_history 的注入标记表靠它认出这是
    合成消息、不是真人发言，换新前缀会被当成用户说话。长度限制见 SEE_COMMENT_* 的注释。"""
    w = _address_words(entry, caps)
    addr = w["addr"]
    bot_id = entry["person"]
    user_display = _user_display_name()
    label = "私密（仅你可见）" if (moment.get("visibility") or "public") == "private" else "公开"
    moment_text = _clip(moment.get("text") or "", SEE_COMMENT_MOMENT_CHARS)
    image_note = f"\n（{addr} 还附了张图：{image_path}）" if image_path else ""

    def label_of(who):
        if who == bot_id:
            return "你"
        if who in (USER_PROFILE_KEY, user_display):
            return addr
        return running_names.get(who, who)

    others = _comment_lines(moment["id"], comment_id, label_of)

    def render(shown: list, said: str) -> str:
        history_block = (
            "\n这条朋友圈下的其他评论（按时间从早到晚）：\n" + "\n".join(shown) + "\n"
            if shown else ""
        )
        return (
            f"[moment-interaction] {addr}在自己的朋友圈下评论了：\n"
            f"\"{said}\"{image_note}\n\n"
            f"原朋友圈（{label}）：\"{moment_text}\"\n"
            f"{history_block}\n"
            f"要不要接话你自己决定：\n"
            f"- 想在评论里接话，用 Bash 工具回写朋友圈评论：\n"
            f"  {shell_path(PYTHON_BIN)} {shell_path(MOMENT_REPLY_SCRIPT)} {moment['id']} {comment_id} \"<你的回复内容>\" [--image <图绝对路径>]\n"
            f"  可以配图：评论场景适合配图时（{addr} 让你发图、自拍、晒东西、画面感强），"
            f"先用 novelai-skill 生图，再加 --image <路径> 参数。\n"
            f"{DM_RULE.format(addr=addr)}\n"
            f"- 没话想说就什么都不做\n"
            f"\n"
            f"回复要求：\n"
            f"- 已有其他人评过 → 不要重复同样的意思；可以接话或换角度\n"
            f"- {w['no_you_rule']}\n"
            f"- 公开动态不暴露亲密关系\n"
            f"- 朋友圈评论一句话即可"
        )

    text = _fit(render, others, _LINE_BREAK.sub(" ", user_text))
    return _notify(entry, caps, "moment_reply", text, f"moment-reply:{moment['id']}:{comment_id}")


@moments_bp.route("/api/moment", methods=["POST"])
def api_post_user_moment():
    """用户在主页发朋友圈。每个在跑的人收到一条通知，要不要评论 ta 自己决定。这里发动态不触发配图。"""
    data, err = _body()
    if err:
        return err
    text = data.get("text")
    text = text.strip() if isinstance(text, str) else ""
    visibility = data.get("visibility") or "public"
    if visibility not in ("public", "private"):
        visibility = "public"
    image_data = data.get("image")  # 可选 base64 dataURL
    if not text and not image_data:
        return jsonify({"error": "empty"}), 400
    if len(text) > 2000:
        return jsonify({"error": "text too long (>2000)"}), 400

    image_path = None
    if image_data:
        try:
            raw = _decode_image(image_data)
            if len(raw) > MAX_IMAGE_BYTES:
                return jsonify({"error": "image too large (>8MB raw)"}), 400
            image_path = _save_upload("auto", "user", raw)
        except Exception as e:
            return _image_failed(e)

    user_display = _user_display_name()
    moment_id = db.insert_moment(
        bot_id=USER_PROFILE_KEY, ts=int(time.time()),
        text=text, image_path=image_path,
        metadata={"author_display": user_display},
        kind="user_post", visibility=visibility,
    )

    # 每个在跑的人各投一份，并行投、全部投完才返回（名单与投法见 _notify_all_running）
    _notify_all_running("user_moment", _trigger_bot_see_user_moment, moment_id, text, image_path, visibility)

    return jsonify({"id": moment_id, "image_path": image_path,
                    "visibility": visibility, "text": text})


def _trigger_bot_see_user_moment(entry, caps, running_names: dict, moment_id: int, text: str,
                                 image_path, visibility: str) -> bool:
    """通知一个 bot：用户发了一条朋友圈，要不要评论 ta 自己决定。

    同时给它看该动态现有的所有评论，避免重复其他 bot 已经说过的话。"""
    w = _address_words(entry, caps)
    addr = w["addr"]
    bot_id = entry["person"]
    user_display = _user_display_name()
    label = "私密（仅你可见）" if visibility == "private" else "公开"
    image_note = f"\n（{addr} 还附了张图：{image_path}）" if image_path else ""

    others = []
    for c in db.list_comments(moment_id):
        if c["from_user"] == bot_id:
            continue  # 自己之前评过的不列
        if c["from_user"] == USER_PROFILE_KEY or c["from_user"] == user_display:
            speaker = addr
        else:
            # 其他 bot 的显示名：只认在跑的人
            speaker = running_names.get(c["from_user"], c["from_user"])
        others.append(f"  {speaker}: {c['text']}")
    history_block = (
        "\n这条朋友圈下其他人已经评过的：\n" + "\n".join(others) + "\n"
        if others else ""
    )

    # 用户最近 7 天的朋友圈（让 bot 知道用户最近在想啥、做啥，建立连贯感）
    seven_days_ago = int(time.time()) - 7 * 86400
    user_recent = db.list_moments(bot_id=USER_PROFILE_KEY, limit=20, since_ts=seven_days_ago)
    user_history_block = ""
    if user_recent:
        items = [m for m in user_recent if m["id"] != moment_id][:8]
        if items:
            lines = [f"  · [{datetime.fromtimestamp(m['ts']).strftime('%m-%d %H:%M')}] {(m['text'] or '')[:60]}"
                     for m in items]
            user_history_block = (
                f"\n{addr} 最近 7 天发过的其他朋友圈（供参考，不要复述）：\n"
                + "\n".join(lines) + "\n"
            )

    inbox_text = (
        f"[user-moment] {addr} 刚发了一条朋友圈（{label}）：\n"
        f"\"{text}\"{image_note}\n"
        f"{history_block}{user_history_block}\n"
        f"你看到了，可以从下面四种做法里挑一个：\n"
        f"  A. 只点赞（看到了但没话说，最常见的轻互动）：\n"
        f"     {shell_path(PYTHON_BIN)} {shell_path(MOMENT_LIKE_SCRIPT)} {moment_id}\n"
        f"  B. 只评论：\n"
        f"     {shell_path(PYTHON_BIN)} {shell_path(MOMENT_REPLY_SCRIPT)} {moment_id} 0 \"<你的评论>\" [--image <图>]\n"
        f"  C. 点赞+评论（确实有话想说）：先调 A 再调 B\n"
        f"  D. 什么都不做（无感、跟自己没关系）\n"
        f"\n"
        f"按你的人设判断：\n"
        f"- 内容真戳到你、想呼应 → C\n"
        f"- 内容轻量好玩但没特别想说 → A\n"
        f"- 看不太懂、跟你无关 → D\n"
        f"- 私密内容（仅你可见的）→ 倾向 C\n"
        f"- 已有其他人评过 → 不要重复同样的意思；可以接话或换角度，或者改成 A 点赞\n"
        f"{DM_RULE.format(addr=addr)}\n"
        f"- {w['no_you_rule']}"
    )
    return _notify(entry, caps, "user_moment", inbox_text, f"user-moment:{moment_id}")


@moments_bp.route("/api/comment/<int:comment_id>", methods=["DELETE"])
def api_delete_comment(comment_id):
    """删除评论。只允许删除署名为 user_display_name 的评论（不能删 bot 的回复）。"""
    ok = db.delete_comment(comment_id, only_from=_user_display_name())
    if not ok:
        return jsonify({"error": "comment not found or not yours"}), 404
    return jsonify({"ok": True, "id": comment_id})


# 删动态时能删的配图只在这两个目录里：用户在朋友圈页发动态、发带图评论时上传的图存在这两处。
# 图片根里的别的东西是别的功能的原图（生图记录、画风样图、__user__/profile/ 的头像封面），
# bot 评论的配图路径又是原样入库，所以这两处以外一律不删。
_USER_UPLOAD_DIRS = ((USER_PROFILE_KEY, "auto"), (USER_PROFILE_KEY, "comments"))


def _remove_orphan_images(paths) -> tuple:
    """删掉已删动态及其评论引用过、库里也再没有别处引用的用户上传图，返回 (删掉的个数, 删不掉的个数)。

    只删 realpath 后落在图片根下 __user__/auto/、__user__/comments/ 里的普通文件，这两处以外的一律不动，
    包括 bot 评论引用的图与 __user__/profile/ 的头像封面。两个目录按图片根真实路径下的字面位置认，
    路径经符号链接或 .. 解析到这两处以外的整条跳过（链接本身也不动）；库里的非绝对路径不知相对谁，同样跳过。
    是否"别处引用"按文件身份（设备号 + inode）比，库里换个写法、经链接指到同一个文件也算引用。
    文件本来就不在不算错。
    """
    root = _media_root()
    upload_dirs = tuple(os.path.join(root, *parts) for parts in _USER_UPLOAD_DIRS)
    targets = {}
    for p in paths:
        if not isinstance(p, str) or not os.path.isabs(p):
            continue
        try:
            real = os.path.realpath(p)
            if not any(_under(real, d) for d in upload_dirs):
                continue
            st = os.stat(real)
        except (OSError, ValueError):
            continue
        if stat.S_ISREG(st.st_mode):
            targets[(st.st_dev, st.st_ino)] = real
    if targets:
        for ref in db.moment_image_refs():
            try:
                st = os.stat(ref)
            except (OSError, ValueError, TypeError):
                continue
            targets.pop((st.st_dev, st.st_ino), None)
    removed = failed = 0
    for real in targets.values():
        try:
            os.unlink(real)
            removed += 1
        except FileNotFoundError:
            pass
        except OSError:
            failed += 1
    return removed, failed


@moments_bp.route("/api/moment/<int:moment_id>", methods=["DELETE"])
def api_delete_moment(moment_id):
    """删自己发的一条动态：连同它的全部评论、点赞与用户上传的配图，删了就没了，不留记录。
    bot 发的不给删；bot 评论只删库里的行，它引用的图片不在用户上传目录里就不动。日志只记编号与计数，不记正文。"""
    # db.delete_moment_cascade 只报动态的 image_path 与评论的图；metadata 里的多图要在删行之前自己取
    row = db.get_moment(moment_id) if 0 <= moment_id <= 2 ** 63 - 1 else None
    res = db.delete_moment_cascade(moment_id, USER_PROFILE_KEY)
    if res["status"] == "missing":
        return jsonify({"error": "这条动态不存在或已经删了"}), 404
    if res["status"] == "forbidden":
        return jsonify({"error": "只能删自己发的动态"}), 403
    extra = _format_moment(row)["image_paths"] if row and row.get("bot_id") == USER_PROFILE_KEY else []
    files, failed = _remove_orphan_images(list(res["image_paths"]) + list(extra))
    _log.info("朋友圈页：已删除动态 %d，评论 %d 条，点赞 %d 个，图片文件 %d 个（删不掉 %d 个）",
              moment_id, res["comments"], res["likes"], files, failed)
    return jsonify({"ok": True, "id": moment_id})


def _profile_key_ok(bot_id) -> bool:
    """资料接口只收用户主页键或合规的 bot_id，免得任意字符串都在库里建一行。"""
    return bot_id == USER_PROFILE_KEY or valid_bot_id(bot_id)


@moments_bp.route("/api/profile/<bot_id>", methods=["GET"])
def api_get_profile(bot_id):
    if not _profile_key_ok(bot_id):
        return jsonify({"error": "not found"}), 404
    return jsonify(db.get_profile(bot_id))


def _image_url_ok(v) -> bool:
    """空串（= 清除）、http/https 链接、本站 /image/<路径>，其余一律不收。"""
    if v == "":
        return True
    if not isinstance(v, str) or len(v) > IMAGE_URL_MAX or any(c in _IMAGE_URL_BAD for c in v):
        return False
    if v.startswith("/image/"):
        return len(v) > len("/image/")
    try:
        parts = urlsplit(v)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


@moments_bp.route("/api/profile/<bot_id>", methods=["POST"])
def api_set_profile(bot_id):
    if not _profile_key_ok(bot_id):
        return jsonify({"error": "not found"}), 404
    data, err = _body()
    if err:
        return err
    unknown = sorted(k for k in data if k not in PROFILE_KEYS)
    if unknown:
        return jsonify({"error": "不认识的字段：%s" % "、".join(unknown), "unknown_keys": unknown}), 422
    # 先把所有输入核完再动手写：哪一项不合规都回错误，图片不落盘、资料一项都不写。
    # 换头像弹窗的"上传文件"传来 data:image/…;base64,…：解码后 ≤8 MiB 且文件头是真图片才收，存进图片根，
    # 资料里改记本站 /image/… 地址，这样资料里只会有链接或本站地址。
    uploads = {}
    for k in ("avatar_url", "banner_url"):
        v = data.get(k)
        if isinstance(v, str) and v.startswith("data:image/"):
            try:
                raw = _decode_image(v)
                if len(raw) > MAX_IMAGE_BYTES or _image_ext(raw) is None:
                    raise ValueError("not an image or too large")
            except Exception as e:
                return _image_failed(e)
            uploads[k] = raw
    for k in ("avatar_url", "banner_url"):
        v = data.get(k)
        if k not in uploads and v is not None and not _image_url_ok(v):
            return jsonify({"error": "%s 只收 http/https 链接或本站 /image/ 地址（%d 字符以内，"
                                     "不含引号、括号、反斜杠与换行）" % (k, IMAGE_URL_MAX)}), 400
    new_name = None
    if "display_name" in data:
        if not isinstance(data["display_name"], (str, type(None))):
            return jsonify({"error": "display_name 必须是字符串"}), 400
        new_name = (data.get("display_name") or "").strip()
        if len(new_name) > 30:
            return jsonify({"error": "display_name too long (>30)"}), 400

    values = {k: data.get(k) for k in ("avatar_url", "banner_url")}
    for k, raw in uploads.items():
        try:
            full = _save_upload("profile", "%s_%s" % (k.split("_")[0], bot_id), raw)
            values[k] = _image_url(full)
            if values[k] is None:
                raise ValueError("stored outside media root")
        except Exception as e:
            return _image_failed(e)
    if values["avatar_url"] is not None or values["banner_url"] is not None:
        db.set_profile(bot_id, avatar_url=values["avatar_url"], banner_url=values["banner_url"])
    if new_name is not None:
        db.set_display_name(bot_id, new_name)
    return jsonify({"ok": True, **db.get_profile(bot_id)})


@moments_bp.route("/api/moment/<int:moment_id>")
def api_moment_detail(moment_id):
    m = db.get_moment(moment_id)
    if not m:
        return jsonify({"error": "not found"}), 404
    out = _format_moment(m)
    out["likers"] = [r["liker"] for r in db.list_likers(moment_id)]
    out["comments"] = db.list_comments(moment_id)
    return jsonify(out)


# merge_slashes=False：/image//abs/path 这类带 // 的路径直接不匹配（回 404），不让框架先 308 跳到合并后的地址
@moments_bp.route("/image/<path:filename>", merge_slashes=False)
def serve_image(filename):
    """filename 是相对图片根的路径（/ 分隔）。拼回后 realpath 必须在图片根之下、且是已有的普通文件，
    文件头还得是 PNG/JPEG/GIF/WEBP 之一；绝对路径、盘符路径、.. 越界、经符号链接指到根外、目录、不存在、
    不是这四种图片（HTML、SVG、文本、空文件、截断的假头）一律 404。"""
    if filename.startswith(("/", "\\")) or os.path.isabs(filename) or os.path.splitdrive(filename)[0]:
        abort(404)
    root = _media_root()
    real = os.path.realpath(os.path.join(root, *filename.split("/")))
    if not _under(real, root) or not os.path.isfile(real):
        abort(404)
    # 图片根里混进的 HTML/SVG 若照扩展名发出，会在管理台同源下执行脚本：只发文件头认得出的四种图片，
    # Content-Type 也按文件头定，与扩展名无关
    try:
        with open(real, "rb") as f:
            kind = sticker_store.sniff(f.read(12))
    except OSError:
        abort(404)
    if kind is None:
        abort(404)
    resp = send_file(real, mimetype=sticker_store.MIMETYPES[kind], conditional=True)
    # 图片内容不会变（文件名带时间戳与随机后缀），浏览器可永久缓存；只许私有缓存，免得经反向代理/CDN 暴露时
    # 边缘节点把登录后才能看的图缓存下来发给未登录者；不许浏览器另行嗅探类型
    resp.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp
