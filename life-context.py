#!/usr/bin/env python3
"""主入口，被 self-initiate.sh 调用。

用法: python3 life-context.py <bot_id> <chat_id> [--dry-run]
输出（stdout）: JSON {"action": "TEXT|SKIP|FALLBACK", "text": "...", "metadata": {...}}

退出码：
  0  正常（输出有效 JSON）
  1  内部错误（self-initiate.sh 应跳过本次写 inbox）
"""
import sys
import os
import json
import time
import traceback
from datetime import datetime

# 让本地模块可 import
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config_loader
import db
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


def emit(action: str, **kw):
    """输出 JSON 到 stdout"""
    out = {"action": action}
    out.update(kw)
    print(json.dumps(out, ensure_ascii=False))


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

    # ─── 0. 全局/bot pause 检查 ─────────────────────────
    paused = pause.is_paused(bot_id)
    if paused:
        emit("SKIP", reason=paused)
        return

    # ─── 1. 配额 ───────────────────────────────────────
    # /clear hook 路径（用户主动唤醒）允许 bypass，但有硬顶防滥用：
    # quota 真的过 cap*1.5 还是要挡（防连续 /clear 烧爆 Anthropic 真实配额）
    q = quota.check_quota(global_cfg) if not dry_run else "ok"
    bypass_quota = os.environ.get("CLAUDEBOTLIFE_BYPASS_SILENCE") == "1"
    if q == "over" and not bypass_quota:
        emit("SKIP", reason="5h 配额耗尽")
        return
    if bypass_quota and q == "over":
        # 检查硬顶
        cap = (global_cfg or {}).get("max_calls_per_5h", quota.DEFAULT_MAX_CALLS_PER_5H)
        used = quota.current_usage(global_cfg)["used_weighted"]
        if used >= cap * 1.5:
            emit("SKIP", reason=f"配额硬顶({used}/{cap*1.5:.0f})，bypass 失效")
            return
        sys.stderr.write(f"[{bot_id}] /clear bypass quota: {used}/{cap}（硬顶 {cap*1.5:.0f}）\n")
    threshold_boost = (q == "warn") and not bypass_quota

    # ─── 2. 睡眠时段 ───────────────────────────────────
    if config_loader.in_sleep_hours(bot_cfg, now):
        emit("SKIP", reason=f"sleep_hours({now.strftime('%H:%M')})")
        return

    # ─── 3. 收集情境 ───────────────────────────────────
    try:
        situation = situation_mod.collect(bot_id, now, bot_cfg)
        world_snap = world_mod.collect_world(bot_cfg, now)
        wildcard_pick = wildcard_mod.pick_today_wildcard(bot_id, now)
        mood_val, mood_factors = mood_mod.mood_at(now, bot_id, situation, world_snap, bot_cfg)
        recent_msgs = recent_mod.recent_assistant_messages(
            bot_cfg["bot_channel_path"], days=3, limit=10
        )
        since_user_min = chat_history.mins_since_last_user_msg(
            bot_cfg["bot_channel_path"], chat_id=chat_id, bot_name=bot_id
        )
    except Exception as e:
        sys.stderr.write(f"情境收集失败：{e}\n{traceback.format_exc()}\n")
        emit("FALLBACK", reason=f"context error: {type(e).__name__}")
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
    jiwen_info = None
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
        skip_reason = ""  # 强制开口，跳过 prefilter
        sys.stderr.write(f"[{bot_id}] jiwen forced：bypass prefilter\n")
    else:
        skip_reason = prefilter.hard_prefilter(
            now, situation, world_snap, wildcard_pick, mood_val,
            since_user_min, recent_msgs, global_cfg,
        )
    if skip_reason:
        db.log_heartbeat(
            bot_id, int(now.timestamp()), mood_val, mood_factors,
            situation.recurring.name,
            (situation.sporadic or {}).get("name", ""),
            (situation.hobby or {}).get("name", ""),
            (wildcard_pick.card if wildcard_pick else ""),
            False, "", True, skip_reason, "",
        )
        # prefilter SKIP：朋友圈也跟着安静（prefilter 是"是否值得评估"硬闸）
        emit("SKIP", reason=skip_reason)
        return

    # ─── 4b. find_activity 集成：jiwen 触发时让 bot 自己做事 ───
    # 心情差/嘴硬/焦躁时，挑一个 SFW hobby 注入 situation.hobby
    # 同时调 set_activity 写回 jiwen state（让 immersion 起来缓解 connection）
    if jiwen_info and jiwen_info.get("find_activity"):
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
    if not dry_run:
        try:
            moment_id = _try_post_moment(
                bot_id, bot_cfg, now, situation, world_snap, wildcard_pick,
                mood_val, mood_factors, since_user_min, global_cfg,
                jiwen_info=jiwen_info,
            )
        except Exception as e:
            sys.stderr.write(f"[{bot_id}] 朋友圈独立路径失败（不影响 dm）：{e}\n")

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

    # 6b. 朋友圈撞 dm 抑制：刚发了圈就别同时 dm 刷屏
    if moment_id and action == "dm" and not jiwen_forced:
        sys.stderr.write(f"[{bot_id}] 刚发圈，抑制本次 dm 避免撞车\n")
        action, reason = "skip", f"moment_just_posted({moment_id})"

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
        emit("SKIP", reason=f"action=skip:{reason}")
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

    emit("TEXT", text=text, metadata={
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

    并发保护：写入前 reread 最新 state，对比 last_tick_ts；冲突时放弃写入并 warn。
    """
    try:
        from jiwen import engine as _eng
        from jiwen import tick as _tick
        rates = _tick.merge_rates(global_cfg.get("jiwen", {}), {})
        # 设置活动
        new_state = _eng.set_activity(state, activity_type, label, rates)
        if new_state is state:
            return  # 冷却拒绝
        # 写回
        state_dir = global_cfg.get("jiwen", {}).get("state_dir") or \
            os.path.expanduser("~/.claude/dispatcher/.jiwen-state")
        path = _tick.state_path(state_dir, bot_id, chat_id)
        # 读最新看是否被 tick 覆盖过
        try:
            latest = _tick.load_state(path)
            if latest.last_tick_ts > state.last_tick_ts:
                sys.stderr.write(f"[{bot_id}] _write_back_activity 跳过（state 已被 tick 更新过）\n")
                return
        except Exception:
            pass
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
