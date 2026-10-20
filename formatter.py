"""拼装最终 self-initiate text。

风格基调（2026-10-09 用户重定，见 PROJECT.md）：**只给情境数据 + 最小引导，不写"教学说明"**——
注入文本的语气会被模型模仿，规则腔 / 需求文档腔会直接传染给台词。
- mood 用描述化表达（高/平/低），不是数字（防 bot 引用"心情指数 0.42"）
- 报头用自然句（时间 + 距上次发言间隔）；user_address="user" 时附一行内部标识提醒
"""
from school_calendar import IN_SESSION, TERM_LABEL_ZH

_WEEKDAY_ZH = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def mood_label(mood: float) -> str:
    if mood >= 0.7:
        return "心情不错"
    if mood >= 0.55:
        return "心情还行"
    if mood >= 0.4:
        return "心情平稳"
    if mood >= 0.25:
        return "有点低落"
    return "情绪低落"


def format_self_initiate(now, situation, world, wildcard, mood, mood_factors,
                          since_user_min, recent_msgs, memory_brief: str = "",
                          user_address: str = "user",
                          pending_likes: list = None,
                          pending_comments: list = None,
                          jiwen_state_desc: str = "",
                          jiwen_last_activity: dict = None,
                          continuation_mode: str = "fresh",
                          thread_tail: list | None = None,
                          hours_since_user: float = 0.0,
                          memory_hook: str = "",
                          identity: str = "") -> str:
    if not isinstance(identity, str):
        raise TypeError("identity 必须是 str，收到 %s" % type(identity).__name__)
    parts = ["[self-initiate]"]
    stamp = f"{now.strftime('%Y-%m-%d')}（{_WEEKDAY_ZH[now.weekday()]}）{now.strftime('%H:%M')}"
    if since_user_min is not None:
        parts.append(f"现在是 {stamp}，距{user_address}上次发言 {since_user_min} 分钟")
    else:
        parts.append(f"现在是 {stamp}，距{user_address}上次发言时间未知")
    if user_address == "user":
        parts.append("（user 是内部标识，最终台词不得原样说出）")

    parts.append("--- 以下是你当下的情境 ---")

    # 当前身份：来自 load_bot 注入的 _identity，只读字符串，不二次计算。
    if identity.strip():
        parts.append(f"你的当前身份：{identity}")

    # L1 真实世界
    di = world.date_info if world else {}
    if di.get("lunar"):
        # 必须带本地时刻：inbox payload 里唯一的时间字段是 UTC（形如
        # 2026-08-15T16:00:01.000Z），模型若拿它当本地时间就会差一个时区、
        # 甚至跨天。这里给出权威的本地时刻，别删。
        parts.append(f"日期：{di.get('weekday_zh')} · {di.get('lunar')} · {now.strftime('%H:%M')}")

    # 学期状态：只在放假/提前返校时说，在校是常态不用讲。读取双重兜底——recurring
    # 可能是 None 或降级路径来的旧对象，直接点 .term 会把"说错话"升级成提示词链路崩溃
    term = getattr(getattr(situation, "recurring", None), "term", IN_SESSION)
    term_label = TERM_LABEL_ZH.get(term, "") if term != IN_SESSION else ""
    if term_label:
        parts.append(f"学期状态：{term_label}")

    if di.get("festival"):
        parts.append(f"今天是【{di['festival']}】")
    if di.get("lunar_festival"):
        parts.append(f"今天是【{di['lunar_festival']}】")
    if di.get("solar_term"):
        parts.append(f"节气：{di['solar_term']}")

    if world and world.weather:
        w = world.weather
        parts.append(f"天气：{w.get('desc_zh', w.get('main', ''))} {w.get('temp', 0):.0f}°C")

    # L4 心情（用描述化）
    label = mood_label(mood)
    parts.append(f"{label}（因为{'、'.join(mood_factors)}）" if mood_factors else label)

    # L4.5 jiwen 4D 内在状态（Phase 4，仅当启用且非空时）
    if jiwen_state_desc:
        parts.append(f"内在状态：{jiwen_state_desc}")

    # L4.6 jiwen 当前活动（自己正在做什么——find_activity 注入）
    if jiwen_last_activity and jiwen_last_activity.get("label"):
        parts.append(f"正在：{jiwen_last_activity.get('label', '')}")

    # L3a 当前活动
    parts.append(f"当前活动：{situation.recurring.name}（{situation.recurring.description}）")

    # L3b 突发事件
    if situation.sporadic:
        name = situation.sporadic.get("name") or situation.sporadic.get("event_name", "")
        effect = situation.sporadic.get("effect", "")
        parts.append(f"突发状况：{name}，{effect}" if effect else f"突发状况：{name}")

    # L3c 爱好冲动
    if situation.hobby:
        parts.append(f"冲动：{situation.hobby['effect']}")

    # L2 wildcard
    if wildcard:
        parts.append(f"今日触动：{wildcard.card}（情绪：{wildcard.emotion}）")

    # L1 RSS 命中
    if world and world.matched_news:
        n = world.matched_news[0]
        parts.append(f"刚刷到：{n['title']} · {n.get('summary','')[:80]}")

    # 历史上的今天（轻装）
    if world and world.on_this_day:
        ot = world.on_this_day[0]
        parts.append(f"历史上的今天（{ot.get('year','')}）：{ot.get('text','')[:60]}")

    # 反重复参考
    if recent_msgs:
        parts.append("")
        parts.append("你最近 3 天说过的话：")
        for m in recent_msgs[-3:]:
            parts.append(f"  · {m}")

    # 长期记忆（数据本身；不写"别逐条复述"这类教学语）
    if memory_brief:
        parts.append("")
        parts.append(f"--- 你对{user_address}的长期记忆 ---")
        parts.append(memory_brief[:800])

    # 记忆话题钩子：挑中的一件用户的事，可以就着它自然起话头
    if memory_hook:
        parts.append("")
        parts.append(f"--- 你还惦记着{user_address}的某件事 ---")
        parts.append(f"  · {memory_hook}")

    # 朋友圈互动反馈（点赞累积）
    if pending_likes:
        parts.append("")
        parts.append(f"--- {user_address}最近赞了你这几条朋友圈 ---")
        for pl in pending_likes[-5:]:
            txt = (pl.get("text") or "")[:60]
            parts.append(f"  · 赞了：「{txt}」")

    # ─── 对话尾巴注入（first_after_user / followup 模式）─────────
    # 让 LLM 自己读上次和用户聊到哪了，自然决定怎么接，不在 Python 做启发式分类
    if continuation_mode in ("first_after_user", "followup") and thread_tail:
        parts.append("")
        h_disp = f"{hours_since_user:.1f} 小时" if hours_since_user < 100 else "很久"
        parts.append(f"--- 上次和{user_address}的对话尾巴（距{user_address}最后发言 {h_disp}）---")
        for role, text, _ts in thread_tail:
            who = user_address if role == "user" else "我"
            # text 已在 get_thread_tail 截 200 字；这里再做单行展示
            single_line = text.replace("\n", " ").strip()
            parts.append(f"{who}: {single_line}")

    parts.append("")
    parts.append(f"--- 现在想给{user_address}发一条消息 ---")
    parts.append("")

    # ─── 模式行（最小引导；教学性说明交给各 bot 的人设与技能）────────
    if continuation_mode == "first_after_user":
        h_disp = f"{hours_since_user:.1f} 小时"
        parts.append(f"【对话延续模式】距{user_address}上次发言已经 {h_disp}，这是你之后第一次主动开口，必须开口；接上次的话题，或另起一个话题")
    elif continuation_mode == "followup":
        parts.append("【对话延续模式】上次主动跟进后对方还没有回；接上次的话题，或另起一个话题，同一话题最多再跟一次")
    else:
        parts.append("可以选择不说（没什么可说的，且距上次对话很近时）")

    return "\n".join(parts)
