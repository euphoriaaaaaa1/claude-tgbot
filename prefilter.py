"""硬规则预筛——省 LLM 配额的关键闸门。

返回 SKIP 原因字符串则直接 SKIP（不调 judge LLM）；
返回 None 才进入 judge。

核心原则：用户和 bot 在聊天时（静默期内），框架完全静默。
"""
DEFAULT_SILENCE_THRESHOLD_MIN = 30


def hard_prefilter(now, situation, world, wildcard, mood, since_user_min,
                    recent_msgs, global_cfg) -> str | None:
    # 例外：CLAUDEBOTLIFE_BYPASS_SILENCE=1 时**跳过所有硬规则**
    # 用于 /clear hook —— 用户主动唤醒，让 judge + speaking_threshold 自己决定
    import os
    if os.environ.get("CLAUDEBOTLIFE_BYPASS_SILENCE") == "1":
        return None

    silence_threshold = (global_cfg or {}).get("silence_threshold_minutes",
                                                DEFAULT_SILENCE_THRESHOLD_MIN)

    # 1. 静默期判定（核心）
    if since_user_min is not None and since_user_min < silence_threshold:
        return f"非静默期(距上次{since_user_min}min<{silence_threshold}min)"

    # 2. 完全无情境素材 + 心情平稳 → 没东西可说
    has_anything = (
        situation.sporadic
        or situation.hobby
        or wildcard
        or (world and world.matched_news)
        or (world and world.is_special_date)
    )
    if not has_anything and 0.35 < mood < 0.65:
        return "毫无情境素材"

    # 3. 当前 busy 状态 + 没突发事件/强情绪 → 不打扰自己
    if situation.recurring.state.startswith("busy") and not situation.sporadic:
        if 0.25 < mood < 0.75:
            return f"专注时段({situation.recurring.name})无突发"

    # 4. 反重复硬撞：最近消息已含相同关键词
    from generators.recent import recent_topic_collision
    if recent_topic_collision(recent_msgs, situation, wildcard, world, hours=2):
        return "刚说过相同话题"

    # 5. 用户长时间未回（>8h）+ 当下夜间 → 大概率睡了
    if since_user_min and since_user_min > 8 * 60 and (now.hour < 7 or now.hour >= 23):
        return "用户大概率睡眠中"

    return None
