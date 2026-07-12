"""朋友圈 web 服务（Flask）。

启动：python3 -m moments.web
访问：http://localhost:8765
"""
import os
import sys
import json
import time
import secrets
import subprocess
from datetime import datetime, timezone
from flask import Flask, render_template, send_from_directory, request, jsonify

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import config_loader
from moments.styles_routes import styles_bp

app = Flask(__name__,
            template_folder=os.path.join(os.path.dirname(__file__), "templates"))
app.register_blueprint(styles_bp)

USER_ADDRESS_FALLBACK = "哥哥"
USER_DISPLAY_FALLBACK = "我"
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOMENT_REPLY_SCRIPT = os.path.join(_REPO_ROOT, "scripts", "moment_reply.py")
PYTHON_BIN = os.environ.get("CLAUDEBOTLIFE_PYTHON", sys.executable)


def _user_display_name() -> str:
    g = config_loader.load_global()
    return g.get("user_display_name") or USER_DISPLAY_FALLBACK


USER_PROFILE_KEY = "__user__"


def _user_meta() -> dict:
    """全部 tab 用 = 用户自己的朋友圈主页资料"""
    g = config_loader.load_global()
    profile = db.get_profile(USER_PROFILE_KEY)
    return {
        "id": USER_PROFILE_KEY,
        "name": g.get("user_display_name") or USER_DISPLAY_FALLBACK,
        "bio": g.get("user_bio", ""),
        "user_address": "",     # 用户主页时不需要
        "avatar_url": profile.get("avatar_url"),
        "banner_url": profile.get("banner_url"),
    }


def _format_moment(m: dict) -> dict:
    metadata = {}
    try:
        metadata = json.loads(m.get("metadata_json", "{}") or "{}")
    except Exception:
        pass
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
        "image_paths": metadata.get("image_paths") or ([m["image_path"]] if m.get("image_path") else []),
        "kind": m.get("moment_kind"),
        "visibility": m.get("visibility") or "public",
        "metadata": metadata,
        **_chapter_for(dt),
    }


_WEEKDAY_ZH = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def _chapter_for(dt: datetime) -> dict:
    """根据日期分组：今天 / 昨天 / 本周 / YYYY-MM"""
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
    diff = int(time.time()) - ts
    if diff < 60: return f"{diff}秒前"
    if diff < 3600: return f"{diff // 60}分钟前"
    if diff < 86400: return f"{diff // 3600}小时前"
    return f"{diff // 86400}天前"


def _bot_meta(b: dict) -> dict:
    bot_id = b["_bot_id"]
    profile = db.get_profile(bot_id)
    signature = profile.get("signature") or b.get("bio", "")
    # 名字优先 db.display_name（用户改的），fallback yml display_name
    name = profile.get("display_name") or b.get("display_name", bot_id)
    return {
        "id": bot_id,
        "name": name,
        "bio": signature,
        "user_address": b.get("user_address", USER_ADDRESS_FALLBACK),
        "avatar_url": profile.get("avatar_url"),
        "banner_url": profile.get("banner_url"),
    }


PAGE_SIZE = 30


@app.route("/")
def feed():
    bot_filter = request.args.get("bot")
    # 默认进"主页"（用户自己的朋友圈）；切到某 bot 才看 ta 的
    if not bot_filter:
        bot_filter = USER_PROFILE_KEY
    try:
        page = max(1, int(request.args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    moments_raw = db.list_moments(bot_id=bot_filter,
                                   limit=PAGE_SIZE * page + 1)
    has_more = len(moments_raw) > PAGE_SIZE * page
    moments_raw = moments_raw[:PAGE_SIZE * page]
    moments = [_format_moment(m) for m in moments_raw]

    # 批量取点赞 + 评论
    ids = [m["id"] for m in moments]
    likes_map = db.likers_bulk(ids)
    comments_map = db.comments_bulk(ids)
    bots = config_loader.list_enabled_bots()
    bot_meta_by_id = {b["_bot_id"]: _bot_meta(b) for b in bots}
    # 用户自己作为"虚拟 bot"，其朋友圈卡片头像/名字也走这里
    bot_meta_by_id[USER_PROFILE_KEY] = _user_meta()

    def _name_for(uid: str) -> str:
        """反查显示名：bot 用 db.display_name fallback yml display_name；用户用 user_display_name"""
        if uid in bot_meta_by_id:
            return bot_meta_by_id[uid].get("name") or uid
        return uid

    for m in moments:
        m["likers"] = likes_map.get(m["id"], [])
        bot_meta = bot_meta_by_id.get(m["bot_id"], {})
        m["bot_meta"] = bot_meta
        raw_comments = comments_map.get(m["id"], [])
        by_id = {c["id"]: c for c in raw_comments}
        formatted = []
        for c in raw_comments:
            formatted.append({
                **c,
                "from_user_label": _name_for(c["from_user"]),
                "parent_label": _name_for(by_id[c["parent_id"]]["from_user"])
                                 if c.get("parent_id") and c["parent_id"] in by_id else None,
                "pending": bool(c.get("pending")),
            })
        m["comments"] = formatted

    # 当前 bot meta（顶部 banner/avatar 用）
    if bot_filter in bot_meta_by_id:
        active_meta = bot_meta_by_id[bot_filter]
    else:
        active_meta = _user_meta()

    g_cfg = config_loader.load_global()
    img_provider = (g_cfg.get("moments", {}) or {}).get("image_generation", {}).get("provider", "novelai")

    return render_template(
        "feed.html",
        moments=moments,
        bots=[v for k, v in bot_meta_by_id.items() if k != USER_PROFILE_KEY],
        active_bot=bot_filter,
        active_meta=active_meta,
        user_display=_user_display_name(),
        page=page,
        has_more=has_more,
        image_provider=img_provider,
    )


@app.route("/api/moments")
def api_moments():
    bot_filter = request.args.get("bot")
    since = int(request.args.get("since", 0))
    limit = int(request.args.get("limit", 50))
    moments = db.list_moments(bot_id=bot_filter, limit=limit, since_ts=since)
    return jsonify([_format_moment(m) for m in moments])


# ─── 互动 API ──────────────────────────────────────────────
@app.route("/api/like", methods=["POST"])
def api_like():
    data = request.get_json(silent=True) or {}
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


@app.route("/api/comment", methods=["POST"])
def api_comment():
    import base64
    data = request.get_json(silent=True) or {}
    try:
        moment_id = int(data.get("moment_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "moment_id required"}), 400
    text = (data.get("text") or "").strip()
    image_data = data.get("image")  # 可选 base64 dataURL
    if not text and not image_data:
        return jsonify({"error": "empty"}), 400
    if len(text) > 500:
        return jsonify({"error": "text too long (>500)"}), 400
    parent_id = data.get("parent_id")
    if parent_id is not None:
        try: parent_id = int(parent_id)
        except (TypeError, ValueError):
            return jsonify({"error": "invalid parent_id"}), 400
    moment = db.get_moment(moment_id)
    if not moment:
        return jsonify({"error": "moment not found"}), 404

    # 处理用户上传的图片
    image_path = None
    if image_data and image_data.startswith("data:image/"):
        try:
            header, b64 = image_data.split(",", 1)
            ext = "jpg" if "jpeg" in header or "jpg" in header else \
                  ("png" if "png" in header else "webp")
            raw = base64.b64decode(b64)
            if len(raw) > 8 * 1024 * 1024:
                return jsonify({"error": "image too large"}), 400
            save_dir = os.path.expanduser("~/resource/media/__user__/comments")
            os.makedirs(save_dir, exist_ok=True)
            fname = f"cmt_{int(time.time())}_{secrets.token_hex(3)}.{ext}"
            full = os.path.join(save_dir, fname)
            with open(full, "wb") as f: f.write(raw)
            image_path = full
        except Exception as e:
            return jsonify({"error": f"image decode failed: {e}"}), 400

    bot_id = moment["bot_id"]
    user_display = _user_display_name()

    # 决定要不要触发某个 bot 回复，触发哪个：
    # 1. 在 bot 朋友圈下评论 → 触发该 bot
    # 2. 在用户主页朋友圈下回复某条 bot 评论 → 触发该 bot
    # 3. 在用户主页朋友圈下普通评论（parent=None 或 parent 是用户自己）→ 不触发
    target_bot = None
    if bot_id != USER_PROFILE_KEY:
        target_bot = bot_id
    elif parent_id:
        parent_row = db.list_comments(moment_id)
        parent_row = next((c for c in parent_row if c["id"] == parent_id), None)
        pu = parent_row and parent_row["from_user"]
        if pu and pu != user_display and pu != USER_PROFILE_KEY:
            target_bot = pu  # 回复某个 bot 的评论 → 触发该 bot

    pending = bool(target_bot)
    cid = db.add_comment(moment_id, user_display, text, parent_id=parent_id,
                          pending=pending, image_path=image_path)

    if target_bot:
        try:
            cfg = config_loader.load_bot(target_bot)
            _trigger_bot_moment_reply(cfg, moment, text, cid, user_display)
        except Exception as e:
            db.mark_pending(cid, False)
            print(f"[api_comment] trigger {target_bot} failed: {e}", flush=True)

    return jsonify({
        "id": cid, "ts": int(time.time()),
        "from": user_display, "text": text, "pending": pending,
        "parent_id": parent_id, "image_path": image_path,
    })


def _trigger_bot_moment_reply(cfg: dict, moment: dict, user_text: str,
                              comment_id: int, user_display: str):
    """写 inbox JSON 让 dispatcher 唤起 worker；worker 通过 Bash 调脚本回写朋友圈。"""
    bot_dir = cfg["bot_channel_path"]
    chat_id = str(cfg["chat_id"])
    inbox = os.path.join(bot_dir, "chats", chat_id, "inbox")
    os.makedirs(inbox, exist_ok=True)

    user_address = cfg.get("user_address", USER_ADDRESS_FALLBACK)
    visibility_label = "私密" if (moment.get("visibility") or "public") == "private" else "公开"
    moment_text = (moment.get("text") or "")[:200]
    bot_id = cfg.get("id") or cfg.get("_bot_id")

    # 取该 moment 当前所有评论（含历史 + 你刚发的这条），让 bot 看到完整上下文
    # 排除 comment_id 自己（因为下面要单独突出"最新这条"）
    all_comments = db.list_comments(moment["id"])
    history_lines = []
    for c in all_comments:
        if c["id"] == comment_id:
            continue  # 最新这条单独写
        speaker = "你" if c["from_user"] == bot_id else user_address
        history_lines.append(f"  {speaker}: {c['text']}")
    history_block = (
        "\n【这条朋友圈下的历史评论（按时间从早到晚）】\n" + "\n".join(history_lines) + "\n"
        if history_lines else ""
    )

    is_public = (moment.get("visibility") or "public") != "private"
    _del_script = os.path.join(_REPO_ROOT, "scripts", "moment_delete_comment.py")
    public_warn = (
        "⚠️【公开朋友圈泄密风险】\n"
        f"原朋友圈是公开的，所有朋友都能看到评论。绝对不要在回复正文里用「{user_address}」这种亲密称呼，\n"
        f"也不要透露你和 {user_address} 的私密关系。装作和 ta 是普通朋友、像回复任何一个评论一样。\n"
        "如果发现自己之前的回复（在【历史评论】里）已经泄密了亲密称呼，请用 Bash 工具删除：\n"
        f"  {PYTHON_BIN} {_del_script} <comment_id>\n"
    ) if is_public else (
        "（这是私密朋友圈，仅 " + user_address + " 可见，可以放开亲密称呼。）\n"
    )

    text = (
        f"[moment-interaction] {user_address}在你的朋友圈下评论了：\n"
        f"\"{user_text}\"\n\n"
        f"原朋友圈（id={moment['id']}, {visibility_label}）：\"{moment_text}\"\n"
        f"{history_block}\n"
        f"{public_warn}\n"
        f"请用 Bash 工具回写朋友圈评论：\n"
        f"{PYTHON_BIN} {MOMENT_REPLY_SCRIPT} {moment['id']} {comment_id} \"<你的回复内容>\" [--image <图绝对路径>]\n\n"
        f"【可附图（推荐配合人设需要时）】\n"
        f"- 如果评论场景适合配图（{user_address} 让你发图、自拍、晒东西、画面感强），\n"
        f"  先用 novelai-skill 生图，再加 --image <路径> 参数\n"
        f"\n"
        f"【👀 如果 {user_address} 在评论里说了「私发/私聊/发我/单独发我」等】\n"
        f"- 这种情况下你**同时**用 reply() 工具发到 telegram 私聊（含图）\n"
        f"- 朋友圈评论里只回一句简短的「私发了/已发」等表态，但真正的内容/图走 telegram\n"
        f"- 否则不要在 telegram 私聊里说话\n"
        f"\n"
        f"【其他要求】\n"
        f"- 回复要符合人设和当下心情；公开圈不暴露亲密关系\n"
        f"- 顺着历史评论来，不要重复\n"
        f"- 不用「你」指代任何人，直接称呼或省略主语\n"
        f"- 朋友圈评论一句话即可"
    )
    _ = user_display

    ms = int(time.time() * 1000)
    fname = os.path.join(inbox, f"moment-reply-{ms}.json")
    iso_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    payload = {
        "text": text,
        "chat_id": chat_id,
        "from_id": chat_id,
        "from_username": "user",
        "sender_username": "user",
        "chat_type": "private",
        "is_bot_sender": False,
        "ts": iso_ts,
        "message_id": str(ms),
    }
    with open(fname, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    # 关键：worker 没活就 spawn（dispatcher 监听的是 telegram 不是 inbox）
    _ensure_worker_alive(cfg["_bot_id"] if "_bot_id" in cfg else cfg.get("id"),
                        chat_id, bot_dir)


# bot 端口映射（每个 bot 的 dispatcher 端口；加 bot 就在这里加一行）
_BOT_PORTS = {"chenlulu": "17801"}


def _ensure_worker_alive(bot_id: str, chat_id: str, bot_dir: str):
    """POST 该 bot dispatcher 的 /ensure_worker：查活+拉起原子完成（跨平台，替代 tmux）。
    session uuid/slug 由 dispatcher 内部算，这里不再猜（旧版按 mtime 猜 uuid + 手拼
    slug 是丢记忆隐患，且旧 per-chat 会话名根本匹配不上 unified worker）。"""
    import urllib.request
    port = _BOT_PORTS.get(bot_id)
    if not port:
        sys.stderr.write(f"[ensure_worker] 未知 bot {bot_id}，跳过 spawn\n")
        return
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/ensure_worker", method="POST")
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        sys.stderr.write(f"[ensure_worker] {bot_id} 失败(dispatcher 未起?): {e}\n")


@app.route("/api/moment", methods=["POST"])
def api_post_user_moment():
    """用户在主页发朋友圈。三个 bot 各自异步触发评论决策。"""
    import base64
    data = request.get_json(force=True) or {}
    text = (data.get("text") or "").strip()
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
        # 写到 ~/resource/media/__user__/auto/
        save_dir = os.path.expanduser("~/resource/media/__user__/auto")
        os.makedirs(save_dir, exist_ok=True)
        if image_data.startswith("data:image/"):
            try:
                header, b64 = image_data.split(",", 1)
                ext = "jpg" if "jpeg" in header or "jpg" in header else \
                      ("png" if "png" in header else "webp")
                raw = base64.b64decode(b64)
                if len(raw) > 8 * 1024 * 1024:
                    return jsonify({"error": "image too large (>8MB raw)"}), 400
                fname = f"user_{int(time.time())}.{ext}"
                full = os.path.join(save_dir, fname)
                with open(full, "wb") as f: f.write(raw)
                image_path = full
            except Exception as e:
                return jsonify({"error": f"image decode failed: {e}"}), 400

    user_display = _user_display_name()
    moment_id = db.insert_moment(
        bot_id=USER_PROFILE_KEY, ts=int(time.time()),
        text=text, image_path=image_path,
        metadata={"author_display": user_display},
        kind="user_post", visibility=visibility,
    )

    # 触发每个 bot 异步读 + 决策评论
    for b in config_loader.list_enabled_bots():
        try:
            _trigger_bot_see_user_moment(b, moment_id, text, image_path, visibility)
        except Exception as e:
            print(f"[user_moment] trigger {b.get('_bot_id')} failed: {e}", flush=True)

    return jsonify({"id": moment_id, "image_path": image_path,
                    "visibility": visibility, "text": text})


def _trigger_bot_see_user_moment(bot_cfg: dict, moment_id: int, text: str,
                                  image_path: str, visibility: str):
    """通知一个 bot：用户发了一条朋友圈，要不要评论 ta 自己决定。

    inbox 同时给该 bot 看「该 moment 现有的所有评论」，避免:
    - 重复其他 bot 已经说过的话
    - 不知道还有谁评了
    """
    import json as _json
    bot_id = bot_cfg["_bot_id"]
    bot_dir = bot_cfg["bot_channel_path"]
    chat_id = str(bot_cfg.get("chat_id", ""))
    if not chat_id:
        return
    inbox = os.path.join(bot_dir, "chats", chat_id, "inbox")
    os.makedirs(inbox, exist_ok=True)

    user_address = bot_cfg.get("user_address", USER_ADDRESS_FALLBACK)
    user_display = _user_display_name()
    label = "私密（仅你可见）" if visibility == "private" else "公开"
    image_note = f"\n（{user_address} 还附了张图：{image_path}）" if image_path else ""

    # 读其他 bot 已经评过的（按时间排）
    others = []
    for c in db.list_comments(moment_id):
        if c["from_user"] == bot_id:
            continue  # 自己之前评过的也算，但这种情况罕见
        if c["from_user"] == USER_PROFILE_KEY or c["from_user"] == user_display:
            speaker = user_address
        else:
            # 其他 bot 的 display_name
            other_cfg = config_loader.load_bot(c["from_user"])
            speaker = other_cfg.get("display_name", c["from_user"])
        others.append(f"  {speaker}: {c['text']}")
    history_block = (
        "\n【这条朋友圈下其他人已经评过的】\n" + "\n".join(others) + "\n"
        if others else ""
    )

    # user 最近 7 天的朋友圈（让 bot 知道 user 最近在想啥/做啥，建立连贯感）
    seven_days_ago = int(time.time()) - 7 * 86400
    user_recent = db.list_moments(bot_id=USER_PROFILE_KEY, limit=20, since_ts=seven_days_ago)
    user_history_block = ""
    if user_recent:
        # 排除当前正在评论的这条
        items = [m for m in user_recent if m["id"] != moment_id][:8]
        if items:
            lines = [f"  · [{datetime.fromtimestamp(m['ts']).strftime('%m-%d %H:%M')}] {m['text'][:60]}"
                     for m in items]
            user_history_block = (
                f"\n【{user_address} 最近 7 天发过的其他朋友圈】（参考语境，不要直接复述）：\n"
                + "\n".join(lines) + "\n"
            )

    moment_like_script = MOMENT_REPLY_SCRIPT.replace("moment_reply.py", "moment_like.py")
    inbox_text = (
        f"[user-moment] {user_address} 刚发了一条朋友圈（{label}）：\n"
        f"\"{text}\"{image_note}\n"
        f"{history_block}{user_history_block}\n"
        f"你看到了。**自己决定**4 选 1（按真人朋友圈逻辑）：\n"
        f"  A. **只点赞**（看到了但没话说，最常见的轻互动）：\n"
        f"     {PYTHON_BIN} {moment_like_script} {moment_id}\n"
        f"  B. **只评论**：\n"
        f"     {PYTHON_BIN} {MOMENT_REPLY_SCRIPT} {moment_id} 0 \"<你的评论>\" [--image <图>]\n"
        f"  C. **点赞+评论**（确实有话想说）：先调 A 再调 B\n"
        f"  D. **什么都不做**（无感、跟自己没关系）：什么也不写\n"
        f"\n"
        f"判断准则（按你的人设）：\n"
        f"- 内容真戳到你/想呼应 → C（赞+评）\n"
        f"- 内容轻量好玩但没特别想说 → A（点赞）\n"
        f"- 看不太懂 / 跟你无关 → D（无视）\n"
        f"- 私密内容（仅你可见的）→ 倾向 C\n"
        f"- 已有其他人评过 → **不要重复**同样意思；可以接话或换角度，或者改成 A 点赞\n"
        f"- 不要在 telegram 私聊里说话\n"
        f"- 评论里**不要**用「你」指代 {user_address}，直接称「{user_address}」或省略主语"
    )

    ms = int(time.time() * 1000)
    fname = os.path.join(inbox, f"user-moment-{ms}-{moment_id}.json")
    iso_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    payload = {
        "text": inbox_text, "chat_id": chat_id, "from_id": chat_id,
        "from_username": "user", "sender_username": "user",
        "chat_type": "private", "is_bot_sender": False,
        "ts": iso_ts, "message_id": str(ms),
    }
    with open(fname, "w", encoding="utf-8") as f:
        _json.dump(payload, f, ensure_ascii=False)
    _ensure_worker_alive(bot_id, chat_id, bot_dir)


@app.route("/api/comment/<int:comment_id>", methods=["DELETE"])
def api_delete_comment(comment_id):
    """删除评论。只允许删除署名为 user_display_name 的评论（不能删 bot 的回复）。"""
    user_display = _user_display_name()
    ok = db.delete_comment(comment_id, only_from=user_display)
    if not ok:
        return jsonify({"error": "comment not found or not yours"}), 404
    return jsonify({"ok": True, "id": comment_id})


@app.route("/api/image_provider", methods=["GET", "POST"])
def api_image_provider():
    """切换/查询当前生图 provider（novelai|comfyui），按 scope 解耦。

    GET  /api/image_provider                → 兼容旧版，返回 provider 字段
    GET  /api/image_provider?scope=moment   → 朋友圈用什么
    GET  /api/image_provider?scope=telegram → Telegram bot 用什么
    POST {"scope":"telegram","provider":"comfyui"} → 切对应 scope；省略 scope 切兼容字段

    持久化方式：直接改 _global.yml 的 image_generation 下三个字段之一。
    """
    global_path = config_loader.GLOBAL_CFG_PATH
    g = config_loader.load_global()
    img = g.setdefault("moments", {}).setdefault("image_generation", {})

    if request.method == "GET":
        scope = request.args.get("scope", "").strip()
        if scope == "moment":
            p = img.get("provider_moment") or img.get("provider", "novelai")
        elif scope == "telegram":
            p = img.get("provider_telegram") or img.get("provider", "novelai")
        else:
            p = img.get("provider", "novelai")
        return jsonify({"provider": p, "scope": scope or "legacy"})

    data = request.get_json(force=True) or {}
    new_provider = data.get("provider")
    scope = (data.get("scope") or "").strip()
    if new_provider not in ("novelai", "comfyui"):
        return jsonify({"error": "provider must be novelai|comfyui"}), 400

    if scope == "moment":
        img["provider_moment"] = new_provider
    elif scope == "telegram":
        img["provider_telegram"] = new_provider
    elif scope == "":
        # 兼容：不传 scope 同时切两个 + 兼容字段
        img["provider"] = new_provider
        img["provider_moment"] = new_provider
        img["provider_telegram"] = new_provider
    else:
        return jsonify({"error": "scope must be moment|telegram or omitted"}), 400

    import yaml
    with open(global_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(g, f, allow_unicode=True, sort_keys=False, width=4096)
    return jsonify({"provider": new_provider, "scope": scope or "all", "ok": True})


@app.route("/api/profile/<bot_id>", methods=["GET"])
def api_get_profile(bot_id):
    return jsonify(db.get_profile(bot_id))


@app.route("/api/profile/<bot_id>", methods=["POST"])
def api_set_profile(bot_id):
    data = request.get_json(force=True) or {}
    avatar = data.get("avatar_url")
    banner = data.get("banner_url")
    # 后端兜底：拒绝单字段 > 300KB（base64），防止前端绕过
    for k, v in [("avatar_url", avatar), ("banner_url", banner)]:
        if v and len(v) > 300 * 1024:
            return jsonify({"error": f"{k} too large ({len(v)} bytes), must <= 300KB"}), 400
    if avatar is not None or banner is not None:
        db.set_profile(bot_id, avatar_url=avatar, banner_url=banner)
    if "display_name" in data:
        new_name = (data.get("display_name") or "").strip()
        if len(new_name) > 30:
            return jsonify({"error": "display_name too long (>30)"}), 400
        db.set_display_name(bot_id, new_name)
    return jsonify({"ok": True, **db.get_profile(bot_id)})


@app.route("/api/moment/<int:moment_id>")
def api_moment_detail(moment_id):
    m = db.get_moment(moment_id)
    if not m:
        return jsonify({"error": "not found"}), 404
    out = _format_moment(m)
    out["likers"] = [r["liker"] for r in db.list_likers(moment_id)]
    out["comments"] = db.list_comments(moment_id)
    return jsonify(out)


@app.route("/image/<path:filename>")
def serve_image(filename):
    """图片可能在 ~/resource/media/ 下"""
    full_path = "/" + filename if not filename.startswith("/") else filename
    if not os.path.exists(full_path):
        return "Not found", 404
    directory = os.path.dirname(full_path)
    name = os.path.basename(full_path)
    resp = send_from_directory(directory, name)
    # 图片内容不会变（含时间戳路径），让浏览器永久缓存
    resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
    return resp


if __name__ == "__main__":
    db.init()
    port = int(os.environ.get("MOMENTS_WEB_PORT", "8765"))
    host = os.environ.get("MOMENTS_WEB_HOST", "0.0.0.0"); app.run(host=host, port=port, debug=False)
