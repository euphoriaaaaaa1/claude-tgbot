#!/usr/bin/env python3
"""主入口，被 self-initiate.sh 调用。

用法: python3 life-context.py <bot_id> <chat_id> [--dry-run]
输出（stdout）恰好一行 JSON（接口见 .devflow/INTERFACE-initiate-companion.md A 节）:
  TEXT     {"action":"TEXT","text":...,"metadata":{..., "moment_id": int|null}}
  SKIP     {"action":"SKIP","reason":<枚举码>,"detail":<规则名与数字>,"retry_after_s":int|null}
  FALLBACK {"action":"FALLBACK","reason":"error","detail":<异常类型名>}

退出码：
  0  正常（输出有效 JSON）
  1  内部错误（self-initiate.sh 应跳过本次写 inbox）
"""
import sys
import os
import json
import math
import time
import traceback
from datetime import datetime

# 让本地模块可 import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bot_features
import config_loader
import db
import holiday
import quota
import pause
import prefilter
import judge
import signature as sig_mod
import formatter
import chat_history
from jiwen import reader as jiwen_reader
from generators import mood as mood_mod
from generators import situation as situation_mod
from generators import wildcard as wildcard_mod
from generators import world as world_mod
from generators import recent as recent_mod
from memory.memory_inject import read_memory_brief, pick_memory_hook
from moments.post import maybe_post_moment


MOMENT_JUST_POSTED_HINT = "（你几分钟前刚发了一条朋友圈，私聊别复述同一内容）"


def _asleep_reason(bot_cfg: dict, now) -> str:
    """判睡口径：优先 situation.asleep_reason（含赖床段）；旧版没有它时退回 sleep_hours。"""
    fn = getattr(situation_mod, "asleep_reason", None)
    if callable(fn):
        return fn(bot_cfg, now) or ""
    if config_loader.in_sleep_hours(bot_cfg, now):
        return "sleep_hours"
    return ""


def _asleep_until(bot_cfg: dict, now):
    fn = getattr(situation_mod, "asleep_until", None)
    return fn(bot_cfg, now) if callable(fn) else None


def emit(action: str, **kw):
    """输出 JSON 到 stdout"""
    out = {"action": action}
    out.update(kw)
    print(json.dumps(out, ensure_ascii=False))


def emit_skip(reason: str, detail: str = "", retry_after_s=None):
    """SKIP 固定四键：reason 是接口 A 的枚举码（dispatcher 重试表的键），detail 只放
    规则名与数字，retry_after_s 为正整数秒（上限 86400），算不出一律 null。"""
    if isinstance(retry_after_s, bool) or not isinstance(retry_after_s, int) or retry_after_s < 1:
        retry_after_s = None
    else:
        retry_after_s = min(retry_after_s, 86400)
    emit("SKIP", reason=reason, detail=detail, retry_after_s=retry_after_s)


def _features(bot_id: str, bot_cfg: dict) -> dict:
    """这只 bot 的八项开关，整次只读一次（文件不在全开，读不出全关）。频道目录经 bot_features.channel_dir，与壳写开关的
    是同一个目录（全仓唯一的取法）；规则用已读出的 bot_cfg 算，不再读一遍全部 yml。"""
    return bot_features.features_of(bot_id, bot_cfg)


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        sys.stderr.write("用法: life-context.py <bot_id> <chat_id> [--dry-run]\n")
        sys.exit(1)
    bot_id = args[0]
    chat_id = args[1]
    dry_run = "--dry-run" in args

    # 初始化数据库
    db.init()

    # 加载配置
    try:
        global_cfg = config_loader.load_global()
        bot_cfg = config_loader.load_bot(bot_id)
    except Exception as e:
        sys.stderr.write(f"配置加载失败：{e}\n")
        sys.exit(1)

    now = datetime.now()
    # 功能开关（PLAN-bot-features D2；INTERFACE 3.4.8）：生活作息关 → 不判睡、情境/新闻/每日卡片置空；
    # 关系进展关 → 不读积温；朋友圈关 → 第 5 步不发帖；主动找你聊关 → 朋友圈步骤照常，私聊这一路回 SKIP feature_off
    feats = _features(bot_id, bot_cfg)

    # 节假日缓存刷新（唯一联网入口）：放在 pause/quota 之前，免得长期 pause 时
    # 明年数据一直拉不到；每进程只调一次，拿不到锁/负缓存期内都不联网。dry-run 不联网不写缓存。
    if not dry_run:
        try:
            targets = [now.year] + ([now.year + 1] if now.month >= 11 else [])
            sys.stderr.write(f"[holiday] refresh {holiday.refresh_years(targets)}\n")
        except Exception as e:
            sys.stderr.write(f"[holiday] refresh skipped: {type(e).__name__}\n")

    # ─── 0. 全局/bot pause 检查 ─────────────────────────
    paused = pause.is_paused(bot_id)
    if paused:
        emit_skip("paused", str(paused))
        return

    # ─── 1. 配额 ───────────────────────────────────────
    # /clear hook 路径（用户主动唤醒）允许 bypass，但有硬顶防滥用：
    # quota 真的过 cap*1.5 还是要挡（防连续 /clear 烧爆 Anthropic 真实配额）
    q = quota.check_quota(global_cfg) if not dry_run else "ok"
    bypass_quota = os.environ.get("CLAUDEBOTLIFE_BYPASS_SILENCE") == "1"
    if q == "over" and not bypass_quota:
        emit_skip("quota", "5h 配额耗尽")
        return
    if bypass_quota and q == "over":
        # 检查硬顶
        cap = (global_cfg or {}).get("max_calls_per_5h", quota.DEFAULT_MAX_CALLS_PER_5H)
        used = quota.current_usage(global_cfg)["used_weighted"]
        if used >= cap * 1.5:
            emit_skip("quota", f"配额硬顶({used}/{cap*1.5:.0f})，bypass 失效")
            return
        sys.stderr.write(f"[{bot_id}] /clear bypass quota: {used}/{cap}（硬顶 {cap*1.5:.0f}）\n")
    threshold_boost = (q == "warn") and not bypass_quota

    # ─── 2. 睡眠时段 ───────────────────────────────────
    # 判睡只有 asleep_reason 一个口径（探测/发圈/积温同源）；赖床段也算睡
    asleep = _asleep_reason(bot_cfg, now) if feats["life"] else ""
    if asleep:
        until = _asleep_until(bot_cfg, now)
        # 醒来端点本身仍判睡：取整后再多 1 秒，免得回来时还在睡
        retry = math.ceil((until - now).total_seconds()) + 1 if until else None
        emit_skip("asleep", f"{asleep}({now.strftime('%H:%M')})", retry)
        return

    # ─── 3. 收集情境 ───────────────────────────────────
    try:
        if feats["life"]:
            situation = situation_mod.collect(bot_id, now, bot_cfg)
            world_snap = world_mod.collect_world(bot_cfg, now)
            wildcard_pick = wildcard_mod.pick_today_wildcard(bot_id, now)
        else:
            # 生活作息关着：没有作息、突发与爱好（不抽突发，免得写进库），当作一直是自由时间；
            # 新闻归生活作息（不拉订阅源），每日卡片不取；日期、节日、天气照旧
            situation = situation_mod.SituationContext(recurring=situation_mod.Activity(
                name="自由时间", description="没什么特别的事", state="free"))
            world_snap = world_mod.collect_world(dict(bot_cfg, rss_feeds=[]), now)
            wildcard_pick = None
        mood_val, mood_factors = mood_mod.mood_at(now, bot_id, situation, world_snap, bot_cfg)
        recent_msgs = recent_mod.recent_assistant_messages(
            bot_cfg["bot_channel_path"], days=3, limit=10
        )
        since_user_min = chat_history.mins_since_last_user_msg(
            bot_cfg["bot_channel_path"], chat_id=chat_id, bot_name=bot_id
        )
    except Exception as e:
        sys.stderr.write(f"情境收集失败：{e}\n{traceback.format_exc()}\n")
        emit("FALLBACK", reason="error", detail=type(e).__name__)
        return

    # ─── 3.6 对话接续模式判定（独立 try，失败降级为 fresh，不影响主流程）────
    # Python 只算时间戳 + 拿对话尾巴。LLM 自己读尾巴决定怎么接，不在这里做启发式。
    continuation_mode = "fresh"
    thread_tail = []
    hours_since_user = 999.0
    try:
        now_ts = int(now.timestamp())
        last_user_ts = chat_history.last_user_msg_ts(
            bot_cfg["bot_channel_path"], chat_id=chat_id, bot_name=bot_id) or 0
        last_self_init_ts = db.last_self_initiate_ts(bot_id, chat_id) or 0
        if last_user_ts > 0:
            hours_since_user = max(0.0, (now_ts - last_user_ts) / 3600)
            if hours_since_user <= 48:
                if last_user_ts > last_self_init_ts:
                    continuation_mode = "first_after_user"
                else:
                    continuation_mode = "followup"
        if continuation_mode != "fresh":
            thread_tail = chat_history.get_thread_tail(
                bot_cfg["bot_channel_path"], chat_id=chat_id, n=12, max_hours=72,
                bot_name=bot_id)
            if not thread_tail:
                continuation_mode = "fresh"
        sys.stderr.write(
            f"[{bot_id}] continuation_mode={continuation_mode} "
            f"hours_since_user={hours_since_user:.1f} tail_len={len(thread_tail)}\n")
    except Exception as e:
        sys.stderr.write(f"[{bot_id}] 接续模式判定失败（降级 fresh）：{e}\n")

    # ─── 3.5 签名（QQ 风格个性签名）—— bot 自决要不要换 ────
    if not dry_run:
        try:
            sig_mod.maybe_update_signature(
                bot_id, bot_cfg, mood_val, mood_factors,
                situation.sporadic, wildcard_pick, world_snap,
            )
        except Exception as e:
            sys.stderr.write(f"[{bot_id}] 签名调用失败：{e}\n")

    # ─── 3.7 jiwen 4D 状态读取（Phase 4）─────────────────
    # 失败/未启用 → jiwen_info=None，全流程走老逻辑
    # 关系进展关着：不读积温，强制开口与关系语气都不带
    jiwen_info = None
    if feats["relationship"]:
        try:
            jiwen_info = jiwen_reader.read(bot_id, chat_id, global_cfg)
        except Exception as e:
            sys.stderr.write(f"[{bot_id}] jiwen 读取失败（不影响主流程）：{e}\n")

    # forced：connection 撑满，跳过 prefilter+judge 直接说话
    jiwen_forced = bool(jiwen_info and jiwen_info.get("forced"))
    jiwen_state_desc = (jiwen_info or {}).get("description", "")
    jiwen_style_guidance = (jiwen_info or {}).get("style_guidance", "")

    # Bug 防护：state 是 5min 滞后的，user 刚回复时 jiwen 可能还没消化
    # 距上次用户消息<15min 时降级 forced，避免 bypass prefilter 后连发
    if jiwen_forced and isinstance(since_user_min, (int, float)) and since_user_min < 15:
        sys.stderr.write(
            f"[{bot_id}] jiwen forced 但距用户消息 {since_user_min}min<15，降级走正常流程\n")
        jiwen_forced = False

    # ─── 4. 硬规则预筛（省 LLM 配额）────────────────────
    if jiwen_forced:
        skip_reason = None  # 强制开口，跳过 prefilter
        sys.stderr.write(f"[{bot_id}] jiwen forced：bypass prefilter\n")
    else:
        skip_reason = prefilter.hard_prefilter(
            now, situation, world_snap, wildcard_pick, mood_val,
            since_user_min, recent_msgs, global_cfg,
        )
    if skip_reason:
        # 公开版旧 prefilter 返回 str；CBL 新口径返回 (code, detail, retry_after_s)。
        if isinstance(skip_reason, tuple):
            skip_code, skip_detail, skip_retry = skip_reason
        else:
            skip_code, skip_detail, skip_retry = str(skip_reason), "", None
        db.log_heartbeat(
            bot_id, int(now.timestamp()), mood_val, mood_factors,
            situation.recurring.name,
            (situation.sporadic or {}).get("name", ""),
            (situation.hobby or {}).get("name", ""),
            (wildcard_pick.card if wildcard_pick else ""),
            False, "", True, f"{skip_code}:{skip_detail}", "",
        )
        # prefilter SKIP：朋友圈也跟着安静（prefilter 是"是否值得评估"硬闸）。
        # 主动找你聊关着时这一轮私聊本来就不发：回 feature_off，退避按它（INTERFACE 3.4.8、3.8.4 第 1 条）
        if not feats["proactive"]:
            emit_skip("feature_off", "proactive")
            return
        emit_skip(skip_code, skip_detail, skip_retry)
        return

    # ─── 4b. find_activity 集成：jiwen 触发时让 bot 自己做事 ───
    # 心情差/嘴硬/焦躁时，挑一个 SFW hobby 注入 situation.hobby
    # 同时调 set_activity 写回 jiwen state（让 immersion 起来缓解 connection）
    # 生活作息关着时情境保持为空，不往里塞爱好
    if feats["life"] and jiwen_info and jiwen_info.get("find_activity"):
        try:
            fa = jiwen_info["find_activity"]
            activity_type = fa.get("suggested_activity_type", "browse")
            chosen = _pick_activity_by_type(bot_cfg, activity_type)
            if chosen:
                if not situation.hobby:
                    # 用现有 Activity dataclass 风格，注入 hobby 字段
                    situation = situation.__class__(
                        recurring=situation.recurring,
                        sporadic=situation.sporadic,
                        hobby={
                            "name": chosen.get("name", ""),
                            "effect": chosen.get("effect", ""),
                            "kind": "find_activity",
                        },
                    )
                if not dry_run:
                    _write_back_activity(bot_id, chat_id, jiwen_info["state"],
                                         activity_type, chosen.get("name", ""),
                                         global_cfg)
                sys.stderr.write(
                    f"[{bot_id}] find_activity({fa.get('reason')})"
                    f" → set_activity({activity_type}, {chosen.get('name')})"
                    f"{' [dry-run skip write]' if dry_run else ''}\n"
                )
        except Exception as e:
            sys.stderr.write(f"[{bot_id}] find_activity 集成失败（不影响主流程）：{e}\n")

    # ─── 5. 朋友圈独立路径（不依赖 judge action）───────────
    # 朋友圈 = 情境驱动（hobby/wildcard/news/sporadic/find_activity/mood_extreme）
    # dm = jiwen 驱动 + judge 决定
    # 两条路并行，互不干涉
    moment_id = None
    if feats["moments"] and not dry_run:
        try:
            moment_id = _try_post_moment(
                bot_id, bot_cfg, now, situation, world_snap, wildcard_pick,
                mood_val, mood_factors, since_user_min, global_cfg,
                jiwen_info=jiwen_info,
            )
        except Exception as e:
            sys.stderr.write(f"[{bot_id}] 朋友圈独立路径失败（不影响 dm）：{e}\n")

    # ─── 5b. 主动找你聊关着：朋友圈已照常走过，私聊这一路到此为止（积温强制开口也在这之后，一并不发）───
    if not feats["proactive"]:
        emit_skip("feature_off", "proactive")
        return

    # ─── 6. LLM judge：决定 dm vs skip（不再决定朋友圈）─────
    if dry_run:
        action, reason = "dm", "[dry-run] 跳过 judge"
    elif jiwen_forced:
        action, reason = "dm", "jiwen forced"
    else:
        ctx = _build_judge_ctx(now, situation, world_snap, wildcard_pick,
                                mood_val, mood_factors, since_user_min, recent_msgs)
        ctx["jiwen_state_desc"] = jiwen_state_desc
        ctx["jiwen_style_guidance"] = jiwen_style_guidance
        action, reason = judge.judge_action(
            bot_cfg.get("persona_summary", ""),
            ctx,
            threshold_boost=threshold_boost,
            speaking_threshold=bot_cfg.get("speaking_threshold", ""),
        )

    # 6c. action=skip → 不发 self-initiate text（朋友圈已独立处理过）
    if action == "skip":
        db.log_heartbeat(
            bot_id, int(now.timestamp()), mood_val, mood_factors,
            situation.recurring.name,
            (situation.sporadic or {}).get("name", ""),
            (situation.hobby or {}).get("name", ""),
            (wildcard_pick.card if wildcard_pick else ""),
            False, reason, True, f"action=skip:{reason}", "",
        )
        # judge 的原文可能带话题正文，只进 heartbeat/日志，不进 detail
        emit_skip("judge_no", "judge")
        return

    # action ∈ {"dm", "both"}：继续走 self-initiate 私聊路径

    # ─── 6. 拼装富情境 + 计 worker 配额 ─────────────────
    memory_brief = read_memory_brief(bot_cfg["bot_channel_path"], max_chars=800)
    memory_hook = pick_memory_hook(bot_cfg["bot_channel_path"], chat_id=chat_id)

    # 取上次心跳后被赞的朋友圈（只看用户的赞，不包括 bot 自赞）
    last_hb = db.last_heartbeat_ts(bot_id) or (int(now.timestamp()) - 7 * 86400)
    pending_likes = db.pending_likes_for_bot(bot_id, since_ts=last_hb)

    text = formatter.format_self_initiate(
        now, situation, world_snap, wildcard_pick,
        mood_val, mood_factors, since_user_min, recent_msgs,
        memory_brief=memory_brief,
        user_address=bot_cfg.get("user_address", "用户"),
        pending_likes=pending_likes,
        jiwen_state_desc=jiwen_state_desc,
        jiwen_last_activity=(jiwen_info["state"].last_activity if jiwen_info and jiwen_info.get("state") else None),
        continuation_mode=continuation_mode,
        thread_tail=thread_tail,
        hours_since_user=hours_since_user,
        memory_hook=memory_hook,
        identity=bot_cfg.get("_identity", ""),
    )

    if not dry_run:
        # worker_trigger 计费移到 self-initiate.sh：仅在 inbox 文件**真送达**到
        # 在线 worker 之后才记账。否则 race 期 emit("TEXT") 但 notification 被
        # 静默吞掉时，quota 白白扣 5 weight（实测 5h 75 weight 是空消耗）。
        # 标记事件已被注入（不能完全确定 bot 真的会提，但至少避免下次又强 SKIP）
        if situation.sporadic and situation.sporadic.get("started_at"):
            db.mark_event_mentioned(
                bot_id,
                situation.sporadic["name"],
                situation.sporadic["started_at"],
                int(now.timestamp()),
            )

    # dry-run 不写 heartbeat：否则 judge_speak=1 会污染 last_self_initiate_ts，
    # 反复 dry-run 会把接续模式从 first_after_user 错判成 followup
    if not dry_run:
        db.log_heartbeat(
            bot_id, int(now.timestamp()), mood_val, mood_factors,
            situation.recurring.name,
            (situation.sporadic or {}).get("name", ""),
            (situation.hobby or {}).get("name", ""),
            (wildcard_pick.card if wildcard_pick else ""),
            True, reason, False, "", text,
        )

    # 发圈与私聊各走各的周期、互不取消（S-02 修复）；同 tick 刚发过圈时提醒模型别复述
    if moment_id:
        text = text.rstrip("\n") + "\n" + MOMENT_JUST_POSTED_HINT

    emit("TEXT", text=text, metadata={
        "moment_id": moment_id if moment_id else None,
        "mood": mood_val,
        "mood_factors": mood_factors,
        "current_activity": situation.recurring.name,
        "sporadic": (situation.sporadic or {}).get("name"),
        "hobby": (situation.hobby or {}).get("name"),
        "wildcard": wildcard_pick.card if wildcard_pick else None,
        "judge_reason": reason,
        "jiwen_forced": jiwen_forced,
        "jiwen_state_desc": jiwen_state_desc or None,
        "dry_run": dry_run,
    })


def _build_judge_ctx(now, situation, world, wildcard, mood, mood_factors,
                      since_user_min, recent_msgs):
    di = world.date_info if world else {}
    special = di.get("festival") or di.get("lunar_festival") or di.get("solar_term")
    news_desc = "无"
    if world and world.matched_news:
        news_desc = world.matched_news[0]["title"][:50]
    hobby_desc = "无"
    if situation.hobby:
        h = situation.hobby
        prefer = "（私聊专属）" if h.get("prefer_channel") == "dm" else ""
        hobby_desc = f"{h.get('name','')}{prefer} - {h.get('effect','')}"[:120]
    return {
        "recurring": situation.recurring.name,
        "recurring_desc": situation.recurring.description,
        "recurring_state": situation.recurring.state,
        "mood_desc": formatter.mood_label(mood),
        "mood_factors": mood_factors,
        "sporadic_desc": (situation.sporadic.get("name") + " - " +
                          situation.sporadic.get("effect", ""))[:80] if situation.sporadic else "无",
        "hobby_desc": hobby_desc,
        "wildcard_desc": (wildcard.card if wildcard else "无"),
        "news_desc": news_desc,
        "special_date_desc": special or "无",
        "since_user_min": since_user_min if since_user_min is not None else "未知",
        "recent_topics": " | ".join(recent_msgs[-3:]) if recent_msgs else "无",
    }


def _try_post_moment(bot_id, bot_cfg, now, situation, world, wildcard,
                      mood, mood_factors, since_user_min, global_cfg,
                      jiwen_info=None):
    """朋友圈触发是独立判断。失败不影响主流程。返回 moment_id 或 None。"""
    try:
        return maybe_post_moment(
            bot_id, bot_cfg, now, situation, world, wildcard,
            mood, mood_factors, since_user_min, global_cfg,
            jiwen_info=jiwen_info,
        )
    except Exception as e:
        sys.stderr.write(f"moments 触发失败（不影响主流程）：{e}\n")
        return None


# ─── find_activity 集成辅助 ────────────────────────────────

# 关键词模糊匹配表（hobby name 没有 activity_type 字段时用）
_ACTIVITY_KEYWORDS = {
    "cooking": ["烤", "做饭", "煮", "做菜", "饼干", "煲", "炖", "炒", "烘焙", "厨房", "面包"],
    "selfcare": ["面膜", "穿搭", "化妆", "敷", "美容", "护肤", "穿"],
    "browse": ["分享", "刷", "逛", "看小红书", "刷手机", "刷微博", "购物"],
    "reading": ["读", "看书", "小说", "茶艺", "钻研", "学习"],
    "search": ["查", "搜", "找资料", "百度"],
    "observe": ["看", "撸猫", "听", "发呆", "零食"],
}


def _pick_activity_by_type(bot_cfg: dict, activity_type: str) -> dict | None:
    """从 bot 的 personal_hobbies 挑一个匹配 activity_type 的 SFW hobby。

    优先查 hobby.activity_type 显式字段；没有就按 _ACTIVITY_KEYWORDS 模糊匹配 name。
    过滤 prefer_channel=='dm' 的（NSFW hobby 不能上墙做 activity）。
    返回 dict 或 None。
    """
    hobbies = bot_cfg.get("personal_hobbies") or []
    candidates = []
    for h in hobbies:
        if h.get("prefer_channel") == "dm":
            continue
        # 显式字段优先
        if h.get("activity_type") == activity_type:
            candidates.append(h)
            continue
        # 关键词模糊匹配
        kws = _ACTIVITY_KEYWORDS.get(activity_type, [])
        name = h.get("name", "")
        if any(kw in name for kw in kws):
            candidates.append(h)
    if not candidates:
        # 找不到就回退第一个 SFW hobby
        sfw = [h for h in hobbies if h.get("prefer_channel") != "dm"]
        return sfw[0] if sfw else None
    # 用 rng 随机抽一个，避免每次同 type 总是第一个
    import rng
    idx = int(rng.uniform(0, len(candidates) - 1) + 0.5) if len(candidates) > 1 else 0
    return candidates[min(idx, len(candidates) - 1)]


def _write_back_activity(bot_id: str, chat_id: str, state, activity_type: str,
                          label: str, global_cfg: dict) -> None:
    """调 engine.set_activity 后原子写回 jiwen state 文件。

    与 tick 的"读-评-写"共用 <state>.lock（跨进程 flock）：锁内重读磁盘最新 state、
    以它为基底 set_activity（合并），不拿调用方手上可能已陈旧的 state 整份覆盖，
    免得把 tick 并发写下的 connection 等一起抹掉。
    """
    try:
        from jiwen import engine as _eng
        from jiwen import tick as _tick
        rates = _tick.merge_rates(global_cfg.get("jiwen", {}), {})
        state_dir = global_cfg.get("jiwen", {}).get("state_dir") or \
            os.path.expanduser("~/.claude/dispatcher/.jiwen-state")
        path = _tick.state_path(state_dir, bot_id, chat_id)
        with _tick.state_lock(path):
            latest = _tick.load_state(path)
            # 磁盘比手上新 → 以磁盘为基底；否则退回入参（磁盘不存在时默认 State 的
            # last_tick_ts≈now，会让 latest 恒新，但那种情况下也没有别的更新要保）
            base = latest if latest.last_tick_ts >= getattr(state, "last_tick_ts", 0) else state
            new_state = _eng.set_activity(base, activity_type, label, rates)
            if new_state is base:
                return  # 冷却拒绝
            _tick.save_state(path, new_state)
    except Exception as e:
        sys.stderr.write(f"[{bot_id}] _write_back_activity 失败：{e}\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.stderr.write(f"life-context 异常：{type(e).__name__}: {e}\n")
        sys.stderr.write(traceback.format_exc())
        sys.exit(1)
